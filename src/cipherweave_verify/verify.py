"""ES256 JWT verification for CipherWeave-issued access tokens.

What is checked, in order, and why the order matters:

1. Shape: three base64url segments, a JSON header and payload, a bounded size.
2. ``alg`` is exactly ``ES256``. Not "any asymmetric alg", not "whatever the
   key says". ``none`` would skip the signature; ``HS256`` would let an
   attacker sign with the *public* key as an HMAC secret. Both are refused
   before a key is looked up, so neither can reach a code path that might
   accept it.
3. ``kid`` names a key in the cached JWKS (one refresh on a miss).
4. The signature verifies over ``header.payload``.
5. Only then are claims read: ``iss`` equals the configured issuer, ``aud`` is
   exactly this Weave, ``exp``/``nbf``/``iat`` with 60 s skew, and every
   required claim is present with the right type.

Claims are not trusted until step 4 passes, so a forged token can never
produce a "wrong audience" error that leaks what the audience check is.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from cipherweave_verify.dpop import ProofReplayLedger, verify_proof
from cipherweave_verify.errors import (
    DPoPRequiredError,
    ExpiredTokenError,
    InsufficientScopeError,
    InvalidAudienceError,
    InvalidClaimsError,
    InvalidIssuerError,
    InvalidSignatureError,
    MalformedTokenError,
    NotYetValidError,
    UnknownKeyError,
    UnsupportedAlgorithmError,
    VerifierNotConfiguredError,
)
from cipherweave_verify.jwks import Fetcher, JWKSCache, b64url_decode

ALGORITHM = "ES256"
DEFAULT_ISSUER = "urn:cipherweave:prod"
DEFAULT_LEEWAY_SECONDS = 60
# The issuer mints 15-minute tokens. Anything claiming a much longer life was
# not minted by it under its current policy, whatever the signature says.
DEFAULT_MAX_LIFETIME_SECONDS = 3600
MAX_TOKEN_LENGTH = 8192
# Order of the P-256 group: valid ECDSA r and s are in [1, n-1].
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

ENV_JWKS_URL = "CIPHERWEAVE_JWKS_URL"
ENV_JWKS = "CIPHERWEAVE_JWKS"
ENV_ISSUER = "CIPHERWEAVE_ISSUER"

_ALLOWED_TYP = frozenset({"jwt", "at+jwt"})
# A delegation chain longer than this is refused. The issuer caps exchange at
# three hops; one more here is headroom, not an invitation.
MAX_ACTOR_DEPTH = 4


@dataclass(frozen=True)
class VerifiedToken:
    subject: str
    audience: str
    scopes: frozenset[str]
    issued_at: int
    expires_at: int
    jti: str
    kid: str
    claims: dict[str, Any] = field(repr=False)
    # RFC 8693 delegation: the clients acting on the subject's behalf, the
    # current actor first. Empty for a token the subject obtained itself.
    actors: tuple[str, ...] = ()
    # RFC 9449: the thumbprint of the key this token is bound to, or None for
    # a plain bearer token. When set, verify() has already checked the proof.
    cnf_jkt: str | None = None

    def has_scopes(self, required: Iterable[str]) -> bool:
        return set(required) <= self.scopes

    @property
    def delegated(self) -> bool:
        return bool(self.actors)

    @property
    def actor(self) -> str | None:
        """The client presenting a delegated token, or None for the subject's own token."""
        return self.actors[0] if self.actors else None


def actor_chain(claims: dict[str, Any]) -> tuple[str, ...]:
    """The ``act`` chain, current actor first (RFC 8693 §4.1). Malformed is refused."""
    chain: list[str] = []
    act = claims.get("act")
    while act is not None:
        if not isinstance(act, dict):
            raise InvalidClaimsError("act must be an object")
        sub = act.get("sub")
        if not isinstance(sub, str) or not sub:
            raise InvalidClaimsError("act.sub missing")
        chain.append(sub)
        if len(chain) > MAX_ACTOR_DEPTH:
            raise InvalidClaimsError("delegation chain too deep")
        act = act.get("act")
    return tuple(chain)


