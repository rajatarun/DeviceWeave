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

An accepted decision carries a ``Principal`` for per-tool authorization
(``cipherweave_verify.authz.ToolAuthorizer``): scopes, ``iat`` and ``jti`` for a
JWT; ``LEGACY_TOKEN_SCOPES`` and no freshness for the legacy token.

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

from cipherweave_verify.authz import Principal, legacy_scopes
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
    # Set when ok: what ToolAuthorizer needs (scopes, iat, jti).
    principal: Principal | None = None


def parse_authorization(header: str | None) -> tuple[str | None, str | None]:
    """``Authorization`` → (scheme, token). Scheme is ``bearer``/``dpop`` lower-cased."""
    if not isinstance(header, str) or not header.strip():
        return None, None
    scheme, _, value = header.strip().partition(" ")
    scheme = scheme.lower()
    value = value.strip()
    if scheme not in ("bearer", "dpop") or not value or " " in value:
        return None, None
    return scheme, value


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

    def check(
        self,
        presented: str | None,
        *,
        dpop_proof: str | None = None,
        htm: str | None = None,
        htu: str | None = None,
        scheme: str | None = None,
    ) -> AuthDecision:
        """Decide on a presented token.

        ``scheme`` is the Authorization scheme when the caller parsed it
        (``bearer`` or ``dpop``); RFC 9449 §7 pins it: a key-bound token sent
        as ``Bearer`` is refused (``dpop_required``), and the ``DPoP`` scheme
        with an unbound token is refused too. ``None`` skips that check, for
        callers that pass only the token.
        """
        decision = self._decide(presented, dpop_proof, htm, htu, scheme)
        if decision.ok:
            jti = decision.principal.jti if decision.principal else None
            principal = decision.principal
            logger.info(
                "mcp auth method=%s sub=%s jti=%s act=%s dpop=%s",
                decision.method,
                decision.subject or "-",
                jti or "-",
                ",".join(principal.actors) if principal and principal.actors else "-",
                "yes" if principal and principal.dpop_bound else "no",
            )
        else:
            logger.warning("mcp auth rejected reason=%s", decision.reason)
        return decision

    def check_event(self, event: dict) -> AuthDecision:
        """Decide on an API Gateway HTTP API event: Authorization, DPoP, method and URL."""
        from cipherweave_verify.metadata import api_gateway_request

        headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
        method, url, _base = api_gateway_request(event)
        return self.check_header(
            headers.get("authorization"), dpop_proof=headers.get("dpop"), htm=method, htu=url
        )

    def check_header(
        self,
        authorization: str | None,
        *,
        dpop_proof: str | None = None,
        htm: str | None = None,
        htu: str | None = None,
    ) -> AuthDecision:
        """Decide on a raw ``Authorization`` header (``Bearer`` or ``DPoP`` scheme)."""
        scheme, token = parse_authorization(authorization)
        if token is None:
            decision = AuthDecision(False, None, reason="missing")
            logger.warning("mcp auth rejected reason=%s", decision.reason)
            return decision
        return self.check(token, dpop_proof=dpop_proof, htm=htm, htu=htu, scheme=scheme)

    def _decide(
        self,
        presented: str | None,
        dpop_proof: str | None = None,
        htm: str | None = None,
        htu: str | None = None,
        scheme: str | None = None,
    ) -> AuthDecision:
        if not isinstance(presented, str) or not presented:
            return AuthDecision(False, None, reason="missing")

        reason = "invalid_token"
        if looks_like_jwt(presented) and (self._verifier is not None or jwt_configured()):
            try:
                verifier = self._verifier or default_verifier()
                token = verifier.verify(
                    presented, self.audience, dpop_proof=dpop_proof, htm=htm, htu=htu
                )
                if scheme == "bearer" and token.cnf_jkt is not None:
                    return AuthDecision(False, None, reason="dpop_required")
                if scheme == "dpop" and token.cnf_jkt is None:
                    return AuthDecision(False, None, reason="token_not_dpop_bound")
                principal = Principal(
                    method="jwt",
                    subject=token.subject,
                    scopes=token.scopes,
                    issued_at=token.issued_at,
                    expires_at=token.expires_at,
                    jti=token.jti,
                    actors=token.actors,
                    dpop_bound=token.cnf_jkt is not None,
                )
                return AuthDecision(True, "jwt", subject=token.subject, principal=principal)
            except TokenVerificationError as exc:
                if is_jws(presented):
                    return AuthDecision(False, None, reason=exc.reason)
                reason = exc.reason
            except Exception as exc:  # a verifier bug must be a 401, not a 500
                logger.error("cipherweave token check raised type=%s", type(exc).__name__)
                return AuthDecision(False, None, reason="verifier_error")

        if scheme == "dpop":
            # The legacy static token is a bearer secret; it is never DPoP.
            return AuthDecision(False, None, reason=reason)
        if not legacy_accepted():
            return AuthDecision(
                False, None, reason=reason if reason != "invalid_token" else "legacy_disabled"
            )
        if self._legacy_token is None:
            return AuthDecision(False, None, reason=reason)
        expected = self._legacy_token()
        if isinstance(expected, str) and expected and tokens_equal(presented, expected):
            return AuthDecision(
                True, "legacy", principal=Principal("legacy", None, legacy_scopes())
            )
        if not expected:
            # Still compare so the failure path takes the same time.
            tokens_equal(presented, "\x00")
        return AuthDecision(False, None, reason=reason)
