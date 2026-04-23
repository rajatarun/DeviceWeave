"""
Kasa device execution provider.

Uses python-kasa SmartPlug / SmartBulb — no Kasa cloud API.
All device communication is local network only.

Safety guarantees:
  - Idempotency checks before every state-changing command.
  - set_brightness is only attempted on SmartBulb device_type.
  - Unknown actions raise ValueError (caught upstream in app.py).
"""

import asyncio
from typing import Any, Dict

from kasa import SmartBulb, SmartPlug


async def execute_device_command(
    device: Dict[str, Any],
    action: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Connect to a Kasa device over LAN and execute the given action.

    Args:
        device:  Entry from DEVICE_CATALOG (must contain 'ip' and 'device_type').
        action:  One of: turn_on, turn_off, toggle, get_status, set_brightness.
        params:  Action-specific parameters (e.g. {'brightness': 75}).

    Returns:
        A dict describing the outcome. Always includes 'changed: bool'.

    Raises:
        ValueError for unsupported actions or device_type mismatches.
        kasa.KasaException if the device is unreachable or returns an error.
    """
    ip: str = device["ip"]
    device_type: str = device.get("device_type", "SmartPlug")

    kasa_device: SmartPlug | SmartBulb

    if device_type == "SmartBulb":
        kasa_device = SmartBulb(ip)
    elif device_type == "SmartPlug":
        kasa_device = SmartPlug(ip)
    else:
        raise ValueError(
            f"Unsupported device_type '{device_type}'. "
            "Only SmartPlug and SmartBulb are supported."
        )

    # Fetch current device state before acting.
    await kasa_device.update()

    if action == "turn_on":
        return await _turn_on(kasa_device)

    if action == "turn_off":
        return await _turn_off(kasa_device)

    if action == "toggle":
        return await _toggle(kasa_device)

    if action == "get_status":
        return _get_status(kasa_device, device_type)

    if action == "set_brightness":
        if device_type != "SmartBulb":
            raise ValueError(
                f"set_brightness requires device_type 'SmartBulb', "
                f"got '{device_type}'."
            )
        return await _set_brightness(kasa_device, params)  # type: ignore[arg-type]

    raise ValueError(f"Unknown action '{action}'.")


# ---------------------------------------------------------------------------
# Private action helpers
# ---------------------------------------------------------------------------

async def _turn_on(device: SmartPlug) -> Dict[str, Any]:
    if device.is_on:
        return {"state": "on", "changed": False, "message": "Device was already on."}
    await device.turn_on()
    return {"state": "on", "changed": True}


async def _turn_off(device: SmartPlug) -> Dict[str, Any]:
    if device.is_off:
        return {"state": "off", "changed": False, "message": "Device was already off."}
    await device.turn_off()
    return {"state": "off", "changed": True}


async def _toggle(device: SmartPlug) -> Dict[str, Any]:
    if device.is_on:
        await device.turn_off()
        return {"state": "off", "changed": True}
    await device.turn_on()
    return {"state": "on", "changed": True}


def _get_status(device: SmartPlug, device_type: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "state": "on" if device.is_on else "off",
        "alias": device.alias,
        "model": device.model,
        "changed": False,
    }
    if device_type == "SmartBulb":
        bulb: SmartBulb = device  # type: ignore[assignment]
        result["brightness"] = bulb.brightness
    return result


async def _set_brightness(bulb: SmartBulb, params: Dict[str, Any]) -> Dict[str, Any]:
    if "brightness" not in params:
        raise ValueError(
            "set_brightness action requires a 'brightness' parameter (0-100)."
        )
    target: int = params["brightness"]
    current: int = bulb.brightness

    if current == target:
        return {
            "brightness": target,
            "changed": False,
            "message": f"Brightness was already {target}%.",
        }

    await bulb.set_brightness(target)
    return {"brightness": target, "changed": True}
