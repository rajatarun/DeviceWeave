"""
Govee protocol adapter — cloud passthrough via Govee Developer API.

Commands use PUT /v1/devices/control; status uses the newer OpenAPI endpoint.
Credentials are loaded from Secrets Manager (GOVEE_SECRET_ARN):
    {"api_key": "..."}

The device dict must include a 'model' field (stored during ingestion).
"""

import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from providers.base import BaseDeviceProvider, ProviderError

logger = logging.getLogger(__name__)

_GOVEE_SECRET_ARN: str = os.environ.get("GOVEE_SECRET_ARN", "")
_GOVEE_API_URL = "https://developer-api.govee.com"
_GOVEE_STATE_URL = "https://openapi.api.govee.com/router/api/v1/device/state"

_cred_cache: Optional[Dict[str, str]] = None


def _get_credentials() -> Dict[str, str]:
    global _cred_cache
    if _cred_cache:
        return _cred_cache
    if not _GOVEE_SECRET_ARN:
        raise ProviderError("govee", "GOVEE_SECRET_ARN not set.")
    import boto3
    resp = boto3.client("secretsmanager").get_secret_value(SecretId=_GOVEE_SECRET_ARN)
    _cred_cache = json.loads(resp["SecretString"])
    return _cred_cache


async def _send_command(
    session: Any,
    api_key: str,
    device_id: str,
    model: str,
    cmd_name: str,
    cmd_value: Any,
) -> Dict:
    headers = {"Govee-API-Key": api_key, "Content-Type": "application/json"}
    body = {
        "device": device_id,
        "model": model,
        "cmd": {"name": cmd_name, "value": cmd_value},
    }
    async with session.put(
        f"{_GOVEE_API_URL}/v1/devices/control", headers=headers, json=body
    ) as resp:
        data = await resp.json(content_type=None)
    if data.get("code") != 200:
        raise ProviderError(
            device_id,
            f"Govee command error {data.get('code')}: {data.get('message', data)}",
        )
    return data


async def _fetch_status(session: Any, api_key: str, device_id: str, model: str) -> Dict:
    headers = {"Govee-API-Key": api_key, "Content-Type": "application/json"}
    body = {
        "requestId": str(uuid.uuid4()),
        "payload": {"sku": model, "device": device_id},
    }
    async with session.post(_GOVEE_STATE_URL, headers=headers, json=body) as resp:
        data = await resp.json(content_type=None)
    # OpenAPI returns {"code": 200} or {"status": 200} depending on firmware
    if data.get("code") not in (200, None) and data.get("status") not in (200, None):
        raise ProviderError(device_id, f"Govee status error: {data}")
    return data.get("payload", {})


class GoveeAdapter(BaseDeviceProvider):

    @classmethod
    def supported_device_types(cls) -> List[str]:
        return ["GoveeBulb", "GoveePlug"]

    async def execute(
        self,
        device: Dict[str, Any],
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        import aiohttp

        device_id = device["id"]
        device_type = device.get("device_type", "GoveePlug")
        model = device.get("model", "")

        if not model:
            raise ProviderError(
                device_id, "Device record is missing 'model' — re-run /ingest."
            )

        try:
            creds = _get_credentials()
            async with aiohttp.ClientSession() as session:
                return await self._run(
                    session, creds["api_key"],
                    device_id, model, device_type, action, params,
                )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(device_id, f"Cloud action '{action}' failed: {exc}") from exc

    async def _run(
        self,
        session: Any,
        api_key: str,
        device_id: str,
        model: str,
        device_type: str,
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:

        if action == "get_status":
            payload = await _fetch_status(session, api_key, device_id, model)
            caps = payload.get("capabilities", [])
            power_cap = next(
                (c for c in caps if c.get("type") == "devices.capabilities.on_off"), None
            )
            is_on = bool(power_cap and power_cap.get("state", {}).get("value") == 1)
            result: Dict[str, Any] = {"state": "on" if is_on else "off", "changed": False}
            if device_type == "GoveeBulb":
                brightness_cap = next(
                    (c for c in caps
                     if c.get("type") == "devices.capabilities.range"
                     and c.get("instance") == "brightness"),
                    None,
                )
                if brightness_cap:
                    result["brightness"] = brightness_cap.get("state", {}).get("value")
            return result

        if action == "toggle":
            payload = await _fetch_status(session, api_key, device_id, model)
            caps = payload.get("capabilities", [])
            power_cap = next(
                (c for c in caps if c.get("type") == "devices.capabilities.on_off"), None
            )
            is_on = bool(power_cap and power_cap.get("state", {}).get("value") == 1)
            next_action = "turn_off" if is_on else "turn_on"
            return await self._run(
                session, api_key, device_id, model, device_type, next_action, params
            )

        if action == "turn_on":
            await _send_command(session, api_key, device_id, model, "turn", "on")
            return {"state": "on", "changed": True}

        if action == "turn_off":
            await _send_command(session, api_key, device_id, model, "turn", "off")
            return {"state": "off", "changed": True}

        if action == "set_brightness":
            if device_type != "GoveeBulb":
                raise ValueError(f"set_brightness requires GoveeBulb, got '{device_type}'.")
            target = int(params.get("brightness", 100))
            await _send_command(session, api_key, device_id, model, "brightness", target)
            return {"brightness": target, "changed": True}

        if action == "set_color":
            if device_type != "GoveeBulb":
                raise ValueError(f"set_color requires GoveeBulb, got '{device_type}'.")
            color = params.get("color", {"r": 255, "g": 255, "b": 255})
            await _send_command(session, api_key, device_id, model, "color", color)
            return {"color": color, "changed": True}

        if action == "set_color_temp":
            if device_type != "GoveeBulb":
                raise ValueError(f"set_color_temp requires GoveeBulb, got '{device_type}'.")
            temp = int(params.get("color_temp", 4000))
            await _send_command(session, api_key, device_id, model, "colorTem", temp)
            return {"color_temp": temp, "changed": True}

        raise ValueError(f"Unknown action '{action}'.")
