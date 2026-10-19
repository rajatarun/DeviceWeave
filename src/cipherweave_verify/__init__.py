"""Verify CipherWeave-issued access tokens locally.

    from cipherweave_verify import verify, TokenVerificationError

    try:
        token = verify(bearer, "deviceweave")
    except TokenVerificationError:
        return 401

Configuration comes from ``CIPHERWEAVE_JWKS`` (the JWKS itself, given at
deploy time, so no network call at all), ``CIPHERWEAVE_JWKS_URL`` (fetched,
the fallback for a kid the deploy-time set lacks) and ``CIPHERWEAVE_ISSUER``
(default ``urn:cipherweave:prod``), or construct a ``Verifier`` directly.

During the migration from static bearer tokens, ``BearerAuth`` combines this
check with the old one behind ``ACCEPT_LEGACY_TOKEN``.
"""

from cipherweave_verify.bearer import AuthDecision, BearerAuth, legacy_accepted
from cipherweave_verify.errors import (
    ExpiredTokenError,
    InsufficientScopeError,
    InvalidAudienceError,
    InvalidClaimsError,
    InvalidIssuerError,
    InvalidSignatureError,
    JWKSUnavailableError,
    MalformedTokenError,
    NotYetValidError,
    TokenVerificationError,
    UnknownKeyError,
    UnsupportedAlgorithmError,
    VerifierNotConfiguredError,
)
from cipherweave_verify.verify import (
    DEFAULT_ISSUER,
    ENV_ISSUER,
    ENV_JWKS,
    ENV_JWKS_URL,
    VerifiedToken,
    Verifier,
    default_verifier,
    looks_like_jwt,
    parse_static_jwks,
    reset_default_verifier,
    verify,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_ISSUER",
    "ENV_ISSUER",
    "ENV_JWKS",
    "ENV_JWKS_URL",
    "AuthDecision",
    "BearerAuth",
    "ExpiredTokenError",
    "InsufficientScopeError",
    "InvalidAudienceError",
    "InvalidClaimsError",
    "InvalidIssuerError",
    "InvalidSignatureError",
    "JWKSUnavailableError",
    "MalformedTokenError",
    "NotYetValidError",
    "TokenVerificationError",
    "UnknownKeyError",
    "UnsupportedAlgorithmError",
    "VerifiedToken",
    "Verifier",
    "VerifierNotConfiguredError",
    "default_verifier",
    "legacy_accepted",
    "looks_like_jwt",
    "parse_static_jwks",
    "reset_default_verifier",
    "verify",
]
