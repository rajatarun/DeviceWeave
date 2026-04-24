"""
Kasa discovery provider — enumerates TP-Link Kasa devices via LAN broadcast.

Uses python-kasa's Discover.discover() which sends a UDP broadcast and
collects responses.  Each responding device is probed with update() to
fetch full capabilities and metadata.

Network requirement: the Lambda (or host running this code) must be on the
same LAN segment as the Kasa devices, or routable to their subnet.
For AWS deployments this means placing the Lambda inside a VPC whose
subnets have L2 adjacency to the IoT VLAN, or running on a local host.

Discovery is performed concurrently across all responding devices using
asyncio.gather so total latency ≈ max(individual probe latencies).
Devices that fail to respond to update() are logged and excluded from the
result — they do not abort the overall scan.
"""

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from kasa import Credentials, Discover, SmartBulb, SmartPlug

from ingestion.providers.base import AbstractDiscoveryProvider

logger = logging.getLogger(__name__)

# UDP broadcast timeout in seconds.  5 s is sufficient for a quiet LAN;
# raise to 10 if devices are consistently missed on first attempt.
_DISCOVERY_TIMEOUT = 5

_KASA_SECRET_ARN: str = os.environ.get("KASA_SECRET_ARN", "")

# Module-level cache — populated once per warm Lambda container.
_credentials_cache: Optional[Credentials] = None


def _get_credentials() -> Optional[Credentials]:
    """
    Fetch Kasa credentials from Secrets Manager on first call; return cached
    value on subsequent calls within the same Lambda container.

    Expected secret value (JSON):
        {"email": "user@example.com", "password": "secret"}

    Returns None if KASA_SECRET_ARN is not set (local / unauthenticated use).
    """
    global _credentials_cache
    if _credentials_cache is not None:
        return _credentials_cache
    if not _KASA_SECRET_ARN:
        return None

    import boto3
    client = boto3.client("secretsmanager")
    try:
        resp = client.get_secret_value(SecretId=_KASA_SECRET_ARN)
        secret = json.loads(resp["SecretString"])
        _credentials_cache = Credentials(
            username=secret["email"],
            password=secret["password"],
        )
        logger.info("Kasa credentials loaded from Secrets Manager.")
        return _credentials_cache
    except Exception as exc:
        logger.error("Failed to load Kasa credentials from Secrets Manager: %s", exc)
        return None