def confirmation_jkt(claims: dict[str, Any]) -> str | None:
    """``cnf.jkt`` if the token is key-bound. Any other ``cnf`` shape is refused."""
    cnf = claims.get("cnf")
    if cnf is None:
        return None
    if not isinstance(cnf, dict):
        raise InvalidClaimsError("cnf must be an object")
    jkt = cnf.get("jkt")
    if not isinstance(jkt, str) or not jkt or set(cnf) - {"jkt"}:
        # x5t#S256 (mTLS) and jwk are not supported: a binding this verifier
        # cannot check must not be read as no binding at all.
        raise InvalidClaimsError("unsupported cnf")
    return jkt


def looks_like_jwt(token: str | None) -> bool:
    """Cheap shape test so a caller can route legacy opaque tokens elsewhere."""
    return isinstance(token, str) and token.count(".") == 2 and len(token) <= MAX_TOKEN_LENGTH


def _decode_json_segment(segment: str, what: str) -> dict[str, Any]:
    try:
        raw = b64url_decode(segment)
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise MalformedTokenError(f"{what} is not base64url JSON") from exc
    if not isinstance(value, dict):
        raise MalformedTokenError(f"{what} is not a JSON object")
    return value


def _int_claim(claims: dict[str, Any], name: str, *, required: bool) -> int | None:
    value = claims.get(name)
    if value is None:
        if required:
            raise InvalidClaimsError(f"missing claim {name}")
        return None
    # bool is an int subclass; True is not a timestamp.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidClaimsError(f"claim {name} is not a number")
    return int(value)


def _str_claim(claims: dict[str, Any], name: str, *, allow_empty: bool = False) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or (not value and not allow_empty):
        raise InvalidClaimsError(f"missing claim {name}")
    return value


