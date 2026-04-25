"""
DynamoDB-backed policy store for the Policy Authoring System.

Table schema (see template.yaml for CloudFormation definition):

  Primary key:
    PK  rule_id      String  — UUID v4 assigned by the validator
    SK  version      Number  — starts at 1, incremented on each update

  Attributes:
    device_type   String  — scope.device_type (also GSI partition key)
    scope         Map     — full scope object from Policy DSL
    conditions    List    — condition objects from Policy DSL
    action        Map     — action object from Policy DSL
    confidence    String  — stored as String to preserve float precision
    source_text   String  — original natural language rule submitted by the user
    status        String  — "active" | "inactive"
    created_at    String  — ISO 8601 UTC timestamp
    updated_at    String  — ISO 8601 UTC timestamp

  GSI:
    device-type-created-index
      PK  device_type   (queries: "show all fan policies")
      SK  created_at    (sort: newest first when ScanIndexForward=False)

Versioning:
  Each call to save_policy() creates a new version row with an incremented
  version number.  The latest version is always the one with the highest
  version number for a given rule_id.  Old versions are retained for audit.

Degraded mode:
  When POLICY_TABLE_NAME is not set (local dev without DynamoDB), is_configured()
  returns False and callers respond with 503 instead of attempting DynamoDB I/O.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TABLE_NAME: str = os.environ.get("POLICY_TABLE_NAME", "")


def _table():
    """Lazy DynamoDB table resource — one boto3 resource per Lambda container."""
    import boto3
    return boto3.resource("dynamodb").Table(_TABLE_NAME)


def is_configured() -> bool:
    """Return True when a policy table name is configured."""
    return bool(_TABLE_NAME)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def save_policy(
    rule_id: str,
    policy: Dict[str, Any],
    source_text: str,
) -> Dict[str, Any]:
    """
    Persist a validated policy to DynamoDB.

    Determines the next version number by querying for the latest existing
    version of rule_id.  If this is the first time rule_id is stored, version
    is 1.  Subsequent calls with the same rule_id increment the version,
    creating an immutable audit trail.

    Returns the full stored item dict (including version, timestamps, status).
    Raises RuntimeError if the table is not configured.
    Raises botocore.exceptions.ClientError on DynamoDB failure.
    """
    if not _TABLE_NAME:
        raise RuntimeError(
            "POLICY_TABLE_NAME environment variable is not set. "
            "Policy storage is unavailable."
        )

    now = datetime.now(timezone.utc).isoformat()

    existing = get_latest_policy_version(rule_id)
    version = (existing["version"] + 1) if existing else 1

    # Deactivate the previous version so list queries only surface the latest.
    if existing and existing.get("status") == "active":
        _set_status(rule_id, existing["version"], "superseded")

    item: Dict[str, Any] = {
        "rule_id": rule_id,
        "version": version,
        "device_type": policy["scope"]["device_type"],
        "scope": policy["scope"],
        "conditions": policy["conditions"],
        "action": policy["action"],
        "confidence": str(round(float(policy["confidence"]), 4)),
        "source_text": source_text,
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }

    _table().put_item(Item=item)

    logger.info(
        "Policy persisted: rule_id=%s version=%d device_type=%s confidence=%s",
        rule_id, version, item["device_type"], item["confidence"],
    )
    return item


def deactivate_policy(rule_id: str) -> bool:
    """
    Mark the latest active version of a policy as inactive.

    Returns True if the policy was found and deactivated, False if not found.
    """
    if not _TABLE_NAME:
        return False

    existing = get_latest_policy_version(rule_id)
    if not existing or existing.get("status") == "inactive":
        return False

    return _set_status(rule_id, existing["version"], "inactive")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_policy(
    rule_id: str,
    version: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Retrieve a policy by rule_id.

    When version is None, returns the latest version (highest version number).
    When version is specified, returns that exact version or None if not found.
    """
    if not _TABLE_NAME:
        return None

    try:
        if version is not None:
            resp = _table().get_item(Key={"rule_id": rule_id, "version": version})
            return resp.get("Item")
        return get_latest_policy_version(rule_id)
    except Exception as exc:
        logger.warning("get_policy(%s, v=%s) error: %s", rule_id, version, exc)
        return None


def get_latest_policy_version(rule_id: str) -> Optional[Dict[str, Any]]:
    """
    Return the highest-version item for rule_id, or None if no items exist.

    Uses ScanIndexForward=False so DynamoDB returns items in descending version
    order; Limit=1 fetches only the latest.
    """
    if not _TABLE_NAME:
        return None

    try:
        from boto3.dynamodb.conditions import Key
        resp = _table().query(
            KeyConditionExpression=Key("rule_id").eq(rule_id),
            ScanIndexForward=False,
            Limit=1,
        )
        items = resp.get("Items", [])
        return items[0] if items else None
    except Exception as exc:
        logger.warning("get_latest_policy_version(%s) error: %s", rule_id, exc)
        return None


def list_policies(
    device_type: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Return active policies, optionally filtered by device_type.

    When device_type is provided, uses the GSI (device-type-created-index) to
    avoid a full table scan.  Without a filter, falls back to a scan with a
    status=active filter — acceptable for the expected policy table size.

    Results are sorted newest-first (via ScanIndexForward=False on the GSI).
    """
    if not _TABLE_NAME:
        return []

    try:
        from boto3.dynamodb.conditions import Attr, Key

        if device_type:
            resp = _table().query(
                IndexName="device-type-created-index",
                KeyConditionExpression=Key("device_type").eq(device_type),
                FilterExpression=Attr("status").eq("active"),
                ScanIndexForward=False,
                Limit=limit,
            )
        else:
            resp = _table().scan(
                FilterExpression=Attr("status").eq("active"),
                Limit=limit,
            )
        return resp.get("Items", [])
    except Exception as exc:
        logger.warning("list_policies(device_type=%s) error: %s", device_type, exc)
        return []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _set_status(rule_id: str, version: int, status: str) -> bool:
    """Update the status field of a specific rule_id + version item."""
    try:
        _table().update_item(
            Key={"rule_id": rule_id, "version": version},
            UpdateExpression="SET #s = :status, updated_at = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": status,
                ":now": datetime.now(timezone.utc).isoformat(),
            },
        )
        logger.info(
            "Policy status updated: rule_id=%s version=%d → %s",
            rule_id, version, status,
        )
        return True
    except Exception as exc:
        logger.warning(
            "_set_status(%s, v=%d, %s) error: %s", rule_id, version, status, exc
        )
        return False
