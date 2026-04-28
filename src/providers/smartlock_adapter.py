"""
SmartLock adapter — generic REST-based lock/unlock for any HTTP-accessible
smart lock or local home-automation hub (Home Assistant, Hubitat, SmartThings
Maker API, etc.).

Device registry fields used:
    ip              — host or IP of the lock's local API (e.g. "192.168.1.50")
    provider_meta:
        api_url     — (optional) full base URL, overrides ip
                      e.g. "https://api.example.com/v1/locks/abc123"
        api_key     — (optional) Bearer token / API key for authentication
        api_header  — (optional) custom header name for the key
                      defaults to "Authorization" (value: "Bearer <api_key>")

REST contract expected from the lock endpoint:

    GET  {base}/status          → 200  {"state": "locked"|"unlocked",
                                        "battery": <0-100>, ...}
    POST {base}/lock            → 2xx  (body ignored)
    POST {base}/unlock          → 2xx  (body ignored)

Supported device types: SmartLock
Supported actions:      lock, unlock, toggle, get_status
"""

import logging
from typing import Any, Dict, List

from providers.base import BaseDeviceProvider, ProviderError

logger = logging.getLogger(__name__)


def _base_url(device: Dict[str, Any]) -> str:
    meta = device.get("provider_meta", {})
    if meta.get("api_url"):
        return meta["api_url"].rstrip("/")
    ip = device.get("ip", "").strip()
    if not ip:
        raise ProviderError(device["id"], "SmartLock: no 'ip' or 'provider_meta.api_url' set.")
    return f"http://{ip}"


def _auth_headers(device: Dict[str, Any]) -> Dict[str, str]:
    meta = device.get("provider_meta", {})
    key = meta.get("api_key", "")
    if not key:
        return {}
    header_name = meta.get("api_header", "Authorization")
    value = f"Bearer {key}" if header_name == "Authorization" else key
    return {header_name: value}


class SmartLockAdapter(BaseDeviceProvider):
    """
    Protocol adapter for SmartLock devices.

    Communicates with any HTTP endpoint that follows the lock/unlock REST
    contract.  New lock integrations (August, Yale, Schlage, etc.) can be
    added by pointing api_url at their gateway and storing the api_key in
    provider_meta — no code changes required.
    """

    @classmethod
    def supported_device_types(cls) -> List[str]:
        return ["SmartLock"]

    async def execute(
        self,
        device: Dict[str, Any],
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        import aiohttp

        device_id = device["id"]
        try:
            base = _base_url(device)
            headers = _auth_headers(device)
            async with aiohttp.ClientSession() as session:
                return await self._dispatch(session, base, headers, device_id, action)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(device_id, f"SmartLock action '{action}' failed: {exc}") from exc

    async def _dispatch(
        self,
        session: Any,
        base: str,
        headers: Dict[str, str],
        device_id: str,
        action: str,
    ) -> Dict[str, Any]:

        if action == "get_status":
            return await self._get_status(session, base, headers, device_id)

        if action == "toggle":
            status = await self._get_status(session, base, headers, device_id)
            action = "unlock" if status.get("state") == "locked" else "lock"

        if action == "lock":
            await self._post(session, f"{base}/lock", headers, device_id)
            return {"state": "locked", "changed": True}

        if action == "unlock":
            await self._post(session, f"{base}/unlock", headers, device_id)
            return {"state": "unlocked", "changed": True}

        raise ValueError(f"Unknown SmartLock action '{action}'.")

    async def _get_status(
        self,
        session: Any,
        base: str,
        headers: Dict[str, str],
        device_id: str,
    ) -> Dict[str, Any]:
        url = f"{base}/status"
        async with session.get(url, headers=headers) as resp:
            if resp.status >= 400:
                raise ProviderError(
                    device_id, f"SmartLock status endpoint returned {resp.status}."
                )
            data = await resp.json(content_type=None)

        result: Dict[str, Any] = {"changed": False}
        state = data.get("state", "")
        if state:
            result["state"] = state
        if data.get("battery") is not None:
            result["battery"] = data["battery"]
        if data.get("door_state"):
            result["door_state"] = data["door_state"]
        return result

    async def _post(
        self,
        session: Any,
        url: str,
        headers: Dict[str, str],
        device_id: str,
    ) -> None:
        async with session.post(url, headers=headers) as resp:
            if resp.status >= 400:
                raise ProviderError(
                    device_id, f"SmartLock POST to '{url}' returned {resp.status}."
                )
