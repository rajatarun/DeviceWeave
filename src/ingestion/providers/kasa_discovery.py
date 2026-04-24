"""
Kasa discovery provider — cloud-based device enumeration.

Uses the Kasa cloud REST API (wap.tplinkcloud.com) to list every device
registered to the account.  Works from Lambda without LAN access — all
traffic goes through Kasa's cloud over HTTPS.

Credentials are loaded from Secrets Manager:
    {"email": "user@example.com", "password": "secret"}

aiohttp is available as a transitive dependency of python-kasa.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ingestion.providers.base import AbstractDiscoveryProvider

logger = logging.getLogger(__name__)

_KASA_SECRET_ARN: str = os.environ.get("KASA_SECRET_ARN", "")
_KASA_CLOUD_URL = "https://wap.tplinkcloud.com"
_APP_TYPE = "Kasa_Android"
_TERMINAL_UUID = "00000000-0000-0000-0000-000000000000"

# Model prefix → SmartBulb (everything else → SmartPlug)
_BULB_MODEL_PREFIXES = ("LB", "KL", "KE", "MR", "KB")

_cred_cache: Optional[Dict[str, str]] = None


def _get_credentials() -> Optional[Dict[str, str]]:
    """Return {"email": ..., "password": ...} from Secrets Manager, cached per container."""
    global _cred_cache
    if _cred_cache is not None:
        return _cred_cache
    if not _KASA_SECRET_ARN:
        logger.warning("KASA_SECRET_ARN not set — cannot authenticate with Kasa cloud.")
        return None
    import boto3
    try:
        resp = boto3.client("secretsmanager").get_secret_value(SecretId=_KASA_SECRET_ARN)
        secret = json.loads(resp["SecretString"])
        _cred_cache = {"email": secret["email"], "password": secret["password"]}
        logger.info("Kasa credentials loaded from Secrets Manager (user=%s).", secret["email"])
        return _cred_cache
    except Exception as exc:
        logger.error("Failed to load Kasa credentials from Secrets Manager: %s", exc, exc_info=True)
        return None


class KasaDiscovery(AbstractDiscoveryProvider):

    @property
    def name(self) -> str:
        return "kasa"

    async def discover_all(self) -> List[Any]:
        from ingestion.device_registry import DeviceRecord

        creds = _get_credentials()
        if not creds:
            logger.error("No Kasa credentials available — aborting discovery.")
            return []

        import aiohttp

        logger.info("Authenticating with Kasa cloud (%s)…", _KASA_CLOUD_URL)
        try:
            async with aiohttp.ClientSession() as session:
                token = await _cloud_login(session, creds["email"], creds["password"])
                logger.info("Login successful. Fetching device list…")
                raw_devices = await _cloud_get_devices(session, token)
        except Exception as exc:
            logger.error("Kasa cloud API error: %s", exc, exc_info=True)
            return []

        logger.info(
            "Kasa cloud returned %d device(s): %s",
            len(raw_devices),
            [d.get("alias") for d in raw_devices],
        )

        records: List[DeviceRecord] = []
        for device in raw_devices:
            try:
                record = self._to_record(device)
                records.append(record)
                logger.info(
                    "Device registered — alias=%r model=%r mac=%r type=%s status=%s",
                    record.name, record.model, record.mac,
                    record.device_type, record.status,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to convert cloud device %r: %s",
                    device.get("alias"), exc, exc_info=True,
                )

        logger.info("Discovery complete — %d valid record(s).", len(records))
        return records

    def _to_record(self, device: Dict[str, Any]) -> Any:
        from ingestion.device_registry import DeviceRecord

        alias = device.get("alias") or device.get("deviceId", "unknown")
        mac = (device.get("deviceMac") or "").replace("-", ":").upper()
        model = device.get("deviceModel") or "unknown"
        device_id = device.get("deviceId") or mac or alias
        device_type = _device_type(device)
        status = "active" if device.get("status") == 1 else "offline"
        now = datetime.now(timezone.utc).isoformat()

        return DeviceRecord(
            device_id=device_id,
            provider=self.name,
            name=alias,
            ip="",  # cloud-managed — no local IP available
            mac=mac,
            device_type=device_type,
            model=model,
            capabilities=_capabilities(device_type),
            fingerprint=_fingerprint(
                device_id=device_id, name=alias,
                device_type=device_type, model=model, mac=mac,
            ),
            status=status,
            last_seen=now,
            last_synced=now,
            sync_mode="",  # set by pipeline before writing
            provider_meta={
                "app_server_url": device.get("appServerUrl", ""),
                "device_type_raw": device.get("deviceType", ""),
                "hw_ver": device.get("deviceHwVer", ""),
                "fw_ver": device.get("fwVer", ""),
            },
        )


# ---------------------------------------------------------------------------
# Cloud API
# ---------------------------------------------------------------------------

async def _cloud_request(
    session: Any,
    method: str,
    params: Dict[str, Any],
    token: Optional[str] = None,
) -> Dict[str, Any]:
    url = f"{_KASA_CLOUD_URL}?token={token}" if token else _KASA_CLOUD_URL
    async with session.post(url, json={"method": method, "params": params}) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    error_code = data.get("error_code", 0)
    if error_code != 0:
        raise RuntimeError(f"Kasa cloud error {error_code}: {data.get('msg', data)}")
    return data.get("result", {})


async def _cloud_login(session: Any, username: str, password: str) -> str:
    result = await _cloud_request(session, "login", {
        "appType": _APP_TYPE,
        "cloudPassword": password,
        "cloudUserName": username,
        "terminalUUID": _TERMINAL_UUID,
    })
    token = result.get("token")
    if not token:
        raise RuntimeError(f"Login returned no token: {result}")
    logger.debug("Cloud token acquired (prefix=%s…).", token[:8])
    return token


async def _cloud_get_devices(session: Any, token: str) -> List[Dict[str, Any]]:
    result = await _cloud_request(session, "getDeviceList", {}, token=token)
    devices = result.get("deviceList", [])
    logger.debug("Raw cloud device list: %s", json.dumps(devices, default=str))
    return devices


# ---------------------------------------------------------------------------
# Device helpers
# ---------------------------------------------------------------------------

def _device_type(device: Dict[str, Any]) -> str:
    model = (device.get("deviceModel") or "").upper()
    if any(model.startswith(p) for p in _BULB_MODEL_PREFIXES):
        return "SmartBulb"
    type_str = (device.get("deviceType") or "").upper()
    if "BULB" in type_str or "LIGHT" in type_str:
        return "SmartBulb"
    return "SmartPlug"


def _capabilities(device_type: str) -> List[str]:
    caps = ["turn_on", "turn_off", "toggle", "get_status"]
    if device_type == "SmartBulb":
        caps += ["set_brightness", "set_color", "set_color_temp"]
    return caps


def _fingerprint(*, device_id: str, name: str, device_type: str, model: str, mac: str) -> str:
    payload = json.dumps(
        {"device_id": device_id, "name": name, "device_type": device_type,
         "model": model, "mac": mac},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
