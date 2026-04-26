"""
Ring discovery provider — cloud-based device enumeration.

Uses the Ring REST API to list every device registered to the account.

Credentials loaded from Secrets Manager (RING_SECRET_ARN):
    {"email": "user@example.com", "password": "secret", "hardware_id": "<uuid>"}

For 2FA-enabled accounts, include a pre-obtained refresh_token:
    {"email": "...", "password": "...", "hardware_id": "...", "refresh_token": "..."}
"""

import hashlib
import json
import logging
import os
import uuid as _uuid_mod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ingestion.providers.base import AbstractDiscoveryProvider

logger = logging.getLogger(__name__)

_RING_SECRET_ARN: str = os.environ.get("RING_SECRET_ARN", "")
_RING_OAUTH_URL = "https://oauth.ring.com/oauth/token"
_RING_API_URL = "https://api.ring.com/clients_api"
_USER_AGENT = "android:com.ringapp:2.0.67(423)"

_cred_cache: Optional[Dict[str, str]] = None
_token_cache: Optional[Dict[str, str]] = None
_injected_refresh_token: Optional[str] = None


class RingTwoFactorRequired(Exception):
    """Raised when Ring returns 412 — caller must obtain a refresh_token externally."""
    def __init__(self, email: str):
        self.email = email
        super().__init__("Ring 2FA required")


def inject_refresh_token(token: str) -> None:
    """Inject a refresh_token from the ingest request, bypassing the cached secret.

    Called by the ingestion handler when the caller passes a fresh token in
    the request body.  Clears the access_token cache so re-auth runs
    immediately, and marks the token for persistence back to Secrets Manager
    after successful auth.
    """
    global _injected_refresh_token, _token_cache
    _injected_refresh_token = token
    _token_cache = None  # force re-auth with the new token


def _persist_refresh_token(new_token: str, creds: Dict[str, str]) -> None:
    """Write an updated refresh_token back to Secrets Manager."""
    global _cred_cache
    if not _RING_SECRET_ARN or not new_token:
        return
    try:
        import boto3
        updated = {**creds, "refresh_token": new_token}
        boto3.client("secretsmanager").update_secret(
            SecretId=_RING_SECRET_ARN,
            SecretString=json.dumps(updated),
        )
        _cred_cache = updated
        logger.info("Ring refresh_token persisted to Secrets Manager.")
    except Exception as exc:
        logger.warning("Failed to persist Ring refresh_token: %s", exc)

# 'other'-category device kinds that are bridges/hubs, not controllable lights.
_SKIP_OTHER_KINDS = frozenset({
    "hp_base_v1", "basestation_v1",
    "chime", "chime_v2", "chime_pro", "chime_pro_v2",
})


def _get_credentials() -> Optional[Dict[str, str]]:
    global _cred_cache
    if _cred_cache is not None:
        return _cred_cache
    if not _RING_SECRET_ARN:
        logger.warning("RING_SECRET_ARN not set — cannot authenticate with Ring.")
        return None
    import boto3
    try:
        resp = boto3.client("secretsmanager").get_secret_value(SecretId=_RING_SECRET_ARN)
        _cred_cache = json.loads(resp["SecretString"])
        logger.info("Ring credentials loaded from Secrets Manager.")
        return _cred_cache
    except Exception as exc:
        logger.error("Failed to load Ring credentials: %s", exc, exc_info=True)
        return None


def _hardware_id(creds: Dict[str, str]) -> str:
    if creds.get("hardware_id"):
        return creds["hardware_id"]
    return str(_uuid_mod.uuid5(_uuid_mod.NAMESPACE_DNS, creds.get("email", "ring")))


