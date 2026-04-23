"""
Abstract provider adapter interface.

Every IoT protocol (Kasa, Matter, Zigbee, Thread, …) implements this
interface. The execution planner selects the correct provider at runtime
via the registry in __init__.py — no code outside providers/ needs to
know which protocol a device uses.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List


class BaseDeviceProvider(ABC):
    """
    Protocol adapter for a class of IoT devices.

    Implementations must be stateless — all device state is fetched
    fresh on each execute() call via the underlying protocol.
    """

    @classmethod
    @abstractmethod
    def supported_device_types(cls) -> List[str]:
        """
        Return the device_type strings this adapter can handle.

        These must match the 'device_type' field in DEVICE_CATALOG.
        Example: ["SmartPlug", "SmartBulb"]
        """
        ...

    @abstractmethod
    async def execute(
        self,
        device: Dict[str, Any],
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Execute an action on a physical device.

        Args:
            device:  Full device dict from DEVICE_CATALOG (contains 'ip',
                     'device_type', 'capabilities', etc.).
            action:  Validated action string — one of the device's
                     declared capabilities.
            params:  Action-specific parameters (e.g. {'brightness': 75}).

        Returns:
            A result dict that always includes a 'changed' bool key.

        Raises:
            ValueError   — for unsupported actions or type mismatches.
            ProviderError — for device communication failures (subclass
                            of RuntimeError, defined below).
        """
        ...


class ProviderError(RuntimeError):
    """
    Raised when communication with a physical device fails.

    Wraps low-level protocol exceptions so callers only need to catch
    one exception type regardless of which adapter is in use.
    """

    def __init__(self, device_id: str, message: str):
        self.device_id = device_id
        super().__init__(f"[{device_id}] {message}")
