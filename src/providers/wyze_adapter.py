"""
Wyze protocol adapter — cloud passthrough via wyze-sdk.

Credentials loaded from Secrets Manager (WYZE_SECRET_ARN):
    {"email": "user@example.com", "password": "secret",
     "key_id": "<wyze_developer_key_id>", "api_key": "<wyze_developer_api_key>"}

For TOTP 2FA accounts:
    {"...", "totp_key": "<base32_totp_secret>"}

An access_token is cached per Lambda container.  On auth errors the adapter
re-logins automatically — no interactive 2FA needed after initial setup.

All wyze-sdk calls are synchronous; they are dispatched via asyncio.to_thread
so they don't block the event loop.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

from providers.base import BaseDeviceProvider, ProviderError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSL alignment — see wyze_discovery.py for rationale
# ---------------------------------------------------------------------------

def _configure_ssl() -> None:
    if os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE"):
        return
    for path in (
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/ssl/certs/ca-bundle.crt",
        "/etc/ssl/certs/ca-certificates.crt",
    ):
        if os.path.exists(path):
            os.environ["REQUESTS_CA_BUNDLE"] = path
            return
    try:
        import certifi
        os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
    except ImportError:
        pass

_configure_ssl()

_WYZE_SECRET_ARN: str = os.environ.get("WYZE_SECRET_ARN", "")

_cred_cache: Optional[Dict[str, str]] = None
_client_cache: Optional[Any] = None  # wyze_sdk.Client


def _get_credentials() -> Dict[str, str]:
    global _cred_cache
    if _cred_cache:
        return _cred_cache
    if not _WYZE_SECRET_ARN:
        raise ProviderError("wyze", "WYZE_SECRET_ARN not set.")
    import boto3
    try:
        resp = boto3.client("secretsmanager").get_secret_value(SecretId=_WYZE_SECRET_ARN)
        _cred_cache = json.loads(resp["SecretString"])
        return _cred_cache
    except Exception as exc:
        raise ProviderError("wyze", f"Failed to load Wyze credentials: {exc}") from exc


def _build_client(creds: Dict[str, str]) -> Any:
    from wyze_sdk import Client

    access_token = creds.get("access_token")
    if access_token:
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
        raise ProviderError("wyze", "Wyze login returned no access_token.")
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


class WyzeAdapter(BaseDeviceProvider):

    @classmethod
    def supported_device_types(cls) -> List[str]:
        return [
            "WyzeBulb",
            "WyzePlug",
            "WyzeSwitch",
            "WyzeCamera",
            "WyzeLock",
            "WyzeMotionSensor",
            "WyzeContactSensor",
            "WyzeThermostat",
        ]

    async def execute(
        self,
        device: Dict[str, Any],
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        import asyncio

        device_id = device["id"]
        device_type = device.get("device_type", "WyzeDevice")
        meta = device.get("provider_meta", {})
        device_mac = meta.get("mac") or device.get("mac", "")
        device_model = meta.get("model") or device.get("model", "")

        if not device_mac:
            raise ProviderError(device_id, "Missing device MAC in provider_meta.")

        try:
            creds = _get_credentials()
            return await asyncio.to_thread(
                self._sync_execute,
                creds, device_id, device_mac, device_model, device_type, action, params,
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(device_id, f"Wyze action '{action}' failed: {exc}") from exc

    def _sync_execute(
        self,
        creds: Dict[str, str],
        device_id: str,
        device_mac: str,
        device_model: str,
        device_type: str,
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        from wyze_sdk.errors import WyzeApiError

        try:
            client = _get_client(creds)
            return self._dispatch(
                client, device_id, device_mac, device_model, device_type, action, params,
            )
        except WyzeApiError as exc:
            err = str(exc).lower()
            if any(kw in err for kw in ("token", "auth", "expired", "access")):
                logger.warning("Wyze token expired — clearing cache and retrying.")
                _clear_client_cache()
                creds.pop("access_token", None)
                client = _get_client(creds)
                return self._dispatch(
                    client, device_id, device_mac, device_model, device_type, action, params,
                )
            raise ProviderError(device_id, f"Wyze API error: {exc}") from exc

    def _dispatch(
        self,
        client: Any,
        device_id: str,
        device_mac: str,
        device_model: str,
        device_type: str,
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        if device_type == "WyzeBulb":
            return self._bulb_action(client, device_id, device_mac, device_model, action, params)

        if device_type == "WyzePlug":
            return self._plug_action(client, device_id, device_mac, device_model, action, params)

        if device_type == "WyzeSwitch":
            return self._switch_action(client, device_id, device_mac, device_model, action, params)

        if device_type == "WyzeLock":
            return self._lock_action(client, device_id, device_mac, action)

        if device_type in ("WyzeCamera", "WyzeMotionSensor", "WyzeContactSensor", "WyzeThermostat"):
            if action == "get_status":
                return self._generic_status(client, device_id, device_mac, device_model, device_type)
            raise ValueError(f"Action '{action}' not supported for '{device_type}'.")

        raise ValueError(f"Unsupported device_type '{device_type}'.")

    # ------------------------------------------------------------------
    # Bulb
    # ------------------------------------------------------------------

    def _bulb_action(
        self,
        client: Any,
        device_id: str,
        device_mac: str,
        device_model: str,
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        if action == "turn_on":
            client.bulbs.turn_on(device_mac=device_mac, device_model=device_model)
            return {"state": "on", "changed": True}

        if action == "turn_off":
            client.bulbs.turn_off(device_mac=device_mac, device_model=device_model)
            return {"state": "off", "changed": True}

        if action == "toggle":
            info = client.bulbs.info(device_mac=device_mac, device_model=device_model)
            if info and info.is_on:
                client.bulbs.turn_off(device_mac=device_mac, device_model=device_model)
                return {"state": "off", "changed": True}
            client.bulbs.turn_on(device_mac=device_mac, device_model=device_model)
            return {"state": "on", "changed": True}

        if action == "set_brightness":
            brightness = max(1, min(100, int(params.get("brightness", 100))))
            client.bulbs.set_brightness(
                device_mac=device_mac, device_model=device_model, brightness=brightness,
            )
            return {"brightness": brightness, "changed": True}

        if action == "set_color":
            color = str(params.get("color", "ffffff")).lstrip("#")
            client.bulbs.set_color(
                device_mac=device_mac, device_model=device_model, color=color,
            )
            return {"color": color, "changed": True}

        if action == "set_color_temp":
            color_temp = int(params.get("color_temp", 4000))
            client.bulbs.set_color_temp(
                device_mac=device_mac, device_model=device_model, color_temp=color_temp,
            )
            return {"color_temp": color_temp, "changed": True}

        if action == "get_status":
            info = client.bulbs.info(device_mac=device_mac, device_model=device_model)
            result: Dict[str, Any] = {"changed": False}
            if info:
                result["state"] = "on" if info.is_on else "off"
                result["online"] = bool(getattr(info, "is_online", True))
                if getattr(info, "brightness", None) is not None:
                    result["brightness"] = info.brightness
                if getattr(info, "color", None) is not None:
                    result["color"] = info.color
                if getattr(info, "color_temp", None) is not None:
                    result["color_temp"] = info.color_temp
            return result

        raise ValueError(f"Unknown bulb action '{action}'.")

    # ------------------------------------------------------------------
    # Plug
    # ------------------------------------------------------------------

    def _plug_action(
        self,
        client: Any,
        device_id: str,
        device_mac: str,
        device_model: str,
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        if action == "turn_on":
            client.plugs.turn_on(device_mac=device_mac, device_model=device_model)
            return {"state": "on", "changed": True}

        if action == "turn_off":
            client.plugs.turn_off(device_mac=device_mac, device_model=device_model)
            return {"state": "off", "changed": True}

        if action == "toggle":
            info = client.plugs.info(device_mac=device_mac, device_model=device_model)
            if info and info.is_on:
                client.plugs.turn_off(device_mac=device_mac, device_model=device_model)
                return {"state": "off", "changed": True}
            client.plugs.turn_on(device_mac=device_mac, device_model=device_model)
            return {"state": "on", "changed": True}

        if action == "get_status":
            info = client.plugs.info(device_mac=device_mac, device_model=device_model)
            result: Dict[str, Any] = {"changed": False}
            if info:
                result["state"] = "on" if info.is_on else "off"
                result["online"] = bool(getattr(info, "is_online", True))
            return result

        raise ValueError(f"Unknown plug action '{action}'.")

    # ------------------------------------------------------------------
    # Switch
    # ------------------------------------------------------------------

    def _switch_action(
        self,
        client: Any,
        device_id: str,
        device_mac: str,
        device_model: str,
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        if action == "turn_on":
            client.switches.turn_on(device_mac=device_mac, device_model=device_model)
            return {"state": "on", "changed": True}

        if action == "turn_off":
            client.switches.turn_off(device_mac=device_mac, device_model=device_model)
            return {"state": "off", "changed": True}

        if action == "toggle":
            info = client.switches.info(device_mac=device_mac, device_model=device_model)
            if info and info.is_on:
                client.switches.turn_off(device_mac=device_mac, device_model=device_model)
                return {"state": "off", "changed": True}
            client.switches.turn_on(device_mac=device_mac, device_model=device_model)
            return {"state": "on", "changed": True}

        if action == "get_status":
            info = client.switches.info(device_mac=device_mac, device_model=device_model)
            result: Dict[str, Any] = {"changed": False}
            if info:
                result["state"] = "on" if info.is_on else "off"
                result["online"] = bool(getattr(info, "is_online", True))
            return result

        raise ValueError(f"Unknown switch action '{action}'.")

    # ------------------------------------------------------------------
    # Lock
    # ------------------------------------------------------------------

    def _lock_action(
        self,
        client: Any,
        device_id: str,
        device_mac: str,
        action: str,
    ) -> Dict[str, Any]:
        if action == "lock":
            client.locks.lock(device_mac=device_mac)
            return {"state": "locked", "changed": True}

        if action == "unlock":
            client.locks.unlock(device_mac=device_mac)
            return {"state": "unlocked", "changed": True}

        if action == "get_status":
            info = client.locks.info(device_mac=device_mac)
            result: Dict[str, Any] = {"changed": False}
            if info:
                is_locked = bool(getattr(info, "is_locked", False))
                result["state"] = "locked" if is_locked else "unlocked"
                result["online"] = bool(getattr(info, "is_online", True))
            return result

        raise ValueError(f"Unknown lock action '{action}'.")

    # ------------------------------------------------------------------
    # Camera / sensor / thermostat — read-only status
    # ------------------------------------------------------------------

    def _generic_status(
        self,
        client: Any,
        device_id: str,
        device_mac: str,
        device_model: str,
        device_type: str,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {"changed": False}
        try:
            if device_type == "WyzeCamera":
                info = client.cameras.info(device_mac=device_mac, device_model=device_model)
                if info:
                    result["online"] = bool(getattr(info, "is_online", True))
            elif device_type == "WyzeThermostat":
                info = client.thermostats.info(device_mac=device_mac, device_model=device_model)
                if info:
                    result["online"] = bool(getattr(info, "is_online", True))
                    if getattr(info, "temperature", None) is not None:
                        result["temperature"] = info.temperature
                    if getattr(info, "cool_sp", None) is not None:
                        result["cool_setpoint"] = info.cool_sp
                    if getattr(info, "heat_sp", None) is not None:
                        result["heat_setpoint"] = info.heat_sp
        except Exception as exc:
            logger.warning(
                "Could not fetch status for %s (%s): %s", device_id, device_type, exc,
            )
        return result
