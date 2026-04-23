"""
Kasa protocol adapter — TP-Link Kasa LAN control via python-kasa.

Supports SmartPlug and SmartBulb device types.
All communication is local network only; no Kasa cloud API is used.

Safety guarantees (enforced per action):
  turn_on / turn_off — idempotent: no-op if device already in target state.
  set_brightness     — idempotent: no-op if current brightness == target.
  toggle             — always changes state; inherently non-idempotent.
  get_status         — read-only, never mutates device state.
"""

from typing import Any, Dict, List

from kasa import SmartBulb, SmartPlug

from providers.base import BaseDeviceProvider, ProviderError


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
        ip: str = device["ip"]
        device_type: str = device.get("device_type", "SmartPlug")

        try:
            if device_type == "SmartBulb":
                kasa_device: SmartPlug | SmartBulb = SmartBulb(ip)
            elif device_type == "SmartPlug":
                kasa_device = SmartPlug(ip)
            else:
                raise ValueError(
                    f"KasaAdapter does not support device_type '{device_type}'."
                )

            await kasa_device.update()
        except ValueError:
            raise
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
                    raise ValueError(
                        f"set_brightness requires device_type 'SmartBulb', "
                        f"got '{device_type}'."
                    )
                return await self._set_brightness(kasa_device, params)  # type: ignore[arg-type]
            raise ValueError(f"Unknown action '{action}'.")
        except ValueError:
            raise
        except Exception as exc:
            raise ProviderError(device["id"], f"Action '{action}' failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Private action helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _turn_on(device: SmartPlug) -> Dict[str, Any]:
        if device.is_on:
            return {"state": "on", "changed": False, "message": "Already on."}
        await device.turn_on()
        return {"state": "on", "changed": True}

    @staticmethod
    async def _turn_off(device: SmartPlug) -> Dict[str, Any]:
        if device.is_off:
            return {"state": "off", "changed": False, "message": "Already off."}
        await device.turn_off()
        return {"state": "off", "changed": True}

    @staticmethod
    async def _toggle(device: SmartPlug) -> Dict[str, Any]:
        if device.is_on:
            await device.turn_off()
            return {"state": "off", "changed": True}
        await device.turn_on()
        return {"state": "on", "changed": True}

    @staticmethod
    def _get_status(device: SmartPlug, device_type: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "state": "on" if device.is_on else "off",
            "alias": device.alias,
            "model": device.model,
            "changed": False,
        }
        if device_type == "SmartBulb":
            result["brightness"] = device.brightness  # type: ignore[attr-defined]
        return result

    @staticmethod
    async def _set_brightness(bulb: SmartBulb, params: Dict[str, Any]) -> Dict[str, Any]:
        if "brightness" not in params:
            raise ValueError(
                "set_brightness requires a 'brightness' parameter (0–100)."
            )
        target: int = int(params["brightness"])
        current: int = bulb.brightness

        if current == target:
            return {
                "brightness": target,
                "changed": False,
                "message": f"Already at {target}%.",
            }
        await bulb.set_brightness(target)
        return {"brightness": target, "changed": True}
