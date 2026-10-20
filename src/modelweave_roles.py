"""Which model a Weave should call, from ModelWeave's role pins. Vendored.

Copy this one file into a Weave that calls Bedrock. It has no dependency
beyond the standard library and boto3 (imported lazily, and only to read SSM).

    from modelweave_roles import converse

    response, model_id = converse(bedrock, "reasoning",
                                  fallback="us.anthropic.claude-haiku-4-5-20251001-v1:0",
                                  messages=messages, system=system, toolConfig=tools)

``modelweave_client.py`` is the full wrapper -- result cache, spans, outcome
reports -- and suits a simple text call. Most Weaves cannot use it as is: a
tool-using agent needs ``toolConfig``, and every Weave already has its own
observability. What they all need is the one decision ModelWeave exists to
make, which model a role resolves to, and this module is only that:

* ``model(role, fallback)`` reads the pin ``/modelweave/roles/<role>`` (the
  value ``scripts/pin_roles.py`` and the client write: a JSON body with
  ``model_id``, or a bare id) and caches it for five minutes. A missing pin,
  no permission, or SSM being unreachable returns the call site's own
  ``fallback`` -- the model it used before ModelWeave existed -- so reading a
  pin can never be the reason a call fails.
* ``recover(...)`` runs after a Bedrock error. If the error is about the model
  (retired, unknown id, or access denied for that model -- the same rules as
  ``modelweave.model_errors``), it re-reads the pin and returns a different
  model to retry with: a newer pin, else the fallback. A model that failed
  that way is not returned again for the cache period. Anything else
  (throttling, a bad prompt) is not a model error and is not retried.
* ``converse(client, role, fallback, **kwargs)`` does both around one
  ``client.converse`` call and retries at most once.

``MODELWEAVE_ROLES=off`` returns every fallback without reading SSM;
``MODELWEAVE_ROLE_PREFIX`` moves the pins. Embedding calls should not use
this: a stored vector index is only comparable with the model that built it,
so an embedding model changes with a re-index, not with a pin.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_PREFIX = "/modelweave/roles"
DEFAULT_TTL_SECONDS = 300.0
# A pin that could not be read is retried sooner than a good one is refreshed.
UNREADABLE_TTL_SECONDS = 60.0

_NOT_FOUND_CODES = {"resourcenotfoundexception", "modelnotfoundexception", "modelnotfound"}
_ACCESS_CODES = {"accessdeniedexception", "accessdenied", "unauthorizedexception"}
_EOL_PHRASES = (
    "end of life", "end-of-life", "endoflife", "has been retired", "model is retired",
    "no longer available", "reached end of life", "reached the end of its life",
    "discontinued",
)
_INVALID_ID_PHRASES = (
    "model identifier is invalid", "invalid model identifier",
    "could not resolve the foundation model", "unknown model", "model not found",
    "does not exist",
)
_ACCESS_PHRASES = ("access denied", "not authorized", "explicit deny", "is not authorized")


def _norm_code(error_code: str | None) -> str:
    return (error_code or "").strip().lower().replace("_", "").replace(" ", "")


def classify_model_error(error_code: str | None, message: str | None,
                         model_id: str | None) -> str | None:
    """Same rules as ``modelweave.model_errors.classify_model_error``."""
    code = _norm_code(error_code)
    msg = (message or "").lower()
    model = (model_id or "").lower()
    mentions_model = bool(model) and model in msg
    if code in _NOT_FOUND_CODES:
        return "retired"
    if any(phrase in msg for phrase in _EOL_PHRASES):
        return "retired"
    if any(phrase in msg for phrase in _INVALID_ID_PHRASES):
        return "retired"
    if "deprecated" in msg and ("model" in msg or mentions_model):
        return "retired"
    access = code in _ACCESS_CODES or any(phrase in msg for phrase in _ACCESS_PHRASES)
    about_this_model = mentions_model or "foundation-model/" in msg or "inference-profile/" in msg
    if access and about_this_model:
        return "suspect"
    return None


def decode_pin(raw: Any) -> str:
    """The model id in a pin: a JSON body with ``model_id``, or a bare id."""
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return ""
        text = str((data or {}).get("model_id") or "").strip()
    return text if text and not any(c.isspace() for c in text) else ""


def error_parts(exc: BaseException) -> tuple[str, str]:
    """(code, message) from a botocore ClientError, or the exception's type and text."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        return str(error.get("Code") or ""), str(error.get("Message") or "")
    return type(exc).__name__, str(exc)


