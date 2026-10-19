"""Bearer check for a Weave during the move to CipherWeave tokens.

One decision, in one place, so the four Weaves cannot drift apart on it:

1. A JWT-shaped token is verified as a CipherWeave access token for this
   Weave's audience (when ``CIPHERWEAVE_JWKS`` or ``CIPHERWEAVE_JWKS_URL``
   is set). If it really is a
   JWS (its header decodes to a JSON object with ``alg``) and verification
   fails, that is the answer: 401, without touching the legacy secret. A
   static token is ``openssl rand -hex 32`` and can never be one, and falling
   through would turn "wrong audience" into whatever the legacy loader does
   when its secret is gone — a 500 in DeviceWeave.
2. Otherwise, and only while ``ACCEPT_LEGACY_TOKEN`` is true, the token is
   compared in constant time with the Weave's old static token.
3. Anything else is rejected.

``ACCEPT_LEGACY_TOKEN`` defaults to true, so deploying this changes nothing
for existing callers. Setting it to anything but a true value (``true``,
``1``, ``yes``) turns the legacy path off — an unparseable value fails closed.

The legacy token is loaded lazily: a request carrying a good JWT never reads
Secrets Manager, and with the flag off the old secret is never read at all,
so it can be deleted without breaking the Weave. Whatever the loader raises
propagates, so each Weave keeps its own "secret unavailable" behaviour.

Every decision is logged as ``mcp auth method=jwt|legacy`` or
``mcp auth rejected reason=...``: the owner flips the flag once no
``method=legacy`` line appears.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

from cipherweave_verify.errors import TokenVerificationError
from cipherweave_verify.jwks import b64url_decode
from cipherweave_verify.verify import (
    ENV_JWKS,
    ENV_JWKS_URL,
    Verifier,
    default_verifier,
    looks_like_jwt,
)

logger = logging.getLogger(__name__)

ENV_ACCEPT_LEGACY = "ACCEPT_LEGACY_TOKEN"
_TRUE = frozenset({"true", "1", "yes"})


def legacy_accepted() -> bool:
    raw = os.environ.get(ENV_ACCEPT_LEGACY)
    if raw is None:
        return True
    return raw.strip().lower() in _TRUE


def jwt_configured() -> bool:
    return any((os.environ.get(name) or "").strip() for name in (ENV_JWKS, ENV_JWKS_URL))


def tokens_equal(presented: str, expected: str) -> bool:
    """Constant time, including across lengths: compare fixed-size digests."""
    left = hashlib.sha256(presented.encode("utf-8")).digest()
    right = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)


def is_jws(token: str) -> bool:
    """True when the first segment is a base64url JSON object naming an ``alg``."""
    if not looks_like_jwt(token):
        return False
    try:
        header = json.loads(b64url_decode(token.split(".", 1)[0]))
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(header, dict) and "alg" in header


@dataclass(frozen=True)
class AuthDecision:
    ok: bool
    method: str | None  # "jwt" | "legacy" | None
    subject: str | None = None
    reason: str | None = None


class BearerAuth:
    def __init__(
        self,
        audience: str,
        legacy_token: Callable[[], str | None] | None = None,
        *,
        verifier: Verifier | None = None,
    ) -> None:
        if not audience:
            raise ValueError("audience is required")
        self.audience = audience
        self._legacy_token = legacy_token
        self._verifier = verifier

    def check(self, presented: str | None) -> AuthDecision:
        decision = self._decide(presented)
        if decision.ok:
            logger.info("mcp auth method=%s sub=%s", decision.method, decision.subject or "-")
        else:
            logger.warning("mcp auth rejected reason=%s", decision.reason)
        return decision

    def _decide(self, presented: str | None) -> AuthDecision:
        if not isinstance(presented, str) or not presented:
            return AuthDecision(False, None, reason="missing")

        reason = "invalid_token"
        if looks_like_jwt(presented) and (self._verifier is not None or jwt_configured()):
            try:
                verifier = self._verifier or default_verifier()
                token = verifier.verify(presented, self.audience)
                return AuthDecision(True, "jwt", subject=token.subject)
            except TokenVerificationError as exc:
                if is_jws(presented):
                    return AuthDecision(False, None, reason=exc.reason)
                reason = exc.reason
            except Exception as exc:  # a verifier bug must be a 401, not a 500
                logger.error("cipherweave token check raised type=%s", type(exc).__name__)
                return AuthDecision(False, None, reason="verifier_error")

        if not legacy_accepted():
            return AuthDecision(
                False, None, reason=reason if reason != "invalid_token" else "legacy_disabled"
            )
        if self._legacy_token is None:
            return AuthDecision(False, None, reason=reason)
        expected = self._legacy_token()
        if isinstance(expected, str) and expected and tokens_equal(presented, expected):
            return AuthDecision(True, "legacy")
        if not expected:
            # Still compare so the failure path takes the same time.
            tokens_equal(presented, "\x00")
        return AuthDecision(False, None, reason=reason)