class KasaDiscovery(AbstractDiscoveryProvider):

    @property
    def name(self) -> str:
        return "kasa"

    async def discover_all(self) -> List[Any]:
        """
        Broadcast on the local network and return a DeviceRecord for every
        responsive Kasa device.

        Returns an empty list (not an exception) if no devices respond or
        if the network is unreachable.
        """
        from ingestion.device_registry import DeviceRecord  # local to avoid circular

        credentials = _get_credentials()
        logger.info(
            "Starting Kasa discovery — timeout=%ds credentials=%s",
            _DISCOVERY_TIMEOUT,
            "loaded" if credentials else "none (unauthenticated)",
        )

        try:
            raw: Dict[str, Any] = await Discover.discover(
                credentials=credentials,
                timeout=_DISCOVERY_TIMEOUT,
            )
        except Exception as exc:
            logger.error("Kasa broadcast discovery failed: %s", exc, exc_info=True)
            return []

        logger.info(
            "Kasa broadcast complete — %d device(s) responded: %s",
            len(raw),
            list(raw.keys()) if raw else "[]",
        )

        if not raw:
            logger.warning(
                "No Kasa devices responded. Possible causes: Lambda not on the same "
                "LAN/VPC subnet as devices, UDP broadcast blocked by a firewall or "
                "security group, or all devices are offline."
            )
            return []

        tasks = [self._probe(ip, device) for ip, device in raw.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        records: List[DeviceRecord] = []
        for result in results:
            if isinstance(result, DeviceRecord):
                records.append(result)
            elif isinstance(result, Exception):
                logger.warning("Probe raised unexpected exception: %s", result, exc_info=result)

        logger.info(
            "Kasa discovery complete — responded=%d probed_ok=%d failed=%d",
            len(raw),
            len(records),
            len(raw) - len(records),
        )
        return records

    async def _probe(self, ip: str, device: Any) -> Any:
        """
        Call update() on a single device and convert to a DeviceRecord.

        Returns the DeviceRecord on success, logs a warning and returns
        None on failure (caller filters None from the results list).
        """
        from ingestion.device_registry import DeviceRecord

        logger.debug("Probing device at %s (type=%s)…", ip, type(device).__name__)
        try:
            await device.update()
        except Exception as exc:
            logger.warning(
                "update() failed for device at %s: %s", ip, exc, exc_info=True
            )
            return None

        logger.info(
            "Probed %s — alias=%r model=%r mac=%r is_on=%s device_id=%r",
            ip,
            getattr(device, "alias", None),
            getattr(device, "model", None),
            getattr(device, "mac", None),
            getattr(device, "is_on", None),
            getattr(device, "device_id", None),
        )

        try:
            return self._to_record(ip, device)
        except Exception as exc:
            logger.warning(
                "Failed to build DeviceRecord for %s: %s", ip, exc, exc_info=True
            )
            return None

    # ------------------------------------------------------------------
    # Conversion helpers
    # ------------------------------------------------------------------

    def _to_record(self, ip: str, device: Any) -> Any:
        from ingestion.device_registry import DeviceRecord

        device_id = self._device_id(device, ip)
        mac = getattr(device, "mac", "") or ""
        name = getattr(device, "alias", "") or ip
        model = getattr(device, "model", "unknown") or "unknown"
        device_type = self._device_type(device)
        capabilities = self._capabilities(device)
        now = datetime.now(timezone.utc).isoformat()

        fingerprint = _fingerprint(ip=ip, name=name, device_type=device_type,
                                   model=model, mac=mac)

        provider_meta: Dict[str, Any] = {
            "raw_alias": name,
            "is_on": device.is_on,
        }
        if isinstance(device, SmartBulb):
            provider_meta["brightness"] = getattr(device, "brightness", None)
            provider_meta["is_color"] = getattr(device, "is_color", False)
            provider_meta["is_dimmable"] = getattr(device, "is_dimmable", False)
            provider_meta["is_variable_color_temp"] = getattr(
                device, "is_variable_color_temp", False
            )

        return DeviceRecord(
            device_id=device_id,
            provider=self.name,
            name=name,
            ip=ip,
            mac=mac,
            device_type=device_type,
            model=model,
            capabilities=capabilities,
            fingerprint=fingerprint,
            status="active",
            last_seen=now,
            last_synced=now,
            sync_mode="",          # set by pipeline before writing
            provider_meta=provider_meta,
        )

    @staticmethod
    def _device_id(device: Any, fallback_ip: str) -> str:
        """
        Prefer device.device_id (hardware-stable MAC-derived ID).
        Fall back to MAC, then IP if neither is available.
        """
        did = getattr(device, "device_id", None)
        if did:
            return str(did)
        mac = getattr(device, "mac", None)
        if mac:
            return mac.replace(":", "").upper()
        return fallback_ip.replace(".", "_")

    @staticmethod
    def _device_type(device: Any) -> str:
        if isinstance(device, SmartBulb):
            return "SmartBulb"
        # SmartStrip is a subclass of SmartPlug in some versions; check SmartPlug last.
        return "SmartPlug"

    @staticmethod
    def _capabilities(device: Any) -> List[str]:
        caps = ["turn_on", "turn_off", "toggle", "get_status"]
        if isinstance(device, SmartBulb):
            if getattr(device, "is_dimmable", False):
                caps.append("set_brightness")
            if getattr(device, "is_color", False):
                caps.append("set_color")
            if getattr(device, "is_variable_color_temp", False):
                caps.append("set_color_temp")
        if getattr(device, "has_emeter", False):
            caps.append("get_energy_usage")
        return caps


def _fingerprint(*, ip: str, name: str, device_type: str, model: str, mac: str) -> str:
    """
    Stable 16-character hex fingerprint of a device's identity fields.

    Changing any identity field causes a new fingerprint, triggering a
    DynamoDB write in delta mode.  Runtime state (is_on, brightness) is
    excluded — those are not identity fields.
    """
    payload = json.dumps(
        {"ip": ip, "name": name, "device_type": device_type, "model": model, "mac": mac},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
