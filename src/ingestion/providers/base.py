"""
Abstract discovery provider interface.

Discovery providers scan a protocol's network segment and return a list of
DeviceRecords. They are read-only — no state changes on the devices.

Separate from src/providers/ (execution adapters) by design:
  - src/providers/        → protocol adapters for executing commands
  - src/ingestion/providers/ → protocol scanners for discovering devices
"""

from abc import ABC, abstractmethod
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from ingestion.device_registry import DeviceRecord


class AbstractDiscoveryProvider(ABC):
    """
    Protocol scanner that enumerates devices on a network segment.

    Implementations must be stateless — each discover_all() call performs
    a fresh scan.  No caching inside the provider itself.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier string, e.g. 'kasa'."""
        ...

    @abstractmethod
    async def discover_all(self) -> "List[DeviceRecord]":
        """
        Scan the network and return all reachable devices.

        Must not raise — individual device failures should be caught and
        counted internally; the list should contain every successfully
        probed device.
        """
        ...