def parse_static_jwks(value: str | dict[str, Any] | None) -> dict[str, Any] | None:
    """A JWKS given at deploy time: a dict, JSON text, or base64 of the JSON.

    Base64 because the value usually travels as a CloudFormation parameter,
    and SAM CLI's override parser cannot carry the quotes and commas of raw
    JSON. A value that does not parse fails closed (VerifierNotConfiguredError)
    rather than being ignored: a typo must not quietly mean "no keys".
    """
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        if not text.startswith("{"):
            text = base64.b64decode(text, validate=True).decode("utf-8")
        document = json.loads(text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise VerifierNotConfiguredError("static JWKS does not parse") from exc
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise VerifierNotConfiguredError("static JWKS has no keys array")
    return document


class Verifier:
    """Verify CipherWeave tokens.

    Keys come from ``static_jwks`` (given at deploy time, no network at all),
    from ``jwks_url`` (fetched and cached), or both — then the static set is
    tried first and the URL only for a kid it does not contain, e.g. a key
    activated before this service was redeployed.
    """

    def __init__(
        self,
        jwks_url: str | None = None,
        issuer: str = DEFAULT_ISSUER,
        *,
        static_jwks: str | dict[str, Any] | None = None,
        fetcher: Fetcher | None = None,
        leeway_seconds: int = DEFAULT_LEEWAY_SECONDS,
        max_lifetime_seconds: int = DEFAULT_MAX_LIFETIME_SECONDS,
        jwks_ttl_seconds: float = 600.0,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        document = parse_static_jwks(static_jwks)
        if not jwks_url and document is None:
            raise VerifierNotConfiguredError("no JWKS configured")
        if not issuer:
            raise VerifierNotConfiguredError("no issuer configured")
        self.issuer = issuer
        self._leeway = leeway_seconds
        self._max_lifetime = max_lifetime_seconds
        self._clock = clock
        self.static_keys = JWKSCache.parse(document) if document is not None else {}
        self.jwks = (
            JWKSCache(jwks_url, fetcher=fetcher, ttl_seconds=jwks_ttl_seconds, clock=monotonic)
            if jwks_url
            else None
        )

    def verify(
        self,
        token: str,
        expected_audience: str,
        *,
        required_scopes: Iterable[str] = (),
        dpop_proof: str | None = None,
        htm: str | None = None,
        htu: str | None = None,
        dpop_replay: ProofReplayLedger | None = None,
    ) -> VerifiedToken:
        """Verify ``token`` for ``expected_audience``.

        A token bound to a key (``cnf.jkt``) is accepted only with a DPoP
        proof for this request: pass the ``DPoP`` header as ``dpop_proof`` and
        the request method and absolute URL as ``htm``/``htu``. A proof sent
        with an unbound token is ignored -- the token is a bearer token
        either way, and refusing it would break clients that always sign.
        """
        verified = self._verify_signed(token, expected_audience, required_scopes)
        if verified.cnf_jkt is not None:
            if not dpop_proof:
                raise DPoPRequiredError("token is DPoP-bound; send a DPoP proof")
            if not htm or not htu:
                raise VerifierNotConfiguredError("DPoP-bound token needs htm and htu")
            verify_proof(
                dpop_proof,
                htm=htm,
                htu=htu,
                access_token=token,
                expected_jkt=verified.cnf_jkt,
                replay_ledger=dpop_replay,
                clock=self._clock,
            )
        return verified

    def _verify_signed(
        self, token: str, expected_audience: str, required_scopes: Iterable[str]
    ) -> VerifiedToken:
        """Signature and claims; no DPoP binding check."""
        if not expected_audience:
            raise VerifierNotConfiguredError("no expected audience")
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
            raise MalformedTokenError("token missing or too long")
        parts = token.split(".")
        if len(parts) != 3 or not all(parts):
            raise MalformedTokenError("token is not a compact JWS")
        header_b64, payload_b64, signature_b64 = parts

        header = _decode_json_segment(header_b64, "header")
        alg = header.get("alg")
        if alg != ALGORITHM:
            raise UnsupportedAlgorithmError("only ES256 is accepted")
        if "crit" in header:
            raise MalformedTokenError("crit header is not supported")
        typ = header.get("typ")
        if typ is not None and (not isinstance(typ, str) or typ.lower() not in _ALLOWED_TYP):
            raise MalformedTokenError("unexpected typ")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > 256:
            raise MalformedTokenError("kid missing")

        try:
            signature = b64url_decode(signature_b64)
        except ValueError as exc:
            raise MalformedTokenError("signature is not base64url") from exc
        if len(signature) != 64:
            # JWS ES256 is raw r||s, 32 bytes each (RFC 7518 §3.4). A DER
            # signature here means the signer skipped the conversion.
            raise InvalidSignatureError("ES256 signature must be 64 bytes")

        key = self.static_keys.get(kid)
        if key is None and self.jwks is not None:
            key = self.jwks.get(kid)
        if key is None:
            raise UnknownKeyError("kid not in JWKS")

        r = int.from_bytes(signature[:32], "big")
        s = int.from_bytes(signature[32:], "big")
        if not (0 < r < _P256_ORDER and 0 < s < _P256_ORDER):
            # Out of range can never verify, and encode_dss_signature raises
            # ValueError on zero -- which would surface as a 500, not a 401.
            raise InvalidSignatureError("signature out of range")
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        try:
            key.verify(encode_dss_signature(r, s), signing_input, ec.ECDSA(hashes.SHA256()))
        except (InvalidSignature, ValueError) as exc:
            raise InvalidSignatureError("signature does not verify") from exc

        claims = _decode_json_segment(payload_b64, "payload")
        return self._check_claims(claims, kid, expected_audience, required_scopes)

    def verify_subject_token(self, token: str, expected_audience: str) -> VerifiedToken:
        """Verify a token presented to the *authorization server* as an RFC 8693 subject token.

        Everything ``verify`` checks except the DPoP proof. A key-bound token
        reaches the issuer in the hands of the resource server it was sent
        to, which checked the client's proof when it arrived and cannot
        produce the client's key now. The binding protected that hop; the
        exchange is authenticated separately, by the actor's own credential,
        and the audience check still pins the token to that actor.

        A resource server must never call this: it would accept a stolen
        bound token without its key. Use ``verify``.
        """
        return self._verify_signed(token, expected_audience, ())

    def _check_claims(
        self,
        claims: dict[str, Any],
        kid: str,
        expected_audience: str,
        required_scopes: Iterable[str],
    ) -> VerifiedToken:
        subject = _str_claim(claims, "sub")
        jti = _str_claim(claims, "jti")
        issuer = _str_claim(claims, "iss")
        scope = _str_claim(claims, "scope", allow_empty=True)
        audience = claims.get("aud")
        issued_at = _int_claim(claims, "iat", required=True)
        expires_at = _int_claim(claims, "exp", required=True)
        not_before = _int_claim(claims, "nbf", required=False)
        assert issued_at is not None and expires_at is not None

        if issuer != self.issuer:
            raise InvalidIssuerError("issuer mismatch")
        # Exactly one audience, as a string. A list — even ["thisweave"] — is
        # refused: the issuer never mints one, so it did not mint this.
        if not isinstance(audience, str) or not audience:
            raise InvalidClaimsError("aud must be a single string")
        if audience != expected_audience:
            raise InvalidAudienceError("token is for a different audience")

        now = self._clock()
        if now >= expires_at + self._leeway:
            raise ExpiredTokenError("token expired")
        if not_before is not None and now < not_before - self._leeway:
            raise NotYetValidError("token not yet valid")
        if issued_at > now + self._leeway:
            raise NotYetValidError("token issued in the future")
        if expires_at <= issued_at or expires_at - issued_at > self._max_lifetime:
            raise InvalidClaimsError("token lifetime out of range")

        actors = actor_chain(claims)
        cnf_jkt = confirmation_jkt(claims)

        scopes = frozenset(scope.split())
        missing = set(required_scopes) - scopes
        if missing:
            raise InsufficientScopeError("token lacks a required scope")

        return VerifiedToken(
            subject=subject,
            audience=audience,
            scopes=scopes,
            issued_at=issued_at,
            expires_at=expires_at,
            jti=jti,
            kid=kid,
            claims=claims,
            actors=actors,
            cnf_jkt=cnf_jkt,
        )


# ---------------------------------------------------------------------------
# Module-level convenience: one Verifier per process, configured from env.
# The JWKS cache lives on it, so warm Lambda invocations do not refetch.
# ---------------------------------------------------------------------------
_default: Verifier | None = None
_default_config: tuple[str, str, str] | None = None
_default_lock = threading.Lock()


def default_verifier() -> Verifier:
    global _default, _default_config
    url = (os.environ.get(ENV_JWKS_URL) or "").strip()
    static = (os.environ.get(ENV_JWKS) or "").strip()
    issuer = (os.environ.get(ENV_ISSUER) or "").strip() or DEFAULT_ISSUER
    config = (url, static, issuer)
    with _default_lock:
        if _default is None or _default_config != config:
            _default = Verifier(url or None, issuer, static_jwks=static or None)
            _default_config = config
        return _default


def reset_default_verifier() -> None:
    global _default, _default_config
    with _default_lock:
        _default = None
        _default_config = None


def verify(
    token: str,
    expected_audience: str,
    *,
    required_scopes: Iterable[str] = (),
    dpop_proof: str | None = None,
    htm: str | None = None,
    htu: str | None = None,
) -> VerifiedToken:
    """Verify ``token`` for ``expected_audience`` using the env-configured verifier.

    Reads ``CIPHERWEAVE_JWKS`` (deploy-time JWKS, base64 or JSON),
    ``CIPHERWEAVE_JWKS_URL`` (fetched; fallback for unknown kids) and
    ``CIPHERWEAVE_ISSUER``. At least one of the first two must be set. Raises a
    ``TokenVerificationError`` subclass (``status_code == 401``) on any failure.
    """
    return default_verifier().verify(
        token,
        expected_audience,
        required_scopes=required_scopes,
        dpop_proof=dpop_proof,
        htm=htm,
        htu=htu,
    )
