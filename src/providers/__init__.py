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

# Maps device_type → provider instance.
_REGISTRY: dict[str, BaseDeviceProvider] = {}

_kasa = KasaAdapter()
for _device_type in KasaAdapter.supported_device_types():
    _REGISTRY[_device_type] = _kasa


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
