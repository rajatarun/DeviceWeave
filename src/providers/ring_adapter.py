"""
Ring protocol adapter — cloud passthrough via Ring REST API.

Credentials loaded from Secrets Manager (RING_SECRET_ARN):
    {"email": "user@example.com", "password": "secret", "hardware_id": "<uuid>"}

For 2FA-enabled accounts (Ring enforces 2FA), include a refresh_token obtained
via the ingestion /ingest 2FA flow:
    {"email": "...", "password": "...", "hardware_id": "...", "refresh_token": "..."}

Access tokens are cached per Lambda container.  On expiry ring_doorbell Auth
exchanges the refresh_token automatically — no interactive 2FA needed again.
"""

import json
import logging
import os
import uuid as _uuid_mod
from typing import Any, Dict, List, Optional

from providers.base import BaseDeviceProvider, ProviderError

logger = logging.getLogger(__name__)

_RING_SECRET_ARN: str = os.environ.get("RING_SECRET_ARN", "")
_RING_API_URL = "https://api.ring.com/clients_api"
_USER_AGENT = "android:com.ringapp:2.0.67(423)"

_cred_cache: Optional[Dict[str, str]] = None
_token_cache: Optional[Dict[str, Any]] = None


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


def _persist_refresh_token(new_token: str, creds: Dict[str, str]) -> None:
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


async def _ensure_token(creds: Dict[str, str]) -> str:
    """Return a valid Ring access token via ring_doorbell Auth.

    Uses the stored refresh_token to exchange for an access_token silently —
    no 2FA interaction required after the initial ingestion setup.
    """
    global _token_cache
    if _token_cache and _token_cache.get("access_token"):
        return _token_cache["access_token"]

    from ring_doorbell import Auth
    from ring_doorbell.exceptions import AuthenticationError, Requires2FAError

    hw_id = _hardware_id(creds)
    refresh_tok = (_token_cache or {}).get("refresh_token") or creds.get("refresh_token")

    def _token_updater(new_token: Dict[str, Any]) -> None:
        global _token_cache
        _token_cache = new_token
        _persist_refresh_token(new_token.get("refresh_token", ""), creds)

    if not refresh_tok:
        raise ProviderError(
            "ring",
            "No Ring refresh_token available. Run POST /ingest with provider=ring "
            "to complete the 2FA setup and store a token.",
        )

    auth = Auth(
        "DeviceWeave/1.0",
        token={"refresh_token": refresh_tok},
        token_updater=_token_updater,
        hardware_id=hw_id,
    )
    try:
        new_token = await auth.async_refresh_tokens()
        _token_updater(new_token)
        logger.debug("Ring access token acquired (prefix=%s…).", new_token.get("access_token", "")[:8])
        return new_token["access_token"]
    except (AuthenticationError, Requires2FAError) as exc:
        raise ProviderError(
            "ring",
            "Ring refresh_token expired. Run POST /ingest with provider=ring to re-authenticate.",
        ) from exc
    finally:
        session = getattr(auth, "_session", None)
        if session is not None and not session.closed:
            await session.close()


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
            token = await _ensure_token(creds)
            hw_id = _hardware_id(creds)
            async with aiohttp.ClientSession() as session:
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
