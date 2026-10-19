"""Test helpers: a throwaway local issuer and an in-memory JWKS.

For the test suites of the Weaves that install this package. It mints tokens
in the same shape as CipherWeave's issuer (``tests/test_issuer_e2e.py`` in
CipherWeave holds the two to the same format), signed with a P-256 key that
exists only in the test process. Nothing here talks to AWS.

    issuer = LocalIssuer()
    verifier = Verifier("https://jwks.test/jwks.json", fetcher=issuer.fetcher)
    token = issuer.mint(audience="deviceweave")
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from cipherweave_verify.verify import DEFAULT_ISSUER


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def public_jwk(public_key: ec.EllipticCurvePublicKey) -> dict[str, str]:
    numbers = public_key.public_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": b64url(numbers.x.to_bytes(32, "big")),
        "y": b64url(numbers.y.to_bytes(32, "big")),
    }
    jwk["kid"] = jwk_thumbprint(jwk)
    jwk.update({"use": "sig", "alg": "ES256"})
    return jwk


def jwk_thumbprint(jwk: dict[str, str]) -> str:
    """RFC 7638 thumbprint: SHA-256 over the required members in lexical order."""
    canonical = json.dumps(
        {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]},
        separators=(",", ":"),
        sort_keys=True,
    )
    return b64url(hashlib.sha256(canonical.encode("ascii")).digest())


def encode_jws(
    header: dict[str, Any], claims: dict[str, Any], private_key: ec.EllipticCurvePrivateKey
) -> str:
    signing_input = (
        b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + b64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    der = private_key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return signing_input + "." + b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


class LocalIssuer:
    def __init__(self, issuer: str = DEFAULT_ISSUER, *, lifetime_seconds: int = 900) -> None:
        self.issuer = issuer
        self.lifetime_seconds = lifetime_seconds
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.jwk = public_jwk(self.private_key.public_key())
        self.kid = self.jwk["kid"]
        self.fetch_count = 0

    def jwks(self) -> dict[str, Any]:
        return {"keys": [dict(self.jwk)]}

    def fetcher(self, _url: str) -> dict[str, Any]:
        self.fetch_count += 1
        return self.jwks()

    def claims(
        self,
        *,
        audience: str,
        subject: str = "test-client",
        scopes: tuple[str, ...] = (),
        now: int | None = None,
        **overrides: Any,
    ) -> dict[str, Any]:
        issued = int(time.time()) if now is None else now
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "sub": subject,
            "aud": audience,
            "scope": " ".join(scopes),
            "iat": issued,
            "nbf": issued,
            "exp": issued + self.lifetime_seconds,
            "jti": uuid.uuid4().hex,
        }
        claims.update(overrides)
        return claims

    def mint(
        self,
        *,
        audience: str,
        header_overrides: dict[str, Any] | None = None,
        **claim_kwargs: Any,
    ) -> str:
        header = {"alg": "ES256", "typ": "JWT", "kid": self.kid}
        header.update(header_overrides or {})
        return encode_jws(header, self.claims(audience=audience, **claim_kwargs), self.private_key)
