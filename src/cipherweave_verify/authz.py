"""Per-tool authorization for a Weave, after ``BearerAuth`` has authenticated.

Authentication answers "is this a token CipherWeave minted for me?". This
module answers "may that token call *this* tool?", in three independent
checks, each of which a Weave declares per tool and none of which depends on
what the calling agent decides:

1. **Scope.** Every tool needs exactly one of ``read``, ``execute`` or
   ``destructive``. The scopes are not a ladder: a ``destructive`` token
   cannot read unless it was also granted ``read``. A client registered with
   ``read`` alone gets tokens that cannot actuate anything, whatever its agent
   is talked into.
2. **Step-up.** A tool that can open a door or delete state also needs a
   token minted at most ``step_up_seconds`` ago (60 by default), measured
   from ``iat``. A token copied out of a transcript ten minutes into its
   fifteen-minute life still reads, but it cannot unlock.
3. **Single use.** A step-up call also spends the token's ``jti`` in a
   ledger shared by every instance of the Weave. The same fresh token
   presented twice -- a replay inside the 60 seconds -- is refused the
   second time. Without a ledger configured, step-up tools are refused
   outright rather than run without the replay check.

The static legacy token (accepted only while ``ACCEPT_LEGACY_TOKEN`` is on)
carries the scopes named in ``LEGACY_TOKEN_SCOPES`` (default: all three, the
status quo) and can **never** satisfy step-up: it is the long-lived
credential step-up exists to keep away from the front door.

Failures are ``AuthorizationError`` with ``status_code`` 403 and a short
``reason`` that is safe to log. Like the verifier, nothing here logs a token.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

SCOPE_READ = "read"
SCOPE_EXECUTE = "execute"
SCOPE_DESTRUCTIVE = "destructive"
TOOL_SCOPES = frozenset({SCOPE_READ, SCOPE_EXECUTE, SCOPE_DESTRUCTIVE})

DEFAULT_STEP_UP_SECONDS = 60

ENV_LEGACY_SCOPES = "LEGACY_TOKEN_SCOPES"
ENV_SCOPE_MODE = "CIPHERWEAVE_SCOPE_MODE"
MODE_ENFORCE = "enforce"
MODE_AUDIT = "audit"


class AuthorizationError(Exception):
    """Authenticated, but not allowed to make this call. Answer 403."""

    status_code = 403
    reason = "forbidden"

    def __init__(self, message: str = "forbidden", *, reason: str | None = None) -> None:
        super().__init__(message)
        if reason is not None:
            self.reason = reason


class ScopeRequiredError(AuthorizationError):
    reason = "insufficient_scope"


class StepUpRequiredError(AuthorizationError):
    """The tool needs a freshly minted CipherWeave token."""

    reason = "step_up_required"


class ReplayedTokenError(AuthorizationError):
    reason = "token_replayed"


class LedgerUnavailableError(AuthorizationError):
    """The jti ledger could not be written. Fail closed: no replay check, no call."""

    reason = "replay_ledger_unavailable"


@dataclass(frozen=True)
class ToolPolicy:
    scope: str
    step_up: bool = False

    def __post_init__(self) -> None:
        if self.scope not in TOOL_SCOPES:
            raise ValueError(f"unknown tool scope {self.scope!r}")


READ = ToolPolicy(SCOPE_READ)
EXECUTE = ToolPolicy(SCOPE_EXECUTE)
DESTRUCTIVE = ToolPolicy(SCOPE_DESTRUCTIVE, step_up=True)


@dataclass(frozen=True)
class Principal:
    """Who is calling, as far as authorization is concerned."""

    method: str  # "jwt" | "legacy"
    subject: str | None
    scopes: frozenset[str]
    issued_at: int | None = None  # None for the legacy token: never fresh
    expires_at: int | None = None
    jti: str | None = None

    def log_fields(self) -> dict[str, Any]:
        """Identifiers for an audit line. jti names a token; it is not one."""
        return {"method": self.method, "sub": self.subject, "jti": self.jti}


def legacy_scopes() -> frozenset[str]:
    raw = os.environ.get(ENV_LEGACY_SCOPES)
    if raw is None:
        return TOOL_SCOPES
    return frozenset(s for s in raw.replace(",", " ").split() if s in TOOL_SCOPES)


def scope_mode() -> str:
    """``enforce`` unless explicitly ``audit``. Anything unparseable enforces."""
    raw = (os.environ.get(ENV_SCOPE_MODE) or "").strip().lower()
    return MODE_AUDIT if raw == MODE_AUDIT else MODE_ENFORCE


class JtiLedger(Protocol):
    def claim(self, jti: str, expires_at: int) -> bool:
        """Record ``jti``; False if it was already recorded. Raise if unknowable."""


class DynamoJtiLedger:
    """Single-use jti over a DynamoDB table (partition key ``jti``, TTL ``expires_at``).

    A conditional put is atomic across every concurrent Lambda, which an
    in-process set is not. The item lives until the token would have expired
    anyway; after that the verifier refuses the token on its own.
    """

    def __init__(self, table: Any, *, audience: str) -> None:
        self._table = table
        self._audience = audience

    def claim(self, jti: str, expires_at: int) -> bool:
        try:
            self._table.put_item(
                Item={"jti": f"{self._audience}#{jti}", "expires_at": int(expires_at) + 300},
                ConditionExpression="attribute_not_exists(jti)",
            )
        except Exception as exc:
            code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return False
            raise
        return True


class MemoryJtiLedger:
    """In-process ledger for tests. Not for production: one per Lambda instance."""

    def __init__(self) -> None:
        self.seen: set[str] = set()

    def claim(self, jti: str, expires_at: int) -> bool:
        if jti in self.seen:
            return False
        self.seen.add(jti)
        return True


class ToolAuthorizer:
    def __init__(
        self,
        policies: dict[str, ToolPolicy],
        *,
        ledger: JtiLedger | Callable[[], JtiLedger | None] | None = None,
        step_up_seconds: int = DEFAULT_STEP_UP_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.policies = dict(policies)
        self._ledger = ledger
        self._step_up_seconds = step_up_seconds
        self._clock = clock

    def policy(self, tool: str) -> ToolPolicy:
        # A tool with no declared policy is the most restricted kind, so
        # forgetting to declare one cannot open it up.
        return self.policies.get(tool, DESTRUCTIVE)

    def visible(self, principal: Principal, tools: Iterable[str]) -> list[str]:
        """The tools this principal holds the scope for (tools/list)."""
        if scope_mode() == MODE_AUDIT:
            return list(tools)
        return [t for t in tools if self.policy(t).scope in principal.scopes]

    def _resolve_ledger(self) -> JtiLedger | None:
        ledger = self._ledger
        if ledger is None or hasattr(ledger, "claim"):
            return ledger  # type: ignore[return-value]
        return ledger()  # type: ignore[operator]

    def authorize(self, principal: Principal, tool: str) -> ToolPolicy:
        """Raise ``AuthorizationError`` unless ``principal`` may call ``tool`` now."""
        policy = self.policy(tool)
        self.require(principal, policy, what=tool)
        return policy

    def require(self, principal: Principal, policy: ToolPolicy, *, what: str) -> None:
        if policy.scope not in principal.scopes:
            if scope_mode() == MODE_AUDIT:
                logger.warning(
                    "authz audit-only: would refuse %s reason=insufficient_scope sub=%s",
                    what,
                    principal.subject or "-",
                )
            else:
                raise ScopeRequiredError(f"{what} requires scope {policy.scope}")
        if policy.step_up:
            self._step_up(principal, what)

    def _step_up(self, principal: Principal, what: str) -> None:
        # Step-up is enforced in audit mode too: audit mode exists to roll out
        # scope grants, not to reopen the door to a stale or static token.
        if principal.method != "jwt" or principal.issued_at is None or not principal.jti:
            raise StepUpRequiredError(f"{what} requires a fresh CipherWeave token")
        age = self._clock() - principal.issued_at
        if age > self._step_up_seconds:
            raise StepUpRequiredError(
                f"{what} requires a token minted within {self._step_up_seconds}s"
            )
        ledger = self._resolve_ledger()
        if ledger is None:
            raise LedgerUnavailableError(f"{what} requires a replay ledger")
        try:
            fresh = ledger.claim(principal.jti, principal.expires_at or int(self._clock()) + 900)
        except Exception as exc:
            logger.error("jti ledger write failed type=%s", type(exc).__name__)
            raise LedgerUnavailableError("replay ledger unavailable") from exc
        if not fresh:
            raise ReplayedTokenError(f"{what}: this token was already used for a step-up call")
