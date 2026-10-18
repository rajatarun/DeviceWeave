"""
Eero presence poller.

EventBridge Scheduler invokes this on a rate (default every 5 minutes).
It reads the session token from Secrets Manager, lists clients on eero's
unofficial cloud API, maps the configured devices to people, and writes
``pk=home_state`` through presence_store — the same item GET /presence and
the policy engine read.

The function is not in a VPC. The eero API is on the public internet, and
the private Lambda subnets only reach it through NAT. Keeping this function
out of the VPC preserves the pay-per-request model.

Fail-safe: a rejected session, a transport error, a blank secret, or an
empty tracked-device table never changes ``is_home``. The last presence is
kept and marked ``freshness=stale``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import aws_clients
import eero_client
import eero_presence
import presence_store

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.getLogger().setLevel(LOG_LEVEL)
for _noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    del event, context  # Scheduler input is informational; the poll has no payload.
    table_name = os.environ.get("PRESENCE_TABLE_NAME", "").strip()
    if not table_name:
        logger.error(
            "PRESENCE_TABLE_NAME is unset — not polling and not marking anyone away"
        )
        return {"status": "skipped", "reason": "presence table unset"}

    table = aws_clients.get_dynamodb_resource().Table(table_name)
    secret_name = os.environ.get("EERO_SESSION_SECRET_NAME", "").strip()
    if not secret_name:
        logger.error(
            "EERO_SESSION_SECRET_NAME is blank — not polling and not marking anyone away"
        )
        return _mark_stale(table, "eero session secret name is blank")

    try:
        token = load_session_token(secret_name)
    except Exception as exc:
        code = ""
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = str((response.get("Error") or {}).get("Code") or "")
        logger.error(
            "eero session secret unreadable (%s %s) — keeping last presence and "
            "marking it stale. Not marking anyone away.",
            exc.__class__.__name__,
            code,
        )
        return _mark_stale(table, "eero session secret unreadable")
    if not token:
        logger.error(
            "eero session secret has no session_token — keeping last presence and "
            "marking it stale. Not marking anyone away. Re-run scripts/eero_login.py."
        )
        return _mark_stale(table, "eero session secret has no session_token")

    network_id = os.environ.get("EERO_NETWORK_ID", "").strip() or None
    try:
        clients = eero_client.list_connected_clients(token, network_id=network_id)
    except eero_client.EeroAuthError as exc:
        logger.error(
            "eero session rejected (%s) — keeping last presence and marking it stale. "
            "Not marking anyone away. Re-run scripts/eero_login.py.",
            exc,
        )
        return _mark_stale(table, f"eero session rejected: {exc}")
    except eero_client.EeroError as exc:
        logger.error(
            "eero poll failed (%s) — keeping last presence and marking it stale. "
            "Not marking anyone away.",
            exc,
        )
        return _mark_stale(table, f"eero poll failed: {exc}")

    tracked_name = os.environ.get("EERO_TRACKED_TABLE", "").strip()
    tracked = _load_tracked(tracked_name)
    if not tracked:
        logger.error(
            "no tracked eero devices configured — keeping last presence and marking "
            "it stale. Not marking anyone away. Run scripts/eero_login.py track."
        )
        return _mark_stale(table, "no tracked eero devices configured")

    previous = presence_store.read_home(table)
    now = _now()
    updated = eero_presence.compute_presence(
        previous,
        tracked,
        clients,
        miss_threshold=_miss_threshold(),
        now=now,
    )
    if updated is None:
        logger.error(
            "tracked eero devices were unusable — keeping last presence and marking "
            "it stale. Not marking anyone away."
        )
        return _mark_stale(table, "tracked eero devices were unusable")

    presence_store.write_home(table, updated)
    logger.info(
        "eero presence updated is_home=%s freshness=%s zone=%s people=%s",
        updated.get("is_home"),
        updated.get("freshness"),
        updated.get("zone"),
        [person.get("person") for person in updated.get("people") or []],
    )
    return {
        "status": "updated",
        "is_home": bool(updated.get("is_home")),
        "freshness": "fresh",
        "zone": updated.get("zone"),
    }


def load_session_token(secret_name: str, client: Any = None) -> str:
    """Read the session token. GetSecretValue only; the value is never logged."""
    if client is None:
        import boto3

        client = boto3.client("secretsmanager")
    resp = client.get_secret_value(SecretId=secret_name)
    return eero_client.parse_secret_string(resp.get("SecretString") or "")


def _mark_stale(table: Any, reason: str) -> Dict[str, Any]:
    previous = presence_store.read_home(table)
    was_home = True if not previous else bool(previous.get("is_home", True))
    item = presence_store.apply_stale(previous, reason, _now())
    # Belt: this path keeps the last occupancy and never invents an away.
    if was_home:
        item["is_home"] = True
    presence_store.write_home(table, item)
    logger.info(
        "eero presence left unchanged is_home=%s freshness=stale reason=%s",
        item.get("is_home"),
        reason,
    )
    return {
        "status": "stale",
        "is_home": bool(item.get("is_home")),
        "freshness": "stale",
        "reason": reason,
    }


def _load_tracked(table_name: str) -> List[Dict[str, Any]]:
    if not table_name:
        return []
    table = aws_clients.get_dynamodb_resource().Table(table_name)
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items") or [])
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def _miss_threshold() -> int:
    raw = os.environ.get("EERO_AWAY_MISS_THRESHOLD", "3").strip() or "3"
    try:
        value = int(raw)
    except ValueError:
        return 3
    return value if value >= 1 else 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
