"""Cached JWKS: the only network call the verifier makes.

The set is fetched once and kept for ``ttl_seconds`` (10 minutes). A token
whose ``kid`` is not in the cached set triggers one refresh, so a rotated key
is picked up on the first request that uses it rather than up to ten minutes
later. That refresh is rate-limited (``min_refresh_interval``): without the
limit, anyone could send tokens with random kids and turn every request into
a JWKS fetch.

If a refresh fails, the previous set keeps serving for up to
``stale_if_error_seconds``. Every signature is still checked against it, so a
stale set can only ever reject a token signed by a newer key, never accept a
forged one.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec

from cipherweave_verify.errors import JWKSUnavailableError

logger = logging.getLogger(__name__)

Fetcher = Callable[[str], dict[str, Any]]

_MAX_JWKS_BYTES = 64 * 1024
_FETCH_TIMEOUT_SECONDS = 3.0


def b64url_decode(segment: str) -> bytes:
    """Strict base64url (no padding, URL alphabet only)."""
    import base64
    import binascii

    if not isinstance(segment, str) or not segment:
        raise ValueError("empty segment")
    if "=" in segment or "+" in segment or "/" in segment:
        raise ValueError("not base64url")
    padded = segment + "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError("not base64url") from exc


def http_fetcher(url: str) -> dict[str, Any]:
    """GET the JWKS over HTTPS with a short timeout and a size cap."""
    if not url.startswith("https://"):
        raise JWKSUnavailableError("JWKS URL must be https")
    request = urllib.request.Request(  # noqa: S310 — scheme checked above
        url, headers={"Accept": "application/json", "User-Agent": "cipherweave-verify/0.1"}
    )
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
        body = response.read(_MAX_JWKS_BYTES + 1)
    if len(body) > _MAX_JWKS_BYTES:
        raise JWKSUnavailableError("JWKS response too large")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise JWKSUnavailableError("JWKS is not an object")
    return data


def public_key_from_jwk(jwk: dict[str, Any]) -> ec.EllipticCurvePublicKey | None:
    """Return the P-256 key for an ES256 signing JWK, or None if it is not one.

    Building the key from its numbers validates that the point is on the
    curve, so a crafted JWK cannot smuggle an invalid point in.
    """
    if not isinstance(jwk, dict):
        return None
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        return None
    if jwk.get("use", "sig") != "sig" or jwk.get("alg", "ES256") != "ES256":
        return None
    if "d" in jwk:
        # A private key has no business in a published set. Refuse the entry
        # rather than quietly use its public half.
        return None
    try:
        x = b64url_decode(jwk["x"])
        y = b64url_decode(jwk["y"])
    except (KeyError, ValueError):
        return None
    if len(x) != 32 or len(y) != 32:
        return None
    try:
        numbers = ec.EllipticCurvePublicNumbers(
            int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()
        )
        return numbers.public_key()
    except ValueError:
        return None


class JWKSCache:
    def __init__(
        self,
        url: str,
        *,
        fetcher: Fetcher | None = None,
        ttl_seconds: float = 600.0,
        min_refresh_interval: float = 30.0,
        stale_if_error_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._fetcher = fetcher or http_fetcher
        self._ttl = ttl_seconds
        self._min_refresh = min_refresh_interval
        self._stale_if_error = stale_if_error_seconds
        self._clock = clock
        self._keys: dict[str, ec.EllipticCurvePublicKey] = {}
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None
        self._lock = threading.Lock()
        self.fetch_count = 0

    def get(self, kid: str) -> ec.EllipticCurvePublicKey | None:
        """Return the key for ``kid``, refreshing at most once if it is unknown.

        Raises JWKSUnavailableError when there is no set young enough to use.
        """
        with self._lock:
            now = self._clock()
            expired = self._fetched_at is None or now - self._fetched_at >= self._ttl
            if expired and self._may_attempt(now):
                self._refresh(now)
            self._require_usable(now)
            key = self._keys.get(kid)
            if key is None and self._may_attempt(now):
                # Unknown kid: the issuer may have rotated. One refresh, rate-limited.
                self._refresh(now)
                self._require_usable(now)
                key = self._keys.get(kid)
            return key

    def _may_attempt(self, now: float) -> bool:
        return self._last_attempt is None or now - self._last_attempt >= self._min_refresh

    def _require_usable(self, now: float) -> None:
        if self._fetched_at is None:
            raise JWKSUnavailableError("JWKS unavailable")
        if now - self._fetched_at >= self._ttl + self._stale_if_error:
            self._keys = {}
            raise JWKSUnavailableError("JWKS unavailable")

    def _refresh(self, now: float) -> None:
        """Fetch the set. A failure keeps the previous set; the caller decides."""
        self._last_attempt = now
        self.fetch_count += 1
        try:
            keys = self.parse(self._fetcher(self._url))
        except Exception as exc:
            logger.warning("jwks refresh failed type=%s", type(exc).__name__)
            return
        self._keys = keys
        self._fetched_at = now

    @staticmethod
    def parse(document: dict[str, Any]) -> dict[str, ec.EllipticCurvePublicKey]:
        entries = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(entries, list):
            raise JWKSUnavailableError("JWKS has no keys array")
        keys: dict[str, ec.EllipticCurvePublicKey] = {}
        for entry in entries:
            kid = entry.get("kid") if isinstance(entry, dict) else None
            if not isinstance(kid, str) or not kid:
                continue
            key = public_key_from_jwk(entry)
            if key is not None:
                keys[kid] = key
        return keys
