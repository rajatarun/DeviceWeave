"""Bill each Weave's Bedrock calls to that Weave.

A Bedrock call made with a model id or a system inference profile carries no
tag, so in Cost Explorer every Weave's model spend lands in one undivided
"Amazon Bedrock" line. An *application* inference profile is a copy of a model
or system profile that carries tags: calls made through it are billed with
those tags, and a ``repo`` tag on it puts the call under that repository
beside the Lambda, the table and the bucket that made it.

Each Weave's stack creates one application inference profile per model its
calls can resolve to, tagged ``repo=<RepoName>``, and passes
``BEDROCK_PROFILE_ARNS`` (JSON: model id -> profile ARN). ``attribute(client)``
wraps a ``bedrock-runtime`` client so that ``converse``, ``converse_stream``,
``invoke_model`` and ``invoke_model_with_response_stream`` send the profile ARN
in place of a mapped model id. Everything above the client — role resolution,
observability spans, error classification, pricing — keeps seeing the model id
it chose, because only the request on the wire changes.

Attribution never costs a call. A model id with no profile goes through
unchanged. If Bedrock refuses the profile itself (access denied, or the
profile not found) the call is made once more with the model id, the profile
is not used again in this process, and one ``bedrock_cost_attribution_failed``
line is logged so the gap is visible rather than silent.
``BEDROCK_COST_ATTRIBUTION=off`` turns the rewrite off without a deploy of
code.

Standard library only; the vendored copies in each Weave are checksummed
against this file.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

PROFILES_ENV = "BEDROCK_PROFILE_ARNS"
SWITCH_ENV = "BEDROCK_COST_ATTRIBUTION"
_OPERATIONS = ("converse", "converse_stream", "invoke_model", "invoke_model_with_response_stream")
# Codes Bedrock answers when the profile itself, not the model or the prompt,
# is what it refused.
_PROFILE_ERRORS = {"AccessDeniedException", "ResourceNotFoundException", "ValidationException"}


def enabled() -> bool:
    return os.environ.get(SWITCH_ENV, "on").strip().lower() not in {"off", "false", "0", "no"}


def profiles_from_env() -> dict[str, str]:
    raw = os.environ.get(PROFILES_ENV, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("bedrock_cost_attribution_failed reason=unparseable_%s", PROFILES_ENV)
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items() if isinstance(v, str) and v.startswith("arn:")}


class AttributedClient:
    """A ``bedrock-runtime`` client whose model calls go through tagged profiles."""

    def __init__(self, client: Any, profiles: dict[str, str]) -> None:
        self._client = client
        self._profiles = dict(profiles)
        self._refused: set[str] = set()
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if name in _OPERATIONS:
            return self._attributed(attr)
        return attr

    @property
    def unwrapped(self) -> Any:
        return self._client

    def profile_for(self, model_id: str) -> str | None:
        if not enabled() or model_id in self._refused:
            return None
        return self._profiles.get(model_id)

    def _attributed(self, call: Any) -> Any:
        def invoke(*args: Any, **kwargs: Any) -> Any:
            model_id = kwargs.get("modelId")
            profile = self.profile_for(model_id) if isinstance(model_id, str) else None
            if profile is None:
                return call(*args, **kwargs)
            try:
                return call(*args, **{**kwargs, "modelId": profile})
            except Exception as exc:
                if not _refused_profile(exc, profile):
                    raise
                with self._lock:
                    self._refused.add(model_id)
                logger.error(
                    "bedrock_cost_attribution_failed model=%s profile=%s code=%s; "
                    "calling the model directly, unattributed, for the rest of this process",
                    model_id, profile, _code(exc),
                )
                return call(*args, **kwargs)

        return invoke


def attribute(client: Any, profiles: dict[str, str] | None = None) -> Any:
    """Wrap ``client``; with no profiles configured it is returned unchanged."""
    if isinstance(client, AttributedClient):
        return client
    mapping = profiles_from_env() if profiles is None else profiles
    if not mapping:
        return client
    return AttributedClient(client, mapping)


def _code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str((response.get("Error") or {}).get("Code") or "")
    return type(exc).__name__


def _refused_profile(exc: Exception, profile: str) -> bool:
    """Whether Bedrock refused the profile, as opposed to the model or the request.

    The profile's ARN or id in the message is what separates the two: a
    throttle, a bad prompt or a retired model says nothing about the profile,
    and must surface exactly as it would without attribution.
    """
    if _code(exc) not in _PROFILE_ERRORS:
        return False
    response = getattr(exc, "response", None)
    message = str((response.get("Error") or {}).get("Message") or "") if isinstance(response, dict) else str(exc)
    profile_id = profile.rsplit("/", 1)[-1]
    return profile in message or profile_id in message or "application-inference-profile" in message
