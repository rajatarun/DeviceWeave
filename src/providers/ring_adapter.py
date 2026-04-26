"""
Ring protocol adapter — cloud passthrough via Ring REST API.

Credentials loaded from Secrets Manager (RING_SECRET_ARN):
    {"email": "user@example.com", "password": "secret", "hardware_id": "<uuid>"}

For 2FA-enabled accounts (Ring enforces 2FA), also include a pre-obtained
refresh_token to skip the interactive challenge:
    {"email": "...", "password": "...", "hardware_id": "...", "refresh_token": "..."}

Access tokens are cached per Lambda container and refreshed on expiry.
"""

import json
import logging
import os
import uuid as _uuid_mod
from typing import Any, Dict, List, Optional

from providers.base import BaseDeviceProvider, ProviderError

logger = logging.getLogger(__name__)

_RING_SECRET_ARN: str = os.environ.get("RING_SECRET_ARN", "")
_RING_OAUTH_URL = "https://oauth.ring.com/oauth/token"
_RING_API_URL = "https://api.ring.com/clients_api"
_USER_AGENT = "android:com.ringapp:2.0.67(423)"

_cred_cache: Optional[Dict[str, str]] = None
_token_cache: Optional[Dict[str, str]] = None


def _get_credentials() -> Dict[str, str]:
    global _cred_cache
    if _cred_cache:
        return _cred_cache
    if not _RING_SECRET_ARN:
        raise ProviderError("ring", "RING_SECRET_ARN not set.")
    import boto3
    resp = boto3.client("secretsmanager").get_secret_value(SecretId=_RING_SECRET_ARN)
    _cred_cache = json.loads(resp["SecretString"])
    return _cred_cache


def _hardware_id(creds: Dict[str, str]) -> str:
    if creds.get("hardware_id"):
        return creds["hardware_id"]
    return str(_uuid_mod.uuid5(_uuid_mod.NAMESPACE_DNS, creds.get("email", "ring")))


