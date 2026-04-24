"""
Kasa protocol adapter — local LAN for devices with IP, cloud passthrough for the rest.

Local path (ip set): python-kasa direct TCP connection, idempotency checked.
Cloud path (ip empty): Kasa cloud passthrough API over HTTPS, works from Lambda
  without LAN access. Credentials loaded from Secrets Manager (KASA_SECRET_ARN).
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

from providers.base import BaseDeviceProvider, ProviderError

logger = logging.getLogger(__name__)

_KASA_SECRET_ARN: str = os.environ.get("KASA_SECRET_ARN", "")
_KASA_CLOUD_URL = "https://wap.tplinkcloud.com"
_APP_TYPE = "Kasa_Android"
_TERMINAL_UUID = "00000000-0000-0000-0000-000000000000"

_cred_cache: Optional[Dict[str, str]] = None
_token_cache: Optional[str] = None


def _get_credentials() -> Dict[str, str]:
    global _cred_cache
    if _cred_cache:
        return _cred_cache
    if not _KASA_SECRET_ARN:
        raise ProviderError("kasa", "KASA_SECRET_ARN not set — cannot authenticate with Kasa cloud.")
    import boto3
    resp = boto3.client("secretsmanager").get_secret_value(SecretId=_KASA_SECRET_ARN)
    _cred_cache = json.loads(resp["SecretString"])
    return _cred_cache


async def _get_token(session: Any) -> str:
    global _token_cache
    if _token_cache:
        return _token_cache
    creds = _get_credentials()
    async with session.post(_KASA_CLOUD_URL, json={
        "method": "login",
        "params": {
            "appType": _APP_TYPE,
            "cloudPassword": creds["password"],
            "cloudUserName": creds["email"],
            "terminalUUID": _TERMINAL_UUID,
        },
    }) as resp:
        data = await resp.json(content_type=None)
    if data.get("error_code", 0) != 0:
        raise ProviderError("kasa", f"Cloud login failed: {data.get('msg', data)}")
    _token_cache = data["result"]["token"]
    return _token_cache


async def _passthrough(session: Any, token: str, device_id: str, command: Dict) -> Dict:
    url = f"{_KASA_CLOUD_URL}?token={token}"
    async with session.post(url, json={
        "method": "passthrough",
        "params": {"deviceId": device_id, "requestData": json.dumps(command)},
    }) as resp:
        data = await resp.json(content_type=None)
    if data.get("error_code", 0) != 0:
        raise ProviderError(device_id, f"Passthrough error: {data.get('msg', data)}")
    return json.loads(data["result"]["responseData"])


async def _cloud_sysinfo(session: Any, token: str, device_id: str) -> Dict:
    result = await _passthrough(session, token, device_id, {"system": {"get_sysinfo": {}}})
    return result.get("system", {}).get("get_sysinfo", {})


class KasaAdapter(BaseDeviceProvider):

    @classmethod
    def supported_device_types(cls) -> List[str]:
        return ["SmartPlug", "SmartBulb"]

    async def execute(
        self,
        device: Dict[str, Any],
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        ip: str = device.get("ip", "")
        if ip:
            return await self._execute_local(device, action, params)
        return await self._execute_cloud(device, action, params)

    # ------------------------------------------------------------------
    # Local LAN path
    # ------------------------------------------------------------------

    async def _execute_local(
        self, device: Dict[str, Any], action: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        from kasa import SmartBulb, SmartPlug

        ip = device["ip"]
        device_type = device.get("device_type", "SmartPlug")
        try:
            kasa_device = SmartBulb(ip) if device_type == "SmartBulb" else SmartPlug(ip)
            await kasa_device.update()
        except Exception as exc:
            raise ProviderError(device["id"], f"Failed to connect to {ip}: {exc}") from exc

        try:
            if action == "turn_on":
                return await self._turn_on(kasa_device)
            if action == "turn_off":
                return await self._turn_off(kasa_device)
            if action == "toggle":
                return await self._toggle(kasa_device)
            if action == "get_status":
                return self._get_status(kasa_device, device_type)
            if action == "set_brightness":
                if device_type != "SmartBulb":
                    raise ValueError(f"set_brightness requires SmartBulb, got '{device_type}'.")
                return await self._set_brightness(kasa_device, params)
            raise ValueError(f"Unknown action '{action}'.")
        except ValueError:
            raise
        except Exception as exc:
            raise ProviderError(device["id"], f"Action '{action}' failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Cloud passthrough path
    # ------------------------------------------------------------------

    async def _execute_cloud(
        self, device: Dict[str, Any], action: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        import aiohttp

        device_id = device["id"]
        device_type = device.get("device_type", "SmartPlug")

        try:
            async with aiohttp.ClientSession() as session:
                token = await _get_token(session)
                return await self._cloud_action(session, token, device_id, device_type, action, params)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(device_id, f"Cloud action '{action}' failed: {exc}") from exc

    async def _cloud_action(
        self,
        session: Any,
        token: str,
        device_id: str,
        device_type: str,
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        if action == "get_status":
            info = await _cloud_sysinfo(session, token, device_id)
            result: Dict[str, Any] = {
                "state": "on" if info.get("relay_state") == 1 or
                         info.get("light_state", {}).get("on_off") == 1 else "off",
                "alias": info.get("alias", ""),
                "model": info.get("model", ""),
                "changed": False,
            }
            if device_type == "SmartBulb":
                result["brightness"] = info.get("light_state", {}).get("brightness")
            return result

        if action == "toggle":
            info = await _cloud_sysinfo(session, token, device_id)
            is_on = info.get("relay_state") == 1 or info.get("light_state", {}).get("on_off") == 1
            action = "turn_off" if is_on else "turn_on"

        if action == "turn_on":
            cmd = _on_command(device_type)
            await _passthrough(session, token, device_id, cmd)
            return {"state": "on", "changed": True}

        if action == "turn_off":
            cmd = _off_command(device_type)
            await _passthrough(session, token, device_id, cmd)
            return {"state": "off", "changed": True}

        if action == "set_brightness":
            if device_type != "SmartBulb":
                raise ValueError(f"set_brightness requires SmartBulb, got '{device_type}'.")
            target = int(params.get("brightness", 100))
            cmd = {"smartlife.iot.smartbulb.lightingservice": {
                "transition_light_state": {"on_off": 1, "brightness": target, "transition_period": 0}
            }}
            await _passthrough(session, token, device_id, cmd)
            return {"brightness": target, "changed": True}

        raise ValueError(f"Unknown action '{action}'.")

    # ------------------------------------------------------------------
    # Local action helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _turn_on(device: Any) -> Dict[str, Any]:
        if device.is_on:
            return {"state": "on", "changed": False, "message": "Already on."}
        await device.turn_on()
        return {"state": "on", "changed": True}

    @staticmethod
    async def _turn_off(device: Any) -> Dict[str, Any]:
        if device.is_off:
            return {"state": "off", "changed": False, "message": "Already off."}
        await device.turn_off()
        return {"state": "off", "changed": True}

    @staticmethod
    async def _toggle(device: Any) -> Dict[str, Any]:
        if device.is_on:
            await device.turn_off()
            return {"state": "off", "changed": True}
        await device.turn_on()
        return {"state": "on", "changed": True}

    @staticmethod
    def _get_status(device: Any, device_type: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "state": "on" if device.is_on else "off",
            "alias": device.alias,
            "model": device.model,
            "changed": False,
        }
        if device_type == "SmartBulb":
            result["brightness"] = device.brightness
        return result

    @staticmethod
    async def _set_brightness(bulb: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        if "brightness" not in params:
            raise ValueError("set_brightness requires a 'brightness' parameter (0–100).")
        target = int(params["brightness"])
        if bulb.brightness == target:
            return {"brightness": target, "changed": False, "message": f"Already at {target}%."}
        await bulb.set_brightness(target)
        return {"brightness": target, "changed": True}


# ---------------------------------------------------------------------------
# Cloud command builders
# ---------------------------------------------------------------------------

def _on_command(device_type: str) -> Dict:
    if device_type == "SmartBulb":
        return {"smartlife.iot.smartbulb.lightingservice": {
            "transition_light_state": {"on_off": 1, "transition_period": 0}
        }}
    return {"system": {"set_relay_state": {"state": 1}}}


def _off_command(device_type: str) -> Dict:
    if device_type == "SmartBulb":
        return {"smartlife.iot.smartbulb.lightingservice": {
            "transition_light_state": {"on_off": 0, "transition_period": 0}
        }}
    return {"system": {"set_relay_state": {"state": 0}}}
