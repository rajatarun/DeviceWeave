"""DPoP proof verification (RFC 9449): a token that only its holder can use.

A bearer token works for whoever holds it. A transcript, a log line, a proxy
that keeps headers, an agent talked into echoing its environment: each hands
a working credential to someone else for the rest of its fifteen minutes.

A DPoP-bound token carries ``cnf.jkt``, the RFC 7638 thumbprint of a key the
client generated and never sends. Every request then carries a fresh proof,
a small JWS signed with that key over the method, the URL, the time and a
hash of the access token. A stolen token without the private key is useless,
and a stolen proof is good for one request to one URL for about a minute.

What is checked, in order:

1. Shape: compact JWS, bounded size, ``typ`` exactly ``dpop+jwt``.
2. ``alg`` is ES256. Same pin as the access token, for the same reasons.
3. ``jwk`` in the header is a public P-256 key (a ``d`` member is refused --
   a client that publishes its private key in every request has no binding).
4. The signature verifies under that ``jwk``.
5. ``htm`` equals the request method, ``htu`` equals the request URL without
   query or fragment (§4.3, compared after normalising scheme and host case).
6. ``iat`` within ``max_age_seconds`` in the past (and ``leeway`` in the
   future); ``jti`` present.
7. ``ath`` is the base64url SHA-256 of the access token, when one is bound.
8. The key's thumbprint equals the token's ``cnf.jkt``.
9. ``jti`` is spent in the replay ledger, if one is given.

Every failure is ``InvalidDPoPProofError`` (401, ``reason="invalid_dpop_proof"``);
the message names the check, never the proof.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from cipherweave_verify.errors import InvalidDPoPProofError
from cipherweave_verify.jwks import b64url_decode, public_key_from_jwk

DPOP_TYP = "dpop+jwt"
ALGORITHM = "ES256"
MAX_PROOF_LENGTH = 4096
DEFAULT_MAX_AGE_SECONDS = 60
DEFAULT_LEEWAY_SECONDS = 5
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


class ProofReplayLedger(Protocol):
    def claim(self, jti: str, expires_at: int) -> bool:
        """Record ``jti``; False if it was already recorded. Raise if unknowable."""


def _b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwk_thumbprint(jwk: dict[str, Any]) -> str:
    """RFC 7638 SHA-256 thumbprint of an EC public JWK (only the required members)."""
    try:
        canonical = json.dumps(
            {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]},
            separators=(",", ":"),
            sort_keys=True,
        )
    except (KeyError, TypeError) as exc:
        raise InvalidDPoPProofError("jwk lacks EC members") from exc
    return _b64url(hashlib.sha256(canonical.encode("ascii")).digest())


def access_token_hash(access_token: str) -> str:
    """The ``ath`` claim: base64url(SHA-256(ASCII(access_token)))."""
    return _b64url(hashlib.sha256(access_token.encode("ascii")).digest())


def normalize_htu(url: str) -> str:
    """Scheme and host lower-cased, default port dropped, query and fragment removed."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise InvalidDPoPProofError("htu is not a URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http") or not parts.hostname:
        raise InvalidDPoPProofError("htu must be an absolute http(s) URL")
    host = parts.hostname.lower()
    port = parts.port
    if port is not None and not (
        (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    ):
        host = f"{host}:{port}"
    return f"{scheme}://{host}{parts.path or '/'}"


@dataclass(frozen=True)
class DPoPProof:
    jkt: str
    jti: str
    issued_at: int
    htm: str
    htu: str


def _segment(segment: str, what: str) -> dict[str, Any]:
    try:
        value = json.loads(b64url_decode(segment))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidDPoPProofError(f"{what} is not base64url JSON") from exc
    if not isinstance(value, dict):
        raise InvalidDPoPProofError(f"{what} is not a JSON object")
    return value


def verify_proof(
    proof: str | None,
    *,
    htm: str,
    htu: str,
    access_token: str | None = None,
    expected_jkt: str | None = None,
    replay_ledger: ProofReplayLedger | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    leeway_seconds: int = DEFAULT_LEEWAY_SECONDS,
    clock: Callable[[], float] = time.time,
) -> DPoPProof:
    """Verify one DPoP proof for one request. Raise ``InvalidDPoPProofError`` otherwise."""
    if not isinstance(proof, str) or not proof or len(proof) > MAX_PROOF_LENGTH:
        raise InvalidDPoPProofError("DPoP proof missing or too long")
    if "," in proof:
        # Two DPoP headers joined by a proxy. RFC 9449 §4.3 check 1: exactly one.
        raise InvalidDPoPProofError("more than one DPoP proof")
    parts = proof.split(".")
    if len(parts) != 3 or not all(parts):
        raise InvalidDPoPProofError("DPoP proof is not a compact JWS")
    header_b64, payload_b64, signature_b64 = parts

    header = _segment(header_b64, "proof header")
    if header.get("typ") != DPOP_TYP:
        raise InvalidDPoPProofError("typ must be dpop+jwt")
    if header.get("alg") != ALGORITHM:
        raise InvalidDPoPProofError("only ES256 proofs are accepted")
    if "crit" in header:
        raise InvalidDPoPProofError("crit header is not supported")
    jwk = header.get("jwk")
    if not isinstance(jwk, dict) or "d" in jwk:
        raise InvalidDPoPProofError("jwk must be a public key")
    key = public_key_from_jwk({k: v for k, v in jwk.items() if k not in ("use", "alg", "kid")})
    if key is None:
        raise InvalidDPoPProofError("jwk is not a P-256 public key")

    try:
        signature = b64url_decode(signature_b64)
    except ValueError as exc:
        raise InvalidDPoPProofError("signature is not base64url") from exc
    if len(signature) != 64:
        raise InvalidDPoPProofError("ES256 signature must be 64 bytes")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not (0 < r < _P256_ORDER and 0 < s < _P256_ORDER):
        raise InvalidDPoPProofError("signature out of range")
    try:
        key.verify(
            encode_dss_signature(r, s),
            f"{header_b64}.{payload_b64}".encode("ascii"),
            ec.ECDSA(hashes.SHA256()),
        )
    except (InvalidSignature, ValueError) as exc:
        raise InvalidDPoPProofError("proof signature does not verify") from exc

    claims = _segment(payload_b64, "proof payload")
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti or len(jti) > 256:
        raise InvalidDPoPProofError("jti missing")
    claim_htm = claims.get("htm")
    if not isinstance(claim_htm, str) or claim_htm != htm.upper():
        raise InvalidDPoPProofError("htm does not match the request method")
    claim_htu = claims.get("htu")
    if not isinstance(claim_htu, str) or normalize_htu(claim_htu) != normalize_htu(htu):
        raise InvalidDPoPProofError("htu does not match the request URL")
    iat = claims.get("iat")
    if isinstance(iat, bool) or not isinstance(iat, (int, float)):
        raise InvalidDPoPProofError("iat missing")
    now = clock()
    if iat > now + leeway_seconds:
        raise InvalidDPoPProofError("proof issued in the future")
    if now - iat > max_age_seconds:
        raise InvalidDPoPProofError("proof too old")

    if access_token is not None:
        ath = claims.get("ath")
        if not isinstance(ath, str) or ath != access_token_hash(access_token):
            raise InvalidDPoPProofError("ath does not match the access token")

    jkt = jwk_thumbprint(jwk)
    if expected_jkt is not None and jkt != expected_jkt:
        raise InvalidDPoPProofError("proof key does not match the token's cnf.jkt")

    if replay_ledger is not None:
        try:
            fresh = replay_ledger.claim(f"dpop#{jkt}#{jti}", int(iat) + max_age_seconds)
        except Exception as exc:
            raise InvalidDPoPProofError("DPoP replay ledger unavailable") from exc
        if not fresh:
            raise InvalidDPoPProofError("DPoP proof replayed")

    return DPoPProof(jkt=jkt, jti=jti, issued_at=int(iat), htm=claim_htm, htu=claim_htu)


class DPoPKey:
    """A client's DPoP key and proof builder. For callers and tests.

    Weaves calling Weaves use this to sign their requests; the private key
    lives only in the caller's process.
    """

    def __init__(self, private_key: ec.EllipticCurvePrivateKey | None = None) -> None:
        self._key = private_key or ec.generate_private_key(ec.SECP256R1())
        numbers = self._key.public_key().public_numbers()
        self.public_jwk = {
            "kty": "EC",
            "crv": "P-256",
            "x": _b64url(numbers.x.to_bytes(32, "big")),
            "y": _b64url(numbers.y.to_bytes(32, "big")),
        }
        self.jkt = jwk_thumbprint(self.public_jwk)

    def proof(
        self,
        htm: str,
        htu: str,
        *,
        access_token: str | None = None,
        iat: int | None = None,
        jti: str | None = None,
        nonce: str | None = None,
    ) -> str:
        import uuid

        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        header = {"typ": DPOP_TYP, "alg": ALGORITHM, "jwk": self.public_jwk}
        claims: dict[str, Any] = {
            "jti": jti or uuid.uuid4().hex,
            "htm": htm.upper(),
            "htu": htu,
            "iat": int(time.time()) if iat is None else iat,
        }
        if access_token is not None:
            claims["ath"] = access_token_hash(access_token)
        if nonce is not None:
            claims["nonce"] = nonce
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64url(json.dumps(claims, separators=(",", ":")).encode())
        )
        r, s = decode_dss_signature(
            self._key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
        )
        return signing_input + "." + _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
