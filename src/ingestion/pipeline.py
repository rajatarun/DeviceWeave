"""
Ingestion pipeline orchestrator.

Two modes
---------
full
    Discover every device on the network.  Upsert all records into the
    registry.  Any device previously marked active that was NOT found this
    run is flipped to status='offline'.  Use this for daily reconciliation.

delta
    Discover every device on the network (same scan cost as full).  Only
    write items whose fingerprint has changed since the last sync; for
    unchanged devices, update only last_seen.  Never marks devices offline.
    Use this for frequent background polls where write costs matter.

Fingerprint-based change detection
-----------------------------------
Each DeviceRecord carries a 16-char hex fingerprint (SHA-256) of its
identity fields: ip, name, device_type, model, mac.  In delta mode the
pipeline fetches the stored fingerprint via get_item(ProjectionExpression)
before deciding whether to upsert or just touch last_seen.  If the stored
fingerprint is absent the item is new — treated as a write.

Provider registration
---------------------
Call register_provider() to add discovery providers beyond Kasa.
KasaDiscovery is registered automatically at module load time.
"""

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Type

from ingestion.device_registry import DeviceRecord, DeviceRegistry
from ingestion.providers.base import AbstractDiscoveryProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sync mode
# ---------------------------------------------------------------------------

class SyncMode(str, Enum):
    FULL = "full"
    DELTA = "delta"


# ---------------------------------------------------------------------------
# Result object returned to the caller / Lambda response body
# ---------------------------------------------------------------------------

@dataclass
class IngestionResult:
    provider: str
    mode: str
    discovered: int
    upserted: int
    unchanged: int
    offline: int          # full-mode only; always 0 in delta
    errors: int           # devices that raised during probe
    duration_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Provider registry (module-level, built at import time)
# ---------------------------------------------------------------------------

_PROVIDERS: Dict[str, AbstractDiscoveryProvider] = {}


def register_provider(provider: AbstractDiscoveryProvider) -> None:
    _PROVIDERS[provider.name] = provider


def get_discovery_provider(name: str) -> AbstractDiscoveryProvider:
    provider = _PROVIDERS.get(name)
    if provider is None:
        supported = ", ".join(sorted(_PROVIDERS))
        raise ValueError(
            f"No discovery provider registered for '{name}'. "
            f"Supported: {supported}"
        )
    return provider


# Register Kasa provider at import time.
try:
    from ingestion.providers.kasa_discovery import KasaDiscovery
    register_provider(KasaDiscovery())
except Exception as _exc:  # pragma: no cover
    logger.warning("Could not register KasaDiscovery: %s", _exc)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class IngestionPipeline:
    """
    Orchestrates a single ingestion run for one provider in one mode.

    Usage:
        result = asyncio.run(IngestionPipeline("kasa", "full").run())
    """

    def __init__(self, provider_name: str, mode: str):
        self.provider = get_discovery_provider(provider_name)
        self.mode = SyncMode(mode)
        self.registry = DeviceRegistry()

    async def run(self) -> IngestionResult:
        t_start = time.monotonic()
        now = datetime.now(timezone.utc).isoformat()

        # --- Discovery (same cost for both modes) ---
        raw_records: List[DeviceRecord] = await self.provider.discover_all()

        # Stamp sync metadata onto each record before writing.
        for rec in raw_records:
            rec.sync_mode = self.mode.value
            rec.last_synced = now

        errors = sum(1 for r in raw_records if not isinstance(r, DeviceRecord))
        records = [r for r in raw_records if isinstance(r, DeviceRecord)]

        if self.mode == SyncMode.FULL:
            result = self._full_sync(records, now)
        else:
            result = self._delta_sync(records, now)

        result.discovered = len(records)
        result.errors = errors
        result.duration_ms = round((time.monotonic() - t_start) * 1000, 1)

        logger.info(
            "Ingestion complete — %s/%s: discovered=%d upserted=%d "
            "unchanged=%d offline=%d errors=%d duration=%.0fms",
            result.provider, result.mode,
            result.discovered, result.upserted, result.unchanged,
            result.offline, result.errors, result.duration_ms,
        )
        return result

    # ------------------------------------------------------------------
    # Full sync
    # ------------------------------------------------------------------

    def _full_sync(self, records: List[DeviceRecord], now: str) -> IngestionResult:
        """
        Upsert every discovered device.  Mark any previously active device
        that was not found this run as offline.
        """
        # Fetch existing active IDs before writing so we can diff.
        existing_ids = self.registry.get_active_device_ids(self.provider.name)
        discovered_ids = {r.device_id for r in records}

        upserted = 0
        for record in records:
            if self.registry.upsert_device(record):
                upserted += 1

        # Devices in the registry but not discovered this run → offline.
        offline = 0
        for gone_id in existing_ids - discovered_ids:
            if self.registry.mark_offline(gone_id, self.provider.name, now):
                offline += 1

        return IngestionResult(
            provider=self.provider.name,
            mode=SyncMode.FULL.value,
            discovered=0,   # filled by caller
            upserted=upserted,
            unchanged=0,
            offline=offline,
            errors=0,       # filled by caller
            duration_ms=0,  # filled by caller
        )

    # ------------------------------------------------------------------
    # Delta sync
    # ------------------------------------------------------------------

    def _delta_sync(self, records: List[DeviceRecord], now: str) -> IngestionResult:
        """
        Only write records whose fingerprint differs from the stored value.
        Unchanged records get a last_seen update only.
        Delta never marks devices offline — use full sync for that.
        """
        upserted = unchanged = 0

        for record in records:
            stored_fp = self.registry.get_fingerprint(record.device_id, record.provider)

            if stored_fp != record.fingerprint:
                # New device or changed identity (IP moved, alias renamed, etc.)
                if self.registry.upsert_device(record):
                    upserted += 1
            else:
                # Unchanged — only bump last_seen to record network visibility.
                self.registry.touch_last_seen(
                    record.device_id, record.provider, now
                )
                unchanged += 1

        return IngestionResult(
            provider=self.provider.name,
            mode=SyncMode.DELTA.value,
            discovered=0,
            upserted=upserted,
            unchanged=unchanged,
            offline=0,
            errors=0,
            duration_ms=0,
        )
