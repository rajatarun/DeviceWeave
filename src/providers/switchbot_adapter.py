"""
SwitchBot protocol adapter — cloud passthrough via SwitchBot REST API v1.1.

All device commands are sent through api.switch-bot.com using HMAC-SHA256
signed requests. Credentials are loaded from Secrets Manager (SWITCHBOT_SECRET_ARN):
    {"token": "...", "secret": "..."}
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from providers.base import BaseDeviceProvider, ProviderError

logger = logging.getLogger(__name__)

_SWITCHBOT_SECRET_ARN: str = os.environ.get("SWITCHBOT_SECRET_ARN", "")
_SWITCHBOT_API_URL = "https://api.switch-bot.com"

_cred_cache: Optional[Dict[str, str]] = None


def _get_credentials() -> Dict[str, str]:
    global _cred_cache
    if _cred_cache:
        return _cred_cache
    if not _SWITCHBOT_SECRET_ARN:
        raise ProviderError("switchbot", "SWITCHBOT_SECRET_ARN not set.")
    import boto3
    resp = boto3.client("secretsmanager").get_secret_value(SecretId=_SWITCHBOT_SECRET_ARN)
    _cred_cache = json.loads(resp["SecretString"])
    return _cred_cache


def _auth_headers(token: str, secret: str) -> Dict[str, str]:
    t = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    string_to_sign = f"{token}{t}{nonce}"
    sign = base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8").upper()
    return {
        "Authorization": token,
        "sign": sign,
        "t": t,
        "nonce": nonce,
        "Content-Type": "application/json",
    }


async def _send_command(
    session: Any,
    headers: Dict[str, str],
    device_id: str,
    command: str,
    parameter: Any = "default",
) -> Dict:
    url = f"{_SWITCHBOT_API_URL}/v1.1/devices/{device_id}/commands"
    body = {"commandType": "command", "command": command, "parameter": parameter}
    async with session.post(url, headers=headers, json=body) as resp:
        data = await resp.json(content_type=None)
    if data.get("statusCode") != 100:
        raise ProviderError(
            device_id,
            f"SwitchBot command error {data.get('statusCode')}: {data.get('message', data)}",
        )
    return data


async def _fetch_status(session: Any, headers: Dict[str, str], device_id: str) -> Dict:
    url = f"{_SWITCHBOT_API_URL}/v1.1/devices/{device_id}/status"
    async with session.get(url, headers=headers) as resp:
        data = await resp.json(content_type=None)
    if data.get("statusCode") != 100:
        raise ProviderError(
            device_id,
            f"SwitchBot status error {data.get('statusCode')}: {data.get('message', data)}",
        )
    return data.get("body", {})


class SwitchBotAdapter(BaseDeviceProvider):

    @classmethod
    def supported_device_types(cls) -> List[str]:
        return [
            "SwitchBotBulb",
            "SwitchBotPlug",
            "SwitchBotFan",
            "SwitchBotSwitch",
            "SwitchBotCurtain",
        ]

    async def execute(
        self,
        device: Dict[str, Any],
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        import aiohttp

        device_id = device["id"]
        device_type = device.get("device_type", "SwitchBotSwitch")

        try:
            creds = _get_credentials()
            headers = _auth_headers(creds["token"], creds["secret"])
            async with aiohttp.ClientSession() as session:
                return await self._run(session, headers, device_id, device_type, action, params)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(device_id, f"Cloud action '{action}' failed: {exc}") from exc

    async def _run(
        self,
        session: Any,
        headers: Dict[str, str],
        device_id: str,
        device_type: str,
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:

        if action == "get_status":
            body = await _fetch_status(session, headers, device_id)
            is_on = body.get("power", "").lower() == "on"
            result: Dict[str, Any] = {
                "state": "on" if is_on else "off",
                "changed": False,
            }
            if device_type == "SwitchBotBulb":
                result["brightness"] = body.get("brightness")
                result["color"] = body.get("color")
                result["color_temp"] = body.get("colorTemperature")
            return result

        if action == "toggle":
            await _send_command(session, headers, device_id, "toggle")
            return {"changed": True}

        if action == "turn_on":
            await _send_command(session, headers, device_id, "turnOn")
            return {"state": "on", "changed": True}

        if action == "turn_off":
            await _send_command(session, headers, device_id, "turnOff")
            return {"state": "off", "changed": True}

        if action == "set_brightness":
            if device_type != "SwitchBotBulb":
                raise ValueError(f"set_brightness requires SwitchBotBulb, got '{device_type}'.")
            target = int(params.get("brightness", 100))
            await _send_command(session, headers, device_id, "setBrightness", str(target))
            return {"brightness": target, "changed": True}

        if action == "set_color":
            if device_type != "SwitchBotBulb":
                raise ValueError(f"set_color requires SwitchBotBulb, got '{device_type}'.")
            color = params.get("color", "255:255:255")
            await _send_command(session, headers, device_id, "setColor", color)
            return {"color": color, "changed": True}

        if action == "set_color_temp":
            if device_type != "SwitchBotBulb":
                raise ValueError(f"set_color_temp requires SwitchBotBulb, got '{device_type}'.")
            temp = int(params.get("color_temp", 4000))
            await _send_command(session, headers, device_id, "setColorTemperature", str(temp))
            return {"color_temp": temp, "changed": True}

        raise ValueError(f"Unknown action '{action}'.")
