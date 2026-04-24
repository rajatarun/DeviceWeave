"""
DynamoDB-backed device registry.

Stores the discovered device fleet — one item per (device_id, provider) pair.

Table schema (see template.yaml → DeviceRegistryTable):
  PK  device_id   String  — hardware-stable device identifier
  SK  provider    String  — discovery provider, e.g. "kasa"

  name            String  — human-readable alias
  ip              String  — current IP address
  mac             String  — MAC address (empty string if unavailable)
  device_type     String  — "SmartBulb" | "SmartPlug" | …
  model           String  — hardware model string
  capabilities    List    — supported action strings
  fingerprint     String  — SHA-256 prefix of identity fields; drives delta writes
  status          String  — "active" | "offline"
  last_seen       String  — ISO 8601 UTC; updated every sync
  last_synced     String  — ISO 8601 UTC; updated only on full upsert
  sync_mode       String  — "full" | "delta"; records which pass wrote this item
  provider_meta   Map     — provider-specific raw metadata

GSI (provider-status-index):
  PK  provider    — allows querying all devices for a given provider
  SK  status      — allows filtering active vs offline

boto3 is pre-installed in Lambda Python 3.11 — excluded from requirements.txt.
"""

import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

_TABLE_NAME: str = os.environ.get("DEVICE_REGISTRY_TABLE", "")


def _table():
    import boto3
    return boto3.resource("dynamodb").Table(_TABLE_NAME)


def is_configured() -> bool:
    return bool(_TABLE_NAME)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class DeviceRecord:
    device_id: str
    provider: str
    name: str
    ip: str
    mac: str
    device_type: str
    model: str
    capabilities: List[str]
    fingerprint: str
    status: str                                   # "active" | "offline"
    last_seen: str                                # ISO 8601
    last_synced: str                              # ISO 8601
    sync_mode: str                                # "full" | "delta"
    provider_meta: Dict[str, Any] = field(default_factory=dict)

    def to_item(self) -> Dict[str, Any]:
        """Serialize to a DynamoDB-safe dict (no non-string scalars in nested maps)."""
        item = asdict(self)
        # DynamoDB requires that empty strings in Maps are stripped or replaced.
        # Flatten provider_meta values to strings to avoid type errors.
        item["provider_meta"] = {
            k: str(v) if not isinstance(v, (str, bool, type(None))) else v
            for k, v in self.provider_meta.items()
        }
        return item


# ---------------------------------------------------------------------------
# Registry operations
# ---------------------------------------------------------------------------

class DeviceRegistry:
    """
    Thin wrapper over DynamoDB for device registry read/write operations.

    All methods degrade gracefully (log + return empty) when
    DEVICE_REGISTRY_TABLE is not set.
    """

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def upsert_device(self, record: DeviceRecord) -> bool:
        """
        Write or overwrite the full device item.

        Uses put_item (full overwrite) — idempotent on repeated calls with
        the same data.  Returns True on success, False on error.
        """
        if not _TABLE_NAME:
            return False
        try:
            _table().put_item(Item=record.to_item())
            logger.debug("Upserted device %s/%s", record.provider, record.device_id)
            return True
        except Exception as exc:
            logger.error("upsert_device failed for %s/%s: %s",
                         record.provider, record.device_id, exc)
            return False

    def mark_offline(self, device_id: str, provider: str, timestamp: str) -> bool:
        """
        Set status to 'offline' and update last_synced.

        Uses update_item so unrelated attributes are preserved.
        """
        if not _TABLE_NAME:
            return False
        from botocore.exceptions import ClientError
        try:
            _table().update_item(
                Key={"device_id": device_id, "provider": provider},
                UpdateExpression="SET #s = :offline, last_synced = :ts",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":offline": "offline",
                    ":ts": timestamp,
                },
                ConditionExpression="attribute_exists(device_id)",
            )
            logger.info("Marked offline: %s/%s", provider, device_id)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.debug("mark_offline: item not found for %s/%s", provider, device_id)
            else:
                logger.error("mark_offline failed for %s/%s: %s", provider, device_id, exc)
            return False
        except Exception as exc:
            logger.error("mark_offline failed for %s/%s: %s", provider, device_id, exc)
            return False

    def touch_last_seen(self, device_id: str, provider: str, timestamp: str) -> bool:
        """
        Update only last_seen without touching any other attribute.

        Used by delta sync for unchanged devices — records that they are
        still visible on the network without a full write.
        """
        if not _TABLE_NAME:
            return False
        try:
            _table().update_item(
                Key={"device_id": device_id, "provider": provider},
                UpdateExpression="SET last_seen = :ts",
                ExpressionAttributeValues={":ts": timestamp},
            )
            return True
        except Exception as exc:
            logger.warning("touch_last_seen failed for %s/%s: %s", provider, device_id, exc)
            return False

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_fingerprint(self, device_id: str, provider: str) -> Optional[str]:
        """
        Fetch only the fingerprint attribute for a single device.

        Returns None if the item does not exist or the table is not configured.
        """
        if not _TABLE_NAME:
            return None
        try:
            resp = _table().get_item(
                Key={"device_id": device_id, "provider": provider},
                ProjectionExpression="fingerprint",
            )
            item = resp.get("Item")
            return item.get("fingerprint") if item else None
        except Exception as exc:
            logger.warning("get_fingerprint failed for %s/%s: %s", provider, device_id, exc)
            return None

    def get_active_device_ids(self, provider: str) -> Set[str]:
        """
        Return the set of device_ids currently marked active for a provider.

        Uses the GSI provider-status-index with status = 'active'.
        Falls back to a filtered scan if the GSI is unavailable.
        """
        if not _TABLE_NAME:
            return set()
        try:
            import boto3
            from boto3.dynamodb.conditions import Key

            resp = _table().query(
                IndexName="provider-status-index",
                KeyConditionExpression=(
                    Key("provider").eq(provider) & Key("status").eq("active")
                ),
                ProjectionExpression="device_id",
            )
            return {item["device_id"] for item in resp.get("Items", [])}
        except Exception as exc:
            logger.warning(
                "get_active_device_ids GSI query failed for %s: %s — falling back to scan",
                provider, exc,
            )
            return self._scan_active_ids(provider)

    def _scan_active_ids(self, provider: str) -> Set[str]:
        """Full-table scan fallback; only used when GSI is unavailable."""
        try:
            from boto3.dynamodb.conditions import Attr
            resp = _table().scan(
                FilterExpression=(
                    Attr("provider").eq(provider) & Attr("status").eq("active")
                ),
                ProjectionExpression="device_id",
            )
            return {item["device_id"] for item in resp.get("Items", [])}
        except Exception as exc:
            logger.error("_scan_active_ids fallback failed for %s: %s", provider, exc)
            return set()
