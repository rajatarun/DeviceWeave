"""
DynamoDB-backed phrase learning store.

Phrases learned from successful executions are persisted here and fed
back into device_resolver to improve cosine similarity accuracy over time.

Table schema (see template.yaml for the CloudFormation definition):
  PK  device_id  String  — catalog device id, e.g. "office_light"
  SK  phrase     String  — normalised command that resolved to this device
  source         String  — "learned" | "manual"
  confidence     String  — cosine score at capture time (stored as String)
  created_at     String  — ISO 8601 UTC timestamp
  use_count      Number  — incremented each time the same phrase resolves

Degraded mode: when LEARNING_TABLE_NAME is not set (e.g. local dev without
DynamoDB), every function returns an empty/False result silently. The rest
of the system continues without learning.

boto3 is pre-installed in the Lambda Python 3.11 runtime.
For local dev: pip install boto3 (not included in requirements.txt to avoid
adding ~10 MB to the Lambda deployment package).
"""

import logging
import os
from datetime import datetime, timezone
from typing import Dict, List

logger = logging.getLogger(__name__)

_TABLE_NAME: str = os.environ.get("LEARNING_TABLE_NAME", "")

# Minimum cosine confidence required to auto-learn a phrase.
# Set via env var so it can be tuned per stage without code changes.
LEARNING_THRESHOLD: float = float(
    os.environ.get("LEARNING_CONFIDENCE_THRESHOLD", "0.85")
)


def _table():
    """Lazy DynamoDB table resource — created once per Lambda container."""
    import boto3  # noqa: PLC0415 — intentional lazy import (not in requirements.txt)
    from boto3.dynamodb.conditions import Key as _Key  # noqa: F401
    dynamodb = boto3.resource("dynamodb")
    return dynamodb.Table(_TABLE_NAME)


def is_configured() -> bool:
    """Return True when a learning table name is provided."""
    return bool(_TABLE_NAME)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def save_learned_phrase(device_id: str, phrase: str, confidence: float) -> bool:
    """
    Persist phrase → device_id mapping.

    Uses a ConditionExpression so the first write wins; subsequent
    identical phrases only increment use_count.

    Returns True if a new item was written, False otherwise.
    """
    if not _TABLE_NAME:
        return False

    from botocore.exceptions import ClientError

    try:
        _table().put_item(
            Item={
                "device_id": device_id,
                "phrase": phrase,
                "source": "learned",
                "confidence": str(round(confidence, 4)),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "use_count": 1,
            },
            ConditionExpression="attribute_not_exists(phrase)",
        )
        logger.info("Learned phrase '%s' → %s (conf=%.4f)", phrase, device_id, confidence)
        return True
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ConditionalCheckFailedException":
            _increment_use_count(device_id, phrase)
        else:
            logger.warning("DynamoDB put_item failed (%s): %s", code, exc)
        return False
    except Exception as exc:
        logger.warning("save_learned_phrase error: %s", exc)
        return False


def save_manual_phrase(device_id: str, phrase: str) -> bool:
    """
    Persist a manually submitted phrase (from POST /learn).

    Overwrites any existing entry for the same phrase so the source
    is updated to "manual".
    """
    if not _TABLE_NAME:
        return False

    from botocore.exceptions import ClientError

    try:
        _table().put_item(
            Item={
                "device_id": device_id,
                "phrase": phrase,
                "source": "manual",
                "confidence": "1.0000",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "use_count": 0,
            }
        )
        logger.info("Manual phrase '%s' → %s", phrase, device_id)
        return True
    except ClientError as exc:
        logger.warning("save_manual_phrase failed: %s", exc)
        return False
    except Exception as exc:
        logger.warning("save_manual_phrase error: %s", exc)
        return False


def _increment_use_count(device_id: str, phrase: str) -> None:
    from botocore.exceptions import ClientError
    try:
        _table().update_item(
            Key={"device_id": device_id, "phrase": phrase},
            UpdateExpression="ADD use_count :inc",
            ExpressionAttributeValues={":inc": 1},
        )
    except (ClientError, Exception):
        pass


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def load_learned_phrases(device_id: str) -> List[str]:
    """Return all stored phrases for a specific device_id."""
    if not _TABLE_NAME:
        return []
    try:
        import boto3
        from boto3.dynamodb.conditions import Key

        resp = _table().query(
            KeyConditionExpression=Key("device_id").eq(device_id)
        )
        return [item["phrase"] for item in resp.get("Items", [])]
    except Exception as exc:
        logger.warning("load_learned_phrases(%s) error: %s", device_id, exc)
        return []


def load_all_learned_phrases() -> Dict[str, List[str]]:
    """
    Return all stored phrases grouped by device_id.

    Uses a full table scan — acceptable for a small phrase store
    (< 10 k items). For larger stores, replace with per-device queries.
    """
    if not _TABLE_NAME:
        return {}
    try:
        resp = _table().scan()
        result: Dict[str, List[str]] = {}
        for item in resp.get("Items", []):
            did = item["device_id"]
            result.setdefault(did, []).append(item["phrase"])
        return result
    except Exception as exc:
        logger.warning("load_all_learned_phrases error: %s", exc)
        return {}