async def _oauth_post(session: Any, hardware_id: str, form_data: Dict) -> Dict[str, Any]:
    headers = {
        "User-Agent": _USER_AGENT,
        "hardware_id": hardware_id,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    async with session.post(_RING_OAUTH_URL, headers=headers, data=form_data) as resp:
        if resp.status == 412:
            raise ProviderError(
                "ring",
                "Ring requires 2FA verification. Pre-authenticate externally and "
                "store the refresh_token in RING_SECRET_ARN.",
            )
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def _ensure_token(session: Any, creds: Dict[str, str]) -> str:
    global _token_cache
    if _token_cache and _token_cache.get("access_token"):
        return _token_cache["access_token"]

    hw_id = _hardware_id(creds)
    refresh_tok = (_token_cache or {}).get("refresh_token") or creds.get("refresh_token")

    if refresh_tok:
        token_data = await _oauth_post(session, hw_id, {
            "client_id": "ring_official_android",
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
            "scope": "client",
        })
    else:
        token_data = await _oauth_post(session, hw_id, {
            "client_id": "ring_official_android",
            "grant_type": "password",
            "username": creds["email"],
            "password": creds["password"],
            "scope": "client",
        })

    access_token = token_data.get("access_token")
    if not access_token:
        raise ProviderError("ring", f"Ring auth returned no access_token: {token_data}")

    _token_cache = {
        "access_token": access_token,
        "refresh_token": token_data.get("refresh_token") or refresh_tok or "",
    }
    logger.debug("Ring access token acquired (prefix=%s…).", access_token[:8])
    return access_token


def _api_headers(token: str, hardware_id: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": _USER_AGENT,
        "hardware_id": hardware_id,
        "Content-Type": "application/json",
    }


async def _api_put(
    session: Any, token: str, hw_id: str, path: str, body: Optional[Dict] = None
) -> Dict[str, Any]:
    url = f"{_RING_API_URL}/{path}"
    async with session.put(url, headers=_api_headers(token, hw_id), json=body or {}) as resp:
        resp.raise_for_status()
        if resp.content_length:
            return await resp.json(content_type=None)
        return {}


async def _api_get(session: Any, token: str, hw_id: str, path: str) -> Dict[str, Any]:
    url = f"{_RING_API_URL}/{path}"
    async with session.get(url, headers=_api_headers(token, hw_id)) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


class RingAdapter(BaseDeviceProvider):

    @classmethod
    def supported_device_types(cls) -> List[str]:
        return ["RingDoorbell", "RingCamera", "RingLight"]

    async def execute(
        self,
        device: Dict[str, Any],
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        import aiohttp

        device_id = device["id"]
        device_type = device.get("device_type", "RingCamera")
        meta = device.get("provider_meta", {})
        category = meta.get("category", "other")
        ring_id = meta.get("ring_id", device_id)

        try:
            creds = _get_credentials()
            async with aiohttp.ClientSession() as session:
                token = await _ensure_token(session, creds)
                hw_id = _hardware_id(creds)
                return await self._dispatch(
                    session, token, hw_id,
                    device_id, ring_id, category, device_type, action, params,
                )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(device_id, f"Ring action '{action}' failed: {exc}") from exc

    async def _dispatch(
        self,
        session: Any,
        token: str,
        hw_id: str,
        device_id: str,
        ring_id: str,
        category: str,
        device_type: str,
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:

        if action == "get_status":
            return await self._get_status(
                session, token, hw_id, ring_id, category, device_type
            )

        if action == "toggle":
            status = await self._get_status(
                session, token, hw_id, ring_id, category, device_type
            )
            next_action = "turn_off" if status.get("state") == "on" else "turn_on"
            return await self._dispatch(
                session, token, hw_id, device_id, ring_id,
                category, device_type, next_action, params,
            )

        if action == "turn_on":
            if device_type != "RingLight":
                raise ValueError(f"turn_on not supported for '{device_type}'.")
            await _api_put(session, token, hw_id, f"{category}/{ring_id}/floodlight_light_on")
            return {"state": "on", "changed": True}

        if action == "turn_off":
            if device_type != "RingLight":
                raise ValueError(f"turn_off not supported for '{device_type}'.")
            await _api_put(session, token, hw_id, f"{category}/{ring_id}/floodlight_light_off")
            return {"state": "off", "changed": True}

        if action == "set_brightness":
            if device_type != "RingLight":
                raise ValueError(f"set_brightness requires RingLight, got '{device_type}'.")
            target = max(0, min(100, int(params.get("brightness", 100))))
            await _api_put(
                session, token, hw_id,
                f"{category}/{ring_id}/brightness",
                {"brightness": target},
            )
            return {"brightness": target, "changed": True}

        raise ValueError(f"Unknown action '{action}'.")

    async def _get_status(
        self,
        session: Any,
        token: str,
        hw_id: str,
        ring_id: str,
        category: str,
        device_type: str,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {"changed": False}

        if device_type == "RingDoorbell":
            data = await _api_get(session, token, hw_id, f"doorbots/{ring_id}/health")
            h = data.get("device_health", {})
            result["state"] = "online" if h.get("network_connection_type") else "offline"
            if h.get("battery_percentage") is not None:
                result["battery"] = h["battery_percentage"]
            if h.get("wifi_signal_strength") is not None:
                result["wifi_rssi"] = h["wifi_signal_strength"]

        elif device_type == "RingCamera":
            data = await _api_get(session, token, hw_id, f"stickup_cams/{ring_id}/health")
            h = data.get("device_health", {})
            result["state"] = "online" if h.get("network_connection_type") else "offline"
            if h.get("battery_percentage") is not None:
                result["battery"] = h["battery_percentage"]
            if h.get("wifi_signal_strength") is not None:
                result["wifi_rssi"] = h["wifi_signal_strength"]

        else:  # RingLight
            data = await _api_get(session, token, hw_id, f"{category}/{ring_id}")
            desc = data.get("description", data)
            led_status = desc.get("led_status", "")
            is_on = led_status == "on" or bool(desc.get("is_on", False))
            result["state"] = "on" if is_on else "off"
            if desc.get("brightness") is not None:
                result["brightness"] = desc["brightness"]

        return result
