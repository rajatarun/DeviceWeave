"""
Provider registry — maps device_type strings to protocol adapter instances.

To add a new protocol (Matter, Zigbee, Thread, …):
  1. Create src/providers/<protocol>_adapter.py implementing BaseDeviceProvider.
  2. Import it here and register its supported_device_types() below.

The registry is built once at module load time. Adapters are shared
instances — they must be stateless.
"""

import os

from providers.base import BaseDeviceProvider, ProviderError
from providers.kasa_adapter import KasaAdapter
from providers.switchbot_adapter import SwitchBotAdapter
from providers.govee_adapter import GoveeAdapter
from providers.ring_adapter import RingAdapter

# Maps device_type → provider instance.
_REGISTRY: dict[str, BaseDeviceProvider] = {}

_ADAPTERS = (KasaAdapter(), SwitchBotAdapter(), GoveeAdapter(), RingAdapter())
for _adapter in _ADAPTERS:
    for _device_type in _adapter.supported_device_types():
        _REGISTRY[_device_type] = _adapter

# Static metadata per provider name
_PROVIDER_META = {
    "kasa": {
        "display_name": "TP-Link Kasa",
        "credential_env": "KASA_SECRET_ARN",
        "supports_rename": True,
    },
    "govee": {
        "display_name": "Govee",
        "credential_env": "GOVEE_SECRET_ARN",
        "supports_rename": False,
    },
    "switchbot": {
        "display_name": "SwitchBot",
        "credential_env": "SWITCHBOT_SECRET_ARN",
        "supports_rename": False,
    },
    "ring": {
        "display_name": "Ring",
        "credential_env": "RING_SECRET_ARN",
        "supports_rename": False,
    },
}


def get_provider(device_type: str) -> BaseDeviceProvider:
    """
    Return the registered provider for device_type.

    Raises ValueError if no adapter is registered for that type.
    """
    provider = _REGISTRY.get(device_type)
    if provider is None:
        supported = ", ".join(sorted(_REGISTRY))
        raise ValueError(
            f"No provider registered for device_type '{device_type}'. "
            f"Supported: {supported}"
        )
    return provider


def list_providers() -> list:
    """Return metadata for every registered provider."""
    # Collect device_types per provider from the registry
    provider_device_types: dict[str, list] = {}
    for device_type, adapter in _REGISTRY.items():
        name = type(adapter).__name__.replace("Adapter", "").lower()
        provider_device_types.setdefault(name, []).append(device_type)

    result = []
    for name, meta in _PROVIDER_META.items():
        result.append({
            "name": name,
            "display_name": meta["display_name"],
            "device_types": sorted(provider_device_types.get(name, [])),
            "configured": bool(os.environ.get(meta["credential_env"], "")),
            "supports_rename": meta["supports_rename"],
        })
    return result


__all__ = ["BaseDeviceProvider", "ProviderError", "get_provider", "list_providers"]
