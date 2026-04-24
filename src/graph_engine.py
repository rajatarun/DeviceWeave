"""
Memgraph graph engine — Bolt client for behavior events and scene graphs.

Connection is lazy and optional: every public function returns a safe default
when MEMGRAPH_HOST is unset or the instance is unreachable.  The rest of the
system never sees an exception from this module.

Credentials come from Secrets Manager (MEMGRAPH_SECRET_ARN):
    {"username": "memgraph", "password": "..."}
If the secret is absent the driver connects unauthenticated (Memgraph default).

Schema (Cypher, created on first use):
    (:Device {device_id})
    (:Device)-[:HAD_EVENT]->(:BehaviorEvent {action, hour, day_of_week, command, ts})
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MEMGRAPH_HOST: str = os.environ.get("MEMGRAPH_HOST", "")
_MEMGRAPH_PORT: int = int(os.environ.get("MEMGRAPH_PORT", "7687"))
_MEMGRAPH_SECRET_ARN: str = os.environ.get("MEMGRAPH_SECRET_ARN", "")

_driver = None
_cred_cache: Optional[Dict[str, str]] = None


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _get_credentials() -> Dict[str, str]:
    global _cred_cache
    if _cred_cache is not None:
        return _cred_cache
    if not _MEMGRAPH_SECRET_ARN:
        _cred_cache = {"username": "", "password": ""}
        return _cred_cache
    import boto3
    try:
        resp = boto3.client("secretsmanager").get_secret_value(
            SecretId=_MEMGRAPH_SECRET_ARN
        )
        secret = json.loads(resp["SecretString"])
        _cred_cache = {
            "username": secret.get("username", "memgraph"),
            "password": secret.get("password", ""),
        }
        return _cred_cache
    except Exception as exc:
        logger.warning("Could not load Memgraph credentials: %s — using no-auth", exc)
        _cred_cache = {"username": "", "password": ""}
        return _cred_cache


def _get_driver():
    global _driver
    if _driver is not None:
        return _driver
    if not _MEMGRAPH_HOST:
        return None
    try:
        from neo4j import GraphDatabase
        creds = _get_credentials()
        auth = (creds["username"], creds["password"]) if creds["password"] else None
        _driver = GraphDatabase.driver(
            f"bolt://{_MEMGRAPH_HOST}:{_MEMGRAPH_PORT}",
            auth=auth,
            connection_timeout=3,
        )
        _ensure_schema(_driver)
        logger.info("Memgraph connected — %s:%d", _MEMGRAPH_HOST, _MEMGRAPH_PORT)
        return _driver
    except Exception as exc:
        logger.warning("Memgraph unavailable (%s) — behavior scoring disabled", exc)
        return None


def _ensure_schema(driver) -> None:
    """Create indexes on first connection so queries stay fast."""
    with driver.session() as session:
        session.run(
            "CREATE INDEX ON :Device(device_id) IF NOT EXISTS;"
        )
        session.run(
            "CREATE INDEX ON :BehaviorEvent(action) IF NOT EXISTS;"
        )


# ---------------------------------------------------------------------------
# Write — record a behaviour event after successful execution
# ---------------------------------------------------------------------------

def record_event(device_id: str, action: str, command: str) -> None:
    """
    Persist one behavior event for a device.

    Called after every successful command execution so the behavior engine
    can learn usage patterns over time.
    """
    driver = _get_driver()
    if driver is None:
        return
    now = datetime.now(timezone.utc)
    try:
        with driver.session() as session:
            session.run(
                """
                MERGE (d:Device {device_id: $device_id})
                CREATE (d)-[:HAD_EVENT]->(e:BehaviorEvent {
                    action:      $action,
                    hour:        $hour,
                    day_of_week: $dow,
                    command:     $command,
                    ts:          $ts
                })
                """,
                device_id=device_id,
                action=action,
                hour=now.hour,
                dow=now.weekday(),
                command=command[:200],
                ts=now.isoformat(),
            )
            logger.debug(
                "Behavior event recorded — device=%s action=%s hour=%d",
                device_id, action, now.hour,
            )
    except Exception as exc:
        logger.warning("graph_engine.record_event failed: %s", exc)


# ---------------------------------------------------------------------------
# Read — query historical patterns for scoring
# ---------------------------------------------------------------------------

def query_behavior_history(
    device_id: str,
    action: str,
    hour: int,
    hour_window: int = 2,
) -> Dict[str, int]:
    """
    Return counts of matching vs total behavior events.

    matching — events for this device+action within ±hour_window of current hour
    total    — all events for this device

    Returns {"matching": 0, "total": 0} when Memgraph is unavailable.
    """
    driver = _get_driver()
    if driver is None:
        return {"matching": 0, "total": 0}
    try:
        with driver.session() as session:
            match_result = session.run(
                """
                MATCH (d:Device {device_id: $device_id})-[:HAD_EVENT]->(e:BehaviorEvent)
                WHERE e.action = $action
                  AND abs(e.hour - $hour) <= $window
                RETURN count(e) AS cnt
                """,
                device_id=device_id,
                action=action,
                hour=hour,
                window=hour_window,
            )
            matching = (match_result.single() or {}).get("cnt", 0)

            total_result = session.run(
                """
                MATCH (d:Device {device_id: $device_id})-[:HAD_EVENT]->(e:BehaviorEvent)
                RETURN count(e) AS cnt
                """,
                device_id=device_id,
            )
            total = (total_result.single() or {}).get("cnt", 0)

        return {"matching": int(matching), "total": int(total)}
    except Exception as exc:
        logger.warning("graph_engine.query_behavior_history failed: %s", exc)
        return {"matching": 0, "total": 0}


def query_top_actions(device_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Return the most frequent actions for a device, ordered by frequency.

    Used by the LLM resolver prompt to provide richer context.
    """
    driver = _get_driver()
    if driver is None:
        return []
    try:
        with driver.session() as session:
            result = session.run(
                """
                MATCH (d:Device {device_id: $device_id})-[:HAD_EVENT]->(e:BehaviorEvent)
                RETURN e.action AS action, count(e) AS freq
                ORDER BY freq DESC
                LIMIT $limit
                """,
                device_id=device_id,
                limit=limit,
            )
            return [{"action": r["action"], "freq": r["freq"]} for r in result]
    except Exception as exc:
        logger.warning("graph_engine.query_top_actions failed: %s", exc)
        return []


def is_available() -> bool:
    """Return True if a Memgraph connection is active."""
    return _get_driver() is not None
