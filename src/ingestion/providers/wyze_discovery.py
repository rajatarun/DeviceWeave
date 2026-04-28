"""
Wyze discovery provider — cloud-based device enumeration.

Uses wyze-sdk to list every device registered to the Wyze account.

Credentials loaded from Secrets Manager (WYZE_SECRET_ARN):
    {"email": "user@example.com", "password": "secret",
     "key_id": "<wyze_developer_key_id>", "api_key": "<wyze_developer_api_key>"}

For accounts with TOTP 2FA, add:
    {"...", "totp_key": "<base32_totp_secret>"}

An access_token obtained on first login is cached in Lambda container memory
to avoid re-login on every warm invocation.  On auth errors the provider
clears the cache and retries once with a fresh login.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ingestion.providers.base import AbstractDiscoveryProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSL alignment
# ---------------------------------------------------------------------------
# wyze-sdk uses `requests`, which defaults to certifi's CA bundle.
# Lambda VPCs with TLS inspection inject a custom CA into the OS store that
# aiohttp (ssl.create_default_context) trusts automatically — but certifi
# does not.  Point requests at the system CA bundle so both HTTP stacks
# behave consistently.  Explicit env-var overrides are respected.

def _configure_ssl() -> None:
    if os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE"):
        return
    for path in (
        "/etc/pki/tls/certs/ca-bundle.crt",  # Amazon Linux 2
        "/etc/ssl/certs/ca-bundle.crt",       # Amazon Linux 2 alt path
        "/etc/ssl/certs/ca-certificates.crt", # Debian/Ubuntu
    ):
        if os.path.exists(path):
            os.environ["REQUESTS_CA_BUNDLE"] = path
            logger.debug("REQUESTS_CA_BUNDLE → %s", path)
            return
    try:
        import certifi
        os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
        logger.debug("REQUESTS_CA_BUNDLE → certifi %s", os.environ["REQUESTS_CA_BUNDLE"])
    except ImportError:
        pass

_configure_ssl()

_WYZE_SECRET_ARN: str = os.environ.get("WYZE_SECRET_ARN", "")

_cred_cache: Optional[Dict[str, str]] = None
_client_cache: Optional[Any] = None  # wyze_sdk.Client


def _get_credentials() -> Optional[Dict[str, str]]:
    global _cred_cache
    if _cred_cache is not None:
        return _cred_cache
    if not _WYZE_SECRET_ARN:
        logger.warning("WYZE_SECRET_ARN not set — cannot authenticate with Wyze.")
        return None
    import boto3
    try:
        resp = boto3.client("secretsmanager").get_secret_value(SecretId=_WYZE_SECRET_ARN)
        _cred_cache = json.loads(resp["SecretString"])
        logger.info("Wyze credentials loaded from Secrets Manager.")
        return _cred_cache
    except Exception as exc:
        logger.error("Failed to load Wyze credentials: %s", exc, exc_info=True)
        return None


def _build_client(creds: Dict[str, str]) -> Any:
    """Create a wyze_sdk.Client.  Uses cached access_token when available."""
    from wyze_sdk import Client

    access_token = creds.get("access_token")
    if access_token:
        logger.debug("Wyze client initialized with cached access_token.")
        return Client(token=access_token)

    logger.info("Logging in to Wyze cloud…")
    login_kwargs: Dict[str, Any] = {
        "email": creds["email"],
        "password": creds["password"],
    }
    if creds.get("key_id"):
        login_kwargs["key_id"] = creds["key_id"]
    if creds.get("api_key"):
        login_kwargs["api_key"] = creds["api_key"]
    if creds.get("totp_key"):
        login_kwargs["totp_key"] = creds["totp_key"]

    response = Client().login(**login_kwargs)
    token = (
        response.get("access_token")
        or response.get("data", {}).get("access_token")
        or ""
    )
    if not token:
        raise RuntimeError("Wyze login returned no access_token.")
    logger.info("Wyze login successful.")
    return Client(token=token)


def _get_client(creds: Dict[str, str]) -> Any:
    global _client_cache
    if _client_cache is not None:
        return _client_cache
    _client_cache = _build_client(creds)
    return _client_cache


def _clear_client_cache() -> None:
    global _client_cache
    _client_cache = None


# ---------------------------------------------------------------------------
# Device type and capability mappings
# ---------------------------------------------------------------------------

# Maps wyze product_type strings → DeviceWeave device_type strings.
_PRODUCT_TYPE_MAP: Dict[str, str] = {
    "MeshLight":     "WyzeBulb",
    "Bulb":          "WyzeBulb",
    "BulbColor":     "WyzeBulb",
    "BulbWhite":     "WyzeBulb",
    "LightStrip":    "WyzeBulb",
    "LightStripPro": "WyzeBulb",
    "Plug":          "WyzePlug",
    "OutdoorPlug":   "WyzePlug",
    "Camera":        "WyzeCamera",
    "Camera3":       "WyzeCamera",
    "DoorBell":      "WyzeCamera",
    "DoorBellPro":   "WyzeCamera",
    "Lock":          "WyzeLock",
    "LockKeypad":    "WyzeLock",
    "MotionSensor":  "WyzeMotionSensor",
    "ContactSensor": "WyzeContactSensor",
    "Thermostat":    "WyzeThermostat",
    "Switch":        "WyzeSwitch",
    "Robot":         "WyzeVacuum",
    "Scale":         "WyzeScale",
}

_CAPABILITIES_MAP: Dict[str, List[str]] = {
    "WyzeBulb":          ["turn_on", "turn_off", "toggle", "get_status",
                          "set_brightness", "set_color_temp"],
    "WyzePlug":          ["turn_on", "turn_off", "toggle", "get_status"],
    "WyzeSwitch":        ["turn_on", "turn_off", "toggle", "get_status"],
    "WyzeCamera":        ["get_status"],
    "WyzeLock":          ["lock", "unlock", "get_status"],
    "WyzeMotionSensor":  ["get_status"],
    "WyzeContactSensor": ["get_status"],
    "WyzeThermostat":    ["get_status", "set_temperature"],
    "WyzeVacuum":        ["get_status"],
    "WyzeScale":         ["get_status"],
}

# Bulb product_types that also support RGB color control.
_COLOR_BULB_TYPES = frozenset({"MeshLight", "BulbColor", "LightStrip", "LightStripPro"})


def _device_type(product_type: str) -> str:
    return _PRODUCT_TYPE_MAP.get(product_type, "WyzeDevice")


def _capabilities(product_type: str) -> List[str]:
    dw_type = _device_type(product_type)
    caps = list(_CAPABILITIES_MAP.get(dw_type, ["get_status"]))
    if product_type in _COLOR_BULB_TYPES and "set_color" not in caps:
        caps.append("set_color")
    return caps


def _fingerprint(*, device_id: str, name: str, device_type: str, model: str) -> str:
    payload = json.dumps(
        {"device_id": device_id, "name": name,
         "device_type": device_type, "model": model},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Discovery provider
# ---------------------------------------------------------------------------

class WyzeDiscovery(AbstractDiscoveryProvider):

    @property
    def name(self) -> str:
        return "wyze"

    async def discover_all(self) -> List[Any]:
        import asyncio

        creds = _get_credentials()
        if not creds:
            logger.error("No Wyze credentials available — aborting discovery.")
            return []

        logger.info("Starting Wyze device discovery…")
        try:
            records = await asyncio.to_thread(self._sync_discover, creds)
        except Exception as exc:
            logger.error("Wyze discovery failed: %s", exc, exc_info=True)
            return []

        logger.info(
            "Wyze discovery complete — %d device(s): %s",
            len(records), [r.name for r in records],
        )
        return records

    def _sync_discover(self, creds: Dict[str, str]) -> List[Any]:
        from ingestion.device_registry import DeviceRecord
        from wyze_sdk.errors import WyzeApiError

        try:
            client = _get_client(creds)
            devices = list(client.devices_list())
        except WyzeApiError as exc:
            err = str(exc).lower()
            if any(kw in err for kw in ("token", "auth", "expired", "access")):
                logger.warning("Wyze token expired — clearing cache and retrying login.")
                _clear_client_cache()
                creds.pop("access_token", None)
                client = _get_client(creds)
                devices = list(client.devices_list())
            else:
                raise

        now = datetime.now(timezone.utc).isoformat()
        records: List[DeviceRecord] = []
        for device in devices:
            try:
                record = self._to_record(device, now)
                records.append(record)
                logger.info(
                    "Device registered — name=%r type=%r model=%r id=%s",
                    record.name, record.device_type, record.model, record.device_id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to convert Wyze device mac=%r: %s",
                    getattr(device, "mac", "unknown"), exc, exc_info=True,
                )
        return records

    def _to_record(self, device: Any, now: str) -> Any:
        from ingestion.device_registry import DeviceRecord

        mac: str = device.mac
        name: str = getattr(device, "nickname", None) or mac
        model: str = str(getattr(device, "product_model", "") or "")
        # product_type may be a str-enum (DeviceTypes) or a plain str
        raw_type = getattr(device, "product_type", "") or ""
        product_type: str = str(raw_type)
        is_online: bool = bool(getattr(device, "is_online", True))

        dw_type = _device_type(product_type)
        caps = _capabilities(product_type)
        device_id = f"wyze_{mac}"

        return DeviceRecord(
            device_id=device_id,
            provider=self.name,
            name=name,
            ip="",
            mac=mac,
            device_type=dw_type,
            model=model,
            capabilities=caps,
            fingerprint=_fingerprint(
                device_id=device_id, name=name, device_type=dw_type, model=model,
            ),
            status="active" if is_online else "offline",
            last_seen=now,
            last_synced=now,
            sync_mode="",
            provider_meta={
                "mac": mac,
                "model": model,
                "product_type": product_type,
            },
        )
