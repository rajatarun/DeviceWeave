"""
Compatibility shim — delegates to providers.kasa_adapter.

This module is preserved so any external scripts that imported
execute_device_command directly continue to work. New code should
import from providers or use execution_planner.execute_steps instead.
"""

from typing import Any, Dict

from providers.kasa_adapter import KasaAdapter

_adapter = KasaAdapter()


async def execute_device_command(
    device: Dict[str, Any],
    action: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    return await _adapter.execute(device, action, params)
