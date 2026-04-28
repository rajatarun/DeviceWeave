"""
MyQ discovery provider — cloud-based device enumeration via pymyq.

Credentials are loaded from Secrets Manager (MYQ_SECRET_ARN):
    {"email": "user@example.com", "password": "secret"}

Discovered device types:
  MyQGarageDoor — capabilities: get_status, open, close, toggle
  MyQLamp       — capabilities: get_status, turn_on, turn_off, toggle

Device families not in the controllable set (gateway, keypad, hub, etc.)
are skipped silently; only garage doors and lamps are registered.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ingestion.providers.base import AbstractDiscoveryProvider

logger = logging.getLogger(__name__)

_MYQ_SECRET_ARN: str = os.environ.get("MYQ_SECRET_ARN", "")

# Device families that map to controllable device types.
_GARAGE_FAMILIES = {"garagedoor"}
_LAMP_FAMILIES = {"lamp", "light"}
_CONTROLLABLE_FAMILIES = _GARAGE_FAMILIES | _LAMP_FAMILIES

_cred_cache: Optional[Dict[str, str]] = None


def _get_credentials() -> Optional[Dict[str, str]]:
    global _cred_cache
    if _cred_cache is not None:
        return _cred_cache
    if not _MYQ_SECRET_ARN:
        logger.warning("MYQ_SECRET_ARN not set — cannot authenticate with MyQ.")
        return None
    import boto3
    from botocore.exceptions import ClientError
    try:
        resp = boto3.client("secretsmanager").get_secret_value(SecretId=_MYQ_SECRET_ARN)
        secret = json.loads(resp["SecretString"])
        _cred_cache = {"email": secret["email"], "password": secret["password"]}
        logger.info("MyQ credentials loaded from Secrets Manager (user=%s).", secret["email"])
        return _cred_cache
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.warning(
                "MyQ secret not found (%s) — create it to enable MyQ discovery.",
                _MYQ_SECRET_ARN,
            )
        else:
            logger.error(
                "Failed to load MyQ credentials from Secrets Manager: %s", exc, exc_info=True,
            )
        return None
    except Exception as exc:
        logger.error("Failed to load MyQ credentials from Secrets Manager: %s", exc, exc_info=True)
        return None


class MyQDiscovery(AbstractDiscoveryProvider):

    @property
    def name(self) -> str:
        return "myq"

    async def discover_all(self) -> List[Any]:
        from ingestion.device_registry import DeviceRecord

        creds = _get_credentials()
        if not creds:
            logger.error("No MyQ credentials available — aborting discovery.")
            return []

        import aiohttp
        import pymyq

        logger.info("Authenticating with MyQ cloud…")
        try:
            async with aiohttp.ClientSession() as websession:
                myq = await pymyq.login(creds["email"], creds["password"], websession)
                await myq.update_device_info()
                raw_devices = dict(myq.devices)
        except Exception as exc:
            logger.error("MyQ login/discovery error: %s", exc, exc_info=True)
            return []

        logger.info(
            "MyQ returned %d device(s): %s",
            len(raw_devices),
            [d.name for d in raw_devices.values()],
        )

        now = datetime.now(timezone.utc).isoformat()
        records: List[DeviceRecord] = []

        for serial, device in raw_devices.items():
            family = (getattr(device, "device_family", "") or "").lower()
            if family not in _CONTROLLABLE_FAMILIES:
                logger.debug(
                    "Skipping MyQ device name=%r family=%r — not controllable.",
                    getattr(device, "name", serial), family,
                )
                continue
            try:
                record = self._to_record(serial, device, now)
                records.append(record)
                logger.info(
                    "Device registered — name=%r type=%r serial=%s online=%s",
                    record.name, record.device_type, serial,
                    record.provider_meta.get("online"),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to convert MyQ device serial=%s: %s", serial, exc, exc_info=True,
                )

        logger.info("MyQ discovery complete — %d valid record(s).", len(records))
        return records

    def _to_record(self, serial: str, device: Any, now: str) -> Any:
        from ingestion.device_registry import DeviceRecord

        name = getattr(device, "name", None) or serial
        family = (getattr(device, "device_family", "") or "").lower()
        device_type = _device_type(family)
        online = bool(getattr(device, "online", False))
        status = "active" if online else "offline"

        return DeviceRecord(
            device_id=serial,
            provider=self.name,
            name=name,
            ip="",
            mac="",
            device_type=device_type,
            model=family,
            capabilities=_capabilities(device_type),
            fingerprint=_fingerprint(
                device_id=serial,
                name=name,
                device_type=device_type,
                model=family,
            ),
            status=status,
            last_seen=now,
            last_synced=now,
            sync_mode="",
            provider_meta={
                "myq_device_id": serial,
                "device_family": family,
                "online": str(online),
            },
        )


def _device_type(family: str) -> str:
    if family in _GARAGE_FAMILIES:
        return "MyQGarageDoor"
    return "MyQLamp"


def _capabilities(device_type: str) -> List[str]:
    if device_type == "MyQGarageDoor":
        return ["get_status", "open", "close", "toggle"]
    return ["get_status", "turn_on", "turn_off", "toggle"]


def _fingerprint(*, device_id: str, name: str, device_type: str, model: str) -> str:
    payload = json.dumps(
        {"device_id": device_id, "name": name, "device_type": device_type, "model": model},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