async def _ensure_token(session: Any, creds: Dict[str, str]) -> str:
    global _token_cache, _injected_refresh_token
    if _token_cache and _token_cache.get("access_token"):
        return _token_cache["access_token"]

    hw_id = _hardware_id(creds)
    using_injected = bool(_injected_refresh_token)
    refresh_tok = (
        _injected_refresh_token
        or (_token_cache or {}).get("refresh_token")
        or creds.get("refresh_token")
    )

    headers: Dict[str, str] = {
        "User-Agent": _USER_AGENT,
        "hardware_id": hw_id,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    form_data: Dict[str, str]
    if refresh_tok:
        form_data = {
            "client_id": "ring_official_android",
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
            "scope": "client",
        }
    else:
        form_data = {
            "client_id": "ring_official_android",
            "grant_type": "password",
            "username": creds["email"],
            "password": creds["password"],
            "scope": "client",
        }

    async with session.post(_RING_OAUTH_URL, headers=headers, data=form_data) as resp:
        if resp.status == 412:
            raise RingTwoFactorRequired(creds.get("email", ""))
        resp.raise_for_status()
        token_data = await resp.json(content_type=None)

    access_token = token_data.get("access_token")
    if not access_token:
        raise RuntimeError(f"Ring auth returned no access_token: {token_data}")

    new_refresh = token_data.get("refresh_token") or refresh_tok or ""
    _token_cache = {"access_token": access_token, "refresh_token": new_refresh}

    if using_injected or new_refresh != creds.get("refresh_token", ""):
        _injected_refresh_token = None
        _persist_refresh_token(new_refresh, creds)

    logger.debug("Ring access token acquired (prefix=%s…).", access_token[:8])
    return access_token


class RingDiscovery(AbstractDiscoveryProvider):

    @property
    def name(self) -> str:
        return "ring"

    async def discover_all(self) -> List[Any]:
        from ingestion.device_registry import DeviceRecord

        creds = _get_credentials()
        if not creds:
            logger.error("No Ring credentials available — aborting discovery.")
            return []

        import aiohttp

        logger.info("Authenticating with Ring cloud…")
        try:
            async with aiohttp.ClientSession() as session:
                token = await _ensure_token(session, creds)
                hw_id = _hardware_id(creds)
                headers = {
                    "Authorization": f"Bearer {token}",
                    "User-Agent": _USER_AGENT,
                    "hardware_id": hw_id,
                }
                logger.info("Fetching Ring device list…")
                async with session.get(
                    f"{_RING_API_URL}/ring_devices", headers=headers
                ) as resp:
                    resp.raise_for_status()
                    raw = await resp.json(content_type=None)
        except RingTwoFactorRequired:
            raise
        except Exception as exc:
            logger.error("Ring API error: %s", exc, exc_info=True)
            return []

        now = datetime.now(timezone.utc).isoformat()
        records: List[DeviceRecord] = []

        for device in raw.get("doorbots", []):
            self._convert_and_append(device, "doorbots", "RingDoorbell", records, now)

        for device in raw.get("authorized_doorbots", []):
            self._convert_and_append(device, "doorbots", "RingDoorbell", records, now)

        for device in raw.get("stickup_cams", []):
            self._convert_and_append(device, "stickup_cams", "RingCamera", records, now)

        for device in raw.get("other", []):
            kind = device.get("kind", "")
            if kind in _SKIP_OTHER_KINDS:
                logger.debug("Skipping Ring other device kind=%r", kind)
                continue
            self._convert_and_append(device, "other", "RingLight", records, now)

        logger.info(
            "Ring discovery complete — %d device(s): %s",
            len(records),
            [r.name for r in records],
        )
        return records

    def _convert_and_append(
        self,
        device: Dict[str, Any],
        category: str,
        device_type: str,
        records: List,
        now: str,
    ) -> None:
        try:
            record = self._to_record(device, category, device_type, now)
            records.append(record)
            logger.info(
                "Device registered — name=%r type=%r model=%r id=%s",
                record.name, record.device_type, record.model, record.device_id,
            )
        except Exception as exc:
            logger.warning(
                "Failed to convert Ring device id=%s: %s",
                device.get("id"), exc, exc_info=True,
            )

    def _to_record(
        self,
        device: Dict[str, Any],
        category: str,
        device_type: str,
        now: str,
    ) -> Any:
        from ingestion.device_registry import DeviceRecord

        ring_id = str(device["id"])
        desc = device.get("description", {})
        name = desc.get("name") or device.get("name") or ring_id
        model = desc.get("type_name") or device.get("kind", "")
        mac = desc.get("mac_address", "")

        return DeviceRecord(
            device_id=ring_id,
            provider=self.name,
            name=name,
            ip="",
            mac=mac,
            device_type=device_type,
            model=model,
            capabilities=_capabilities(device_type),
            fingerprint=_fingerprint(
                device_id=ring_id, name=name,
                device_type=device_type, model=model,
            ),
            status="active",
            last_seen=now,
            last_synced=now,
            sync_mode="",
            provider_meta={
                "category": category,
                "ring_id": ring_id,
                "kind": device.get("kind", ""),
            },
        )


def _capabilities(device_type: str) -> List[str]:
    if device_type == "RingLight":
        return ["turn_on", "turn_off", "toggle", "get_status", "set_brightness"]
    return ["get_status"]


def _fingerprint(*, device_id: str, name: str, device_type: str, model: str) -> str:
    payload = json.dumps(
        {"device_id": device_id, "name": name, "device_type": device_type, "model": model},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
