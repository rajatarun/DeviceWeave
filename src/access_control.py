"""
Who may actuate what — carried from the MCP auth check to the execution layer.

The MCP handler authenticates a request and authorizes the tool it calls
(``cipherweave_verify.ToolAuthorizer``: scope, step-up, single-use jti). That
is not enough for ``execute``: the tool takes natural language, so whether it
unlocks a door is only known once the command has been resolved to a device
and an action — several layers below the handler, possibly inside an LLM
agent loop.

So the handler puts a ``RequestAuth`` in a context variable for the duration
of the request, and ``execution_planner`` asks it before every step that
grants physical access (``unlock``, ``open``, or ``toggle`` on a device that
can do either). Context variables follow ``asyncio.run`` and every task it
gathers, so the check reaches the conversational agents too.

Fail closed: a physical-access step with **no** ``RequestAuth`` in context —
the unauthenticated REST route, the SMS handler, a scheduled job — is
refused. The only way to open the door is an MCP call carrying a CipherWeave
token with the ``destructive`` scope, minted in the last 60 seconds and never
used for a step-up call before. Locking and closing are not gated: the safe
direction must never need a fresh token.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
from typing import Any, Dict, Iterator, Optional

from cipherweave_verify import DESTRUCTIVE, AuthorizationError, Principal, ToolAuthorizer

logger = logging.getLogger(__name__)

PHYSICAL_ACCESS_ACTIONS = frozenset({"unlock", "open"})

_CURRENT: contextvars.ContextVar[Optional["RequestAuth"]] = contextvars.ContextVar(
    "deviceweave_request_auth", default=None
)


class RequestAuth:
    """The authenticated caller of one request, and its step-up state."""

    def __init__(self, principal: Principal, authorizer: ToolAuthorizer) -> None:
        self.principal = principal
        self.authorizer = authorizer
        # One step-up per request: a scene that unlocks two doors spends the
        # token's jti once, not once per door.
        self._stepped_up = False

    def authorize_tool(self, tool: str) -> None:
        policy = self.authorizer.authorize(self.principal, tool)
        if policy.step_up:
            self._stepped_up = True

    def require_physical_access(self, what: str) -> None:
        if self._stepped_up:
            return
        self.authorizer.require(self.principal, DESTRUCTIVE, what=what)
        self._stepped_up = True


def current() -> Optional[RequestAuth]:
    return _CURRENT.get()


@contextlib.contextmanager
def bind(auth: RequestAuth) -> Iterator[RequestAuth]:
    token = _CURRENT.set(auth)
    try:
        yield auth
    finally:
        _CURRENT.reset(token)


def is_physical_access(device: Dict[str, Any], action: str) -> bool:
    if action in PHYSICAL_ACCESS_ACTIONS:
        return True
    if action == "toggle":
        capabilities = device.get("capabilities") or []
        return any(cap in PHYSICAL_ACCESS_ACTIONS for cap in capabilities)
    return False


def check_step(device: Dict[str, Any], action: str) -> Optional[str]:
    """None if the step may run; otherwise why not (safe to return to the caller)."""
    if not is_physical_access(device, action):
        return None
    device_id = device.get("id", "?")
    auth = current()
    if auth is None:
        _audit(device_id, action, None, "denied", "no_token")
        return (
            f"'{action}' grants physical access and needs a fresh CipherWeave token "
            "with the destructive scope, presented over MCP; this request carried none."
        )
    try:
        auth.require_physical_access(f"{action} {device_id}")
    except AuthorizationError as exc:
        _audit(device_id, action, auth.principal, "denied", exc.reason)
        return f"'{action}' refused: {exc.reason}. Mint a new token and retry within 60 seconds."
    _audit(device_id, action, auth.principal, "allowed", None)
    return None


def _audit(
    device_id: str,
    action: str,
    principal: Optional[Principal],
    outcome: str,
    reason: Optional[str],
) -> None:
    fields = principal.log_fields() if principal else {"method": None, "sub": None, "jti": None}
    logger.info(
        json.dumps(
            {
                "event": "physical_access",
                "device_id": device_id,
                "action": action,
                "outcome": outcome,
                "reason": reason,
                **fields,
            },
            sort_keys=True,
        )
    )
