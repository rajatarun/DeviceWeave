"""
MyQ protocol adapter — garage doors and lamps via pymyq.

Credentials are loaded from Secrets Manager (MYQ_SECRET_ARN):
    {"email": "user@example.com", "password": "secret"}

The pymyq client is authenticated fresh per Lambda invocation using an
aiohttp ClientSession. The MyQ device is located by the `myq_device_id`
field in provider_meta (the hardware serial stored at discovery time).

Supported device types:
  MyQGarageDoor — actions: get_status, open, close, toggle
  MyQLamp       — actions: get_status, turn_on, turn_off, toggle
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

from providers.base import BaseDeviceProvider, ProviderError

logger = logging.getLogger(__name__)

_MYQ_SECRET_ARN: str = os.environ.get("MYQ_SECRET_ARN", "")

_cred_cache: Optional[Dict[str, str]] = None

# States reported by pymyq for covers
_OPEN_STATES = {"open", "partial_open"}
_CLOSED_STATES = {"closed"}


def _get_credentials() -> Dict[str, str]:
    global _cred_cache
    if _cred_cache:
        return _cred_cache
    if not _MYQ_SECRET_ARN:
        raise ProviderError("myq", "MYQ_SECRET_ARN not set.")
    import boto3
    resp = boto3.client("secretsmanager").get_secret_value(SecretId=_MYQ_SECRET_ARN)
    _cred_cache = json.loads(resp["SecretString"])
    return _cred_cache


async def _login(websession: Any) -> Any:
    import pymyq
    creds = _get_credentials()
    try:
        myq = await pymyq.login(creds["email"], creds["password"], websession)
        await myq.update_device_info()
        return myq
    except Exception as exc:
        raise ProviderError("myq", f"MyQ login failed: {exc}") from exc


def _lookup_device(myq: Any, myq_device_id: str, device_id: str) -> Any:
    device = myq.devices.get(myq_device_id)
    if device is None:
        known = ", ".join(myq.devices.keys()) or "(none)"
        raise ProviderError(
            device_id,
            f"MyQ device '{myq_device_id}' not found in account. Known: {known}",
        )
    return device


class MyQAdapter(BaseDeviceProvider):

    @classmethod
    def supported_device_types(cls) -> List[str]:
        return ["MyQGarageDoor", "MyQLamp"]

    async def execute(
        self,
        device: Dict[str, Any],
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        import aiohttp

        device_id = device["id"]
        device_type = device.get("device_type", "MyQGarageDoor")
        myq_device_id = device.get("provider_meta", {}).get("myq_device_id", device_id)

        try:
            async with aiohttp.ClientSession() as websession:
                myq = await _login(websession)
                myq_device = _lookup_device(myq, myq_device_id, device_id)
                if device_type == "MyQGarageDoor":
                    return await self._run_garage(myq, myq_device, device_id, action)
                if device_type == "MyQLamp":
                    return await self._run_lamp(myq, myq_device, device_id, action)
                raise ValueError(f"Unsupported device_type '{device_type}'.")
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(device_id, f"MyQ action '{action}' failed: {exc}") from exc

    async def _run_garage(
        self,
        myq: Any,
        myq_device: Any,
        device_id: str,
        action: str,
    ) -> Dict[str, Any]:
        if action == "get_status":
            return _garage_status(myq_device)

        if action == "toggle":
            state = myq_device.state
            action = "close" if state in _OPEN_STATES else "open"

        if action == "open":
            if not myq_device.open_allowed:
                raise ProviderError(device_id, "MyQ: open not currently allowed for this door.")
            await myq_device.open()
            return {"state": "opening", "changed": True}

        if action == "close":
            if not myq_device.close_allowed:
                raise ProviderError(device_id, "MyQ: close not currently allowed for this door.")
            await myq_device.close()
            return {"state": "closing", "changed": True}

        raise ValueError(f"Unknown action '{action}' for MyQGarageDoor.")

    async def _run_lamp(
        self,
        myq: Any,
        myq_device: Any,
        device_id: str,
        action: str,
    ) -> Dict[str, Any]:
        if action == "get_status":
            is_on = myq_device.state == "on"
            return {
                "state": "on" if is_on else "off",
                "name": myq_device.name,
                "online": myq_device.online,
                "changed": False,
            }

        if action == "toggle":
            action = "turn_off" if myq_device.state == "on" else "turn_on"

        if action == "turn_on":
            await myq_device.turnon()
            return {"state": "on", "changed": True}

        if action == "turn_off":
            await myq_device.turnoff()
            return {"state": "off", "changed": True}

        raise ValueError(f"Unknown action '{action}' for MyQLamp.")


def _garage_status(myq_device: Any) -> Dict[str, Any]:
    state: str = myq_device.state or "unknown"
    result: Dict[str, Any] = {
        "state": state,
        "name": myq_device.name,
        "online": myq_device.online,
        "changed": False,
    }
    if myq_device.low_battery is not None:
        result["low_battery"] = myq_device.low_battery
    return result
