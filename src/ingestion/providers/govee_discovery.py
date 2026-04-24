"""
Govee discovery provider — cloud-based device enumeration.

Uses the Govee Developer API to list every device registered to the account.

Credentials are loaded from Secrets Manager (GOVEE_SECRET_ARN):
    {"api_key": "..."}
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ingestion.providers.base import AbstractDiscoveryProvider

logger = logging.getLogger(__name__)

_GOVEE_SECRET_ARN: str = os.environ.get("GOVEE_SECRET_ARN", "")
_GOVEE_API_URL = "https://developer-api.govee.com"

_cred_cache: Optional[Dict[str, str]] = None


def _get_credentials() -> Optional[Dict[str, str]]:
    global _cred_cache
    if _cred_cache is not None:
        return _cred_cache
    if not _GOVEE_SECRET_ARN:
        logger.warning("GOVEE_SECRET_ARN not set — cannot authenticate with Govee.")
        return None
    import boto3
    try:
        resp = boto3.client("secretsmanager").get_secret_value(SecretId=_GOVEE_SECRET_ARN)
        secret = json.loads(resp["SecretString"])
        _cred_cache = {"api_key": secret["api_key"]}
        logger.info("Govee credentials loaded from Secrets Manager.")
        return _cred_cache
    except Exception as exc:
        logger.error("Failed to load Govee credentials: %s", exc, exc_info=True)
        return None


class GoveeDiscovery(AbstractDiscoveryProvider):

    @property
    def name(self) -> str:
        return "govee"

    async def discover_all(self) -> List[Any]:
        from ingestion.device_registry import DeviceRecord

        creds = _get_credentials()
        if not creds:
            logger.error("No Govee credentials available — aborting discovery.")
            return []

        import aiohttp

        headers = {"Govee-API-Key": creds["api_key"]}
        url = f"{_GOVEE_API_URL}/v1/devices"

        logger.info("Fetching Govee device list from %s…", url)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
        except Exception as exc:
            logger.error("Govee API error: %s", exc, exc_info=True)
            return []

        if data.get("code") != 200:
            logger.error(
                "Govee API returned code %s: %s",
                data.get("code"), data.get("message"),
            )
            return []

        device_list = data.get("data", {}).get("devices", [])
        logger.info(
            "Govee returned %d device(s): %s",
            len(device_list),
            [d.get("deviceName") for d in device_list],
        )

        records: List[DeviceRecord] = []
        for device in device_list:
            if not device.get("controllable", True):
                logger.debug("Skipping non-controllable device: %s", device.get("deviceName"))
                continue
            try:
                record = self._to_record(device)
                records.append(record)
                logger.info(
                    "Device registered — name=%r type=%r model=%r id=%s",
                    record.name, record.device_type, record.model, record.device_id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to convert Govee device %r: %s",
                    device.get("deviceName"), exc, exc_info=True,
                )

        logger.info("Govee discovery complete — %d valid record(s).", len(records))
        return records

    def _to_record(self, device: Dict[str, Any]) -> Any:
        from ingestion.device_registry import DeviceRecord

        device_id = device["device"]  # MAC address used as unique ID
        name = device.get("deviceName") or device_id
        model = device.get("model") or device.get("sku", "")
        support_cmds = device.get("supportCmds", [])
        device_type = _device_type(support_cmds)
        now = datetime.now(timezone.utc).isoformat()

        return DeviceRecord(
            device_id=device_id,
            provider=self.name,
            name=name,
            ip="",
            mac=device_id,
            device_type=device_type,
            model=model,
            capabilities=_capabilities(support_cmds),
            fingerprint=_fingerprint(
                device_id=device_id, name=name,
                device_type=device_type, model=model,
            ),
            status="active",
            last_seen=now,
            last_synced=now,
            sync_mode="",
            provider_meta={
                "sku": device.get("sku", model),
                "support_cmds": json.dumps(support_cmds),
                "retrievable": str(device.get("retrievable", False)),
            },
        )


def _device_type(support_cmds: List[str]) -> str:
    if any(cmd in support_cmds for cmd in ("brightness", "color", "colorTem")):
        return "GoveeBulb"
    return "GoveePlug"


def _capabilities(support_cmds: List[str]) -> List[str]:
    caps = ["turn_on", "turn_off", "toggle", "get_status"]
    if "brightness" in support_cmds:
        caps.append("set_brightness")
    if "color" in support_cmds:
        caps.append("set_color")
    if "colorTem" in support_cmds:
        caps.append("set_color_temp")
    return caps


def _fingerprint(*, device_id: str, name: str, device_type: str, model: str) -> str:
    payload = json.dumps(
        {"device_id": device_id, "name": name, "device_type": device_type, "model": model},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
