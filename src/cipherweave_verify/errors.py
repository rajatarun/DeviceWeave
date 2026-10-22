"""Verification errors. Every one of them means HTTP 401.

``reason`` is a short machine-readable code that is safe to log. Messages
never contain the token or any part of it: a log line that quotes a rejected
token hands a valid one to whoever reads the log the first time it is wrong
by only a clock skew.
"""

from __future__ import annotations


class TokenVerificationError(Exception):
    """The presented token is not acceptable. Answer 401."""

    status_code = 401
    reason = "invalid_token"

    def __init__(self, message: str = "invalid token", *, reason: str | None = None) -> None:
        super().__init__(message)
        if reason is not None:
            self.reason = reason


class MalformedTokenError(TokenVerificationError):
    reason = "malformed"


class UnsupportedAlgorithmError(TokenVerificationError):
    """alg is anything but ES256 — including ``none`` and HS256."""

    reason = "unsupported_alg"


class UnknownKeyError(TokenVerificationError):
    """kid is not in the JWKS, even after a refresh."""

    reason = "unknown_kid"


class InvalidSignatureError(TokenVerificationError):
    reason = "bad_signature"


class InvalidClaimsError(TokenVerificationError):
    """A required claim is missing or has the wrong type."""

    reason = "invalid_claims"


class InvalidIssuerError(TokenVerificationError):
    reason = "wrong_issuer"


class InvalidAudienceError(TokenVerificationError):
    """The token was issued for a different Weave."""

    reason = "wrong_audience"


class ExpiredTokenError(TokenVerificationError):
    reason = "expired"


class NotYetValidError(TokenVerificationError):
    reason = "not_yet_valid"


class InsufficientScopeError(TokenVerificationError):
    reason = "insufficient_scope"


class JWKSUnavailableError(TokenVerificationError):
    """The JWKS could not be fetched and no usable cached copy exists.

    Still a 401, not a pass: a verifier that cannot see the keys cannot
    say the token is good.
    """

    reason = "jwks_unavailable"


class VerifierNotConfiguredError(TokenVerificationError):
    """No JWKS URL or issuer configured. Fail closed."""

    reason = "not_configured"


class InvalidDPoPProofError(TokenVerificationError):
    """A DPoP proof is missing a check it must pass (RFC 9449 §4.3)."""

    reason = "invalid_dpop_proof"


class DPoPRequiredError(TokenVerificationError):
    """The token is bound to a key (``cnf.jkt``) and the request carries no proof.

    Answered as a 401 with a ``DPoP`` challenge: without the proof, the
    request is indistinguishable from one made with a stolen token.
    """

    reason = "dpop_required"
