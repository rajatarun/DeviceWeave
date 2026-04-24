"""
SwitchBot discovery provider — cloud-based device enumeration.

Uses the SwitchBot cloud REST API v1.1 to list every device registered
to the account. Works from Lambda without LAN access.

Credentials are loaded from Secrets Manager (SWITCHBOT_SECRET_ARN):
    {"token": "...", "secret": "..."}
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ingestion.providers.base import AbstractDiscoveryProvider

logger = logging.getLogger(__name__)

_SWITCHBOT_SECRET_ARN: str = os.environ.get("SWITCHBOT_SECRET_ARN", "")
_SWITCHBOT_API_URL = "https://api.switch-bot.com"

_DEVICE_TYPE_MAP: Dict[str, str] = {
    "Color Bulb": "SwitchBotBulb",
    "Strip Light": "SwitchBotBulb",
    "LED Strip Light": "SwitchBotBulb",
    "Ceiling Light": "SwitchBotBulb",
    "Ceiling Light Pro": "SwitchBotBulb",
    "Plug": "SwitchBotPlug",
    "Plug Mini (US)": "SwitchBotPlug",
    "Plug Mini (JP)": "SwitchBotPlug",
    "Smart Plug": "SwitchBotPlug",
    "Fan": "SwitchBotFan",
    "Ceiling Fan": "SwitchBotFan",
    "Curtain": "SwitchBotCurtain",
    "Curtain3": "SwitchBotCurtain",
    "Roller Shade": "SwitchBotCurtain",
}

_cred_cache: Optional[Dict[str, str]] = None


def _get_credentials() -> Optional[Dict[str, str]]:
    global _cred_cache
    if _cred_cache is not None:
        return _cred_cache
    if not _SWITCHBOT_SECRET_ARN:
        logger.warning("SWITCHBOT_SECRET_ARN not set — cannot authenticate with SwitchBot.")
        return None
    import boto3
    try:
        resp = boto3.client("secretsmanager").get_secret_value(SecretId=_SWITCHBOT_SECRET_ARN)
        secret = json.loads(resp["SecretString"])
        _cred_cache = {"token": secret["token"], "secret": secret["secret"]}
        logger.info("SwitchBot credentials loaded from Secrets Manager.")
        return _cred_cache
    except Exception as exc:
        logger.error("Failed to load SwitchBot credentials: %s", exc, exc_info=True)
        return None


def _auth_headers(token: str, secret: str) -> Dict[str, str]:
    t = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    string_to_sign = f"{token}{t}{nonce}"
    sign = base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8").upper()
    return {
        "Authorization": token,
        "sign": sign,
        "t": t,
        "nonce": nonce,
        "Content-Type": "application/json",
    }


class SwitchBotDiscovery(AbstractDiscoveryProvider):

    @property
    def name(self) -> str:
        return "switchbot"

    async def discover_all(self) -> List[Any]:
        from ingestion.device_registry import DeviceRecord

        creds = _get_credentials()
        if not creds:
            logger.error("No SwitchBot credentials available — aborting discovery.")
            return []

        import aiohttp

        headers = _auth_headers(creds["token"], creds["secret"])
        url = f"{_SWITCHBOT_API_URL}/v1.1/devices"

        logger.info("Fetching SwitchBot device list from %s…", url)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
        except Exception as exc:
            logger.error("SwitchBot API error: %s", exc, exc_info=True)
            return []

        if data.get("statusCode") != 100:
            logger.error(
                "SwitchBot API returned statusCode %s: %s",
                data.get("statusCode"), data.get("message"),
            )
            return []

        device_list = data.get("body", {}).get("deviceList", [])
        logger.info(
            "SwitchBot returned %d device(s): %s",
            len(device_list),
            [d.get("deviceName") for d in device_list],
        )

        records: List[DeviceRecord] = []
        for device in device_list:
            if not device.get("enableCloudService", True):
                logger.debug("Skipping non-cloud device: %s", device.get("deviceName"))
                continue
            try:
                record = self._to_record(device)
                records.append(record)
                logger.info(
                    "Device registered — name=%r type=%r id=%s",
                    record.name, record.device_type, record.device_id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to convert SwitchBot device %r: %s",
                    device.get("deviceName"), exc, exc_info=True,
                )

        logger.info("SwitchBot discovery complete — %d valid record(s).", len(records))
        return records

    def _to_record(self, device: Dict[str, Any]) -> Any:
        from ingestion.device_registry import DeviceRecord

        device_id = device["deviceId"]
        name = device.get("deviceName") or device_id
        raw_type = device.get("deviceType", "")
        device_type = _DEVICE_TYPE_MAP.get(raw_type, "SwitchBotSwitch")
        now = datetime.now(timezone.utc).isoformat()

        return DeviceRecord(
            device_id=device_id,
            provider=self.name,
            name=name,
            ip="",
            mac="",
            device_type=device_type,
            model=raw_type,
            capabilities=_capabilities(device_type),
            fingerprint=_fingerprint(
                device_id=device_id, name=name,
                device_type=device_type, model=raw_type,
            ),
            status="active",
            last_seen=now,
            last_synced=now,
            sync_mode="",
            provider_meta={
                "hub_device_id": device.get("hubDeviceId", ""),
                "enable_cloud_service": str(device.get("enableCloudService", True)),
                "device_type_raw": raw_type,
            },
        )


def _capabilities(device_type: str) -> List[str]:
    caps = ["turn_on", "turn_off", "toggle", "get_status"]
    if device_type == "SwitchBotBulb":
        caps += ["set_brightness", "set_color", "set_color_temp"]
    return caps


def _fingerprint(*, device_id: str, name: str, device_type: str, model: str) -> str:
    payload = json.dumps(
        {"device_id": device_id, "name": name, "device_type": device_type, "model": model},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