def _enabled() -> bool:
    return (os.environ.get("MODELWEAVE_ROLES") or "").strip().lower() not in ("off", "false", "0")


class RoleResolver:
    def __init__(
        self,
        ssm: Any = None,
        *,
        prefix: str | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        enabled: bool | None = None,
    ) -> None:
        self._ssm = ssm
        self.prefix = (prefix or os.environ.get("MODELWEAVE_ROLE_PREFIX") or DEFAULT_PREFIX).rstrip("/")
        self._ttl = ttl_seconds
        self._clock = clock
        self._enabled = enabled
        self._pins: dict[str, tuple[str, float]] = {}
        self._failed: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return _enabled() if self._enabled is None else self._enabled

    def _client(self) -> Any:
        if self._ssm is None:
            import boto3

            self._ssm = boto3.client("ssm")
        return self._ssm

    def _read_pin(self, role: str) -> tuple[str, float]:
        name = f"{self.prefix}/{role}"
        try:
            response = self._client().get_parameter(Name=name)
        except Exception as exc:
            code, _ = error_parts(exc)
            if code != "ParameterNotFound":
                logger.warning("modelweave pin %s unreadable (%s); using the fallback", name, code)
            return "", UNREADABLE_TTL_SECONDS
        return decode_pin((response.get("Parameter") or {}).get("Value")), self._ttl

    def _usable(self, model_id: str, now: float) -> bool:
        until = self._failed.get(model_id)
        return bool(model_id) and (until is None or now >= until)

    def model(self, role: str, fallback: str) -> str:
        """The model for ``role``: the pin when there is a usable one, else ``fallback``."""
        if not self.enabled:
            return fallback
        now = self._clock()
        with self._lock:
            cached = self._pins.get(role)
            if cached is None or now >= cached[1]:
                pinned, ttl = self._read_pin(role)
                cached = (pinned, now + ttl)
                self._pins[role] = cached
            pinned = cached[0]
            return pinned if self._usable(pinned, now) else fallback

    def recover(self, role: str, failed_model: str, exc: BaseException,
                fallback: str) -> str | None:
        """A different model to retry with after a model error, or None."""
        code, message = error_parts(exc)
        verdict = classify_model_error(code, message, failed_model)
        if verdict is None:
            return None
        now = self._clock()
        logger.warning("model %s for role %s is %s (%s); recovering", failed_model, role,
                       verdict, code)
        with self._lock:
            self._failed[failed_model] = now + self._ttl
            self._pins.pop(role, None)
        candidate = self.model(role, fallback)
        if candidate and candidate != failed_model:
            return candidate
        if fallback and fallback != failed_model:
            return fallback
        return None


_default: RoleResolver | None = None
_default_lock = threading.Lock()


def default_resolver() -> RoleResolver:
    global _default
    with _default_lock:
        if _default is None:
            _default = RoleResolver()
        return _default


def reset_default_resolver() -> None:
    global _default
    with _default_lock:
        _default = None


def model_for(role: str, fallback: str) -> str:
    return default_resolver().model(role, fallback)


def converse(client: Any, role: str, fallback: str, *, resolver: RoleResolver | None = None,
             **kwargs: Any) -> tuple[dict, str]:
    """``client.converse`` with the role's model; one retry on a model error.

    Returns ``(response, model_id_that_answered)``. ``modelId`` must not be in
    ``kwargs``: choosing it is the point.
    """
    if "modelId" in kwargs:
        raise TypeError("modelId is chosen by the role; do not pass it")
    resolver = resolver or default_resolver()
    model_id = resolver.model(role, fallback)
    try:
        return client.converse(modelId=model_id, **kwargs), model_id
    except Exception as exc:
        retry = resolver.recover(role, model_id, exc, fallback)
        if retry is None:
            raise
        return client.converse(modelId=retry, **kwargs), retry
