"""
Kasa protocol adapter — cloud passthrough via Kasa REST API.

All device commands are sent through wap.tplinkcloud.com using the
passthrough API. Credentials are loaded from Secrets Manager (KASA_SECRET_ARN)
and the session token is cached per Lambda container.
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
        raise ProviderError("kasa", "KASA_SECRET_ARN not set.")
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
    logger.info("Kasa cloud token acquired.")
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


async def _sysinfo(session: Any, token: str, device_id: str) -> Dict:
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
        import aiohttp

        device_id = device["id"]
        device_type = device.get("device_type", "SmartPlug")

        try:
            async with aiohttp.ClientSession() as session:
                token = await _get_token(session)
                return await self._run(session, token, device_id, device_type, action, params)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(device_id, f"Cloud action '{action}' failed: {exc}") from exc

    async def _run(
        self,
        session: Any,
        token: str,
        device_id: str,
        device_type: str,
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:

        if action == "get_status":
            info = await _sysinfo(session, token, device_id)
            is_on = info.get("relay_state") == 1 or \
                    info.get("light_state", {}).get("on_off") == 1
            result: Dict[str, Any] = {
                "state": "on" if is_on else "off",
                "alias": info.get("alias", ""),
                "model": info.get("model", ""),
                "changed": False,
            }
            if device_type == "SmartBulb":
                result["brightness"] = info.get("light_state", {}).get("brightness")
            return result

        if action == "toggle":
            info = await _sysinfo(session, token, device_id)
            is_on = info.get("relay_state") == 1 or \
                    info.get("light_state", {}).get("on_off") == 1
            action = "turn_off" if is_on else "turn_on"

        if action == "turn_on":
            await _passthrough(session, token, device_id, _on_cmd(device_type))
            return {"state": "on", "changed": True}

        if action == "turn_off":
            await _passthrough(session, token, device_id, _off_cmd(device_type))
            return {"state": "off", "changed": True}

        if action == "set_brightness":
            if device_type != "SmartBulb":
                raise ValueError(f"set_brightness requires SmartBulb, got '{device_type}'.")
            target = int(params.get("brightness", 100))
            await _passthrough(session, token, device_id, {
                "smartlife.iot.smartbulb.lightingservice": {
                    "transition_light_state": {
                        "on_off": 1, "brightness": target, "transition_period": 0,
                    }
                }
            })
            return {"brightness": target, "changed": True}

        raise ValueError(f"Unknown action '{action}'.")


def _on_cmd(device_type: str) -> Dict:
    if device_type == "SmartBulb":
        return {"smartlife.iot.smartbulb.lightingservice": {
            "transition_light_state": {"on_off": 1, "transition_period": 0}
        }}
    return {"system": {"set_relay_state": {"state": 1}}}


def _off_cmd(device_type: str) -> Dict:
    if device_type == "SmartBulb":
        return {"smartlife.iot.smartbulb.lightingservice": {
            "transition_light_state": {"on_off": 0, "transition_period": 0}
        }}
    return {"system": {"set_relay_state": {"state": 0}}}
