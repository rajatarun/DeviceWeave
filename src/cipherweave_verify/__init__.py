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

Per-tool scopes (``read``/``execute``/``destructive``), step-up freshness,
single-use jti and the delegation rule for destructive calls are
``ToolAuthorizer`` in ``authz``.

Sender-constrained tokens (RFC 9449 DPoP) are checked by ``verify`` when the
token carries ``cnf.jkt``; ``dpop`` builds and verifies proofs. Delegated
tokens (RFC 8693 token exchange) expose their ``act`` chain as
``VerifiedToken.actors``. ``metadata`` builds the RFC 9728 protected-resource
document and RFC 6750 / 9449 ``WWW-Authenticate`` challenges.

During the migration from static bearer tokens, ``BearerAuth`` combines this
check with the old one behind ``ACCEPT_LEGACY_TOKEN``.
"""

from cipherweave_verify.authz import (
    DESTRUCTIVE,
    EXECUTE,
    READ,
    SCOPE_DESTRUCTIVE,
    SCOPE_EXECUTE,
    SCOPE_READ,
    AuthorizationError,
    DelegationNotAllowedError,
    DynamoJtiLedger,
    LedgerUnavailableError,
    MemoryJtiLedger,
    Principal,
    ReplayedTokenError,
    ScopeRequiredError,
    StepUpRequiredError,
    ToolAuthorizer,
    ToolPolicy,
)
from cipherweave_verify.bearer import (
    AuthDecision,
    BearerAuth,
    legacy_accepted,
    parse_authorization,
)
from cipherweave_verify.dpop import (
    DPoPKey,
    DPoPProof,
    access_token_hash,
    jwk_thumbprint,
    verify_proof,
)
from cipherweave_verify.errors import (
    DPoPRequiredError,
    ExpiredTokenError,
    InsufficientScopeError,
    InvalidAudienceError,
    InvalidClaimsError,
    InvalidDPoPProofError,
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
from cipherweave_verify.metadata import (
    WELL_KNOWN_PATH,
    api_gateway_request,
    default_authorization_server,
    protected_resource_metadata,
    resource_metadata_url,
    www_authenticate,
)
from cipherweave_verify.verify import (
    DEFAULT_ISSUER,
    ENV_ISSUER,
    ENV_JWKS,
    ENV_JWKS_URL,
    VerifiedToken,
    Verifier,
    actor_chain,
    confirmation_jkt,
    default_verifier,
    looks_like_jwt,
    parse_static_jwks,
    reset_default_verifier,
    verify,
)

__version__ = "0.3.0"

__all__ = [
    "DEFAULT_ISSUER",
    "DESTRUCTIVE",
    "ENV_ISSUER",
    "ENV_JWKS",
    "ENV_JWKS_URL",
    "EXECUTE",
    "READ",
    "SCOPE_DESTRUCTIVE",
    "SCOPE_EXECUTE",
    "SCOPE_READ",
    "WELL_KNOWN_PATH",
    "AuthDecision",
    "AuthorizationError",
    "BearerAuth",
    "DPoPKey",
    "DPoPProof",
    "DPoPRequiredError",
    "DelegationNotAllowedError",
    "DynamoJtiLedger",
    "ExpiredTokenError",
    "InsufficientScopeError",
    "InvalidAudienceError",
    "InvalidClaimsError",
    "InvalidDPoPProofError",
    "InvalidIssuerError",
    "InvalidSignatureError",
    "JWKSUnavailableError",
    "LedgerUnavailableError",
    "MalformedTokenError",
    "MemoryJtiLedger",
    "NotYetValidError",
    "Principal",
    "ReplayedTokenError",
    "ScopeRequiredError",
    "StepUpRequiredError",
    "TokenVerificationError",
    "ToolAuthorizer",
    "ToolPolicy",
    "UnknownKeyError",
    "UnsupportedAlgorithmError",
    "VerifiedToken",
    "Verifier",
    "VerifierNotConfiguredError",
    "access_token_hash",
    "actor_chain",
    "api_gateway_request",
    "confirmation_jkt",
    "default_authorization_server",
    "default_verifier",
    "jwk_thumbprint",
    "legacy_accepted",
    "looks_like_jwt",
    "parse_authorization",
    "parse_static_jwks",
    "protected_resource_metadata",
    "reset_default_verifier",
    "resource_metadata_url",
    "verify",
    "verify_proof",
    "www_authenticate",
]
