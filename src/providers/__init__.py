"""
Provider registry — maps device_type strings to protocol adapter instances.

To add a new protocol (Matter, Zigbee, Thread, …):
  1. Create src/providers/<protocol>_adapter.py implementing BaseDeviceProvider.
  2. Import it here and register its supported_device_types() below.

The registry is built once at module load time. Adapters are shared
instances — they must be stateless.
"""

from providers.base import BaseDeviceProvider, ProviderError
from providers.kasa_adapter import KasaAdapter
from providers.switchbot_adapter import SwitchBotAdapter
from providers.govee_adapter import GoveeAdapter

# Maps device_type → provider instance.
_REGISTRY: dict[str, BaseDeviceProvider] = {}

for _adapter in (KasaAdapter(), SwitchBotAdapter(), GoveeAdapter()):
    for _device_type in _adapter.supported_device_types():
        _REGISTRY[_device_type] = _adapter


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


__all__ = ["BaseDeviceProvider", "ProviderError", "get_provider"]
