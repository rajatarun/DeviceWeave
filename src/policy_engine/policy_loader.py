"""
TTL-based DynamoDB policy cache for the Policy Engine.

Policies are loaded once from DynamoDB on the first call after cold start
(or after TTL expiry) and held in a module-level dict for subsequent
invocations within the same Lambda container.  This avoids a DynamoDB
read on every /execute call while keeping policies reasonably fresh.

TTL: 60 seconds.  A policy authored via POST /policies/author takes effect
within 60 seconds across all execution Lambda containers.  The TTL is
intentionally short — policies are safety constraints, not preferences.

Cache structure:
    _cache: Dict[device_type → List[policy_dict]]

Failure mode:
    On any DynamoDB error the existing cache (possibly stale or empty) is
    retained and the TTL is reset to 10 seconds for a faster retry.
    Failing open (empty cache → allow all) is safer than retrying on every
    invocation when the table is unavailable.

Pagination:
    Full table scan with pagination handles tables larger than 1 MB
    (the DynamoDB single-response limit).  In practice the policies table
    for a single household stays well under that limit.
"""

import logging
import os
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_TABLE_NAME: str = os.environ.get("POLICY_TABLE_NAME", "")

_TTL_SECONDS = 60
_RETRY_TTL_SECONDS = 10  # shorter TTL after a load failure

_cache: Dict[str, List[Dict[str, Any]]] = {}  # device_type → [policy, …]
_cache_expiry: float = 0.0


def get_policies_for_device(device_type: str) -> List[Dict[str, Any]]:
    """
    Return all active policies scoped to *device_type*.

    Refreshes the in-memory cache from DynamoDB if the TTL has expired.
    Returns an empty list when no table is configured or no matching
    policies exist — callers treat empty as "allow all".
    """
    _ensure_fresh()
    return _cache.get(device_type, [])


def invalidate() -> None:
    """
    Force a cache reload on the next call.

    Called within the same Lambda container after a policy is authored so
    the engine immediately picks up the new rule.  Has no effect across
    different Lambda containers (TTL handles cross-container propagation).
    """
    global _cache_expiry
    _cache_expiry = 0.0
    logger.debug("Policy cache invalidated.")


def is_configured() -> bool:
    return bool(_TABLE_NAME)


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _ensure_fresh() -> None:
    if time.monotonic() < _cache_expiry:
        return
    _reload()


def _reload() -> None:
    global _cache, _cache_expiry

    if not _TABLE_NAME:
        _cache = {}
        _cache_expiry = time.monotonic() + _TTL_SECONDS
        return

    try:
        import boto3
        from boto3.dynamodb.conditions import Attr

        table = boto3.resource("dynamodb").Table(_TABLE_NAME)
        items: List[Dict[str, Any]] = []

        # Paginate — DynamoDB returns at most 1 MB per scan call.
        scan_kwargs: Dict[str, Any] = {
            "FilterExpression": Attr("status").eq("active"),
        }
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        while "LastEvaluatedKey" in resp:
            resp = table.scan(
                **scan_kwargs,
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))

        # Group by device_type for O(1) lookup at evaluation time.
        new_cache: Dict[str, List[Dict[str, Any]]] = {}
        for item in items:
            dt = item.get("device_type")
            if dt:
                new_cache.setdefault(dt, []).append(item)

        _cache = new_cache
        _cache_expiry = time.monotonic() + _TTL_SECONDS

        logger.info(
            "Policy cache loaded: %d active policies across %d device types (TTL=%ds)",
            len(items), len(new_cache), _TTL_SECONDS,
        )

    except Exception as exc:
        logger.warning(
            "Policy cache reload failed (%s) — retaining current cache, retry in %ds",
            exc, _RETRY_TTL_SECONDS,
        )
        # Retain stale cache; retry sooner than the normal TTL.
        _cache_expiry = time.monotonic() + _RETRY_TTL_SECONDS
