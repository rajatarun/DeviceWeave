"""
Memgraph graph engine — Bolt client for behavior events and scene graphs.

Connection is lazy and optional: every public function returns a safe default
when no Memgraph instance is reachable.  The rest of the system never sees an
exception from this module.

Host resolution (runtime):
  1. MEMGRAPH_HOST env var — static override, used as-is when set.
  2. EC2 tag discovery — queries EC2 for a running instance tagged
     memgraph=true and uses its PrivateIpAddress.

Memgraph is a Spot instance: it stops when interrupted and can come back on a
new IP. So nothing about its absence is cached for the container's lifetime.
A failed discovery, a failed connect, or a lost connection (the driver's
ServiceUnavailable / SessionExpired, or a socket error) is retried after
MEMGRAPH_RETRY_SECONDS (default 60). Until then every call returns its safe
default at once, with no EC2 call and no connect timeout. A lost connection
also forgets the host, so the retry rediscovers it.

Credentials come from Secrets Manager (MEMGRAPH_SECRET_ARN):
    {"username": "memgraph", "password": "..."}
If the secret is absent the driver connects unauthenticated (Memgraph default).

record_event() is fire-and-forget via a module-level ThreadPoolExecutor: it
never blocks the Lambda response.  Behavior READ queries
(query_behavior_history, query_top_actions) remain synchronous because they
inform confidence scoring.

Schema (Cypher, created on first use):
    (:Device {device_id})
    (:Device)-[:HAD_EVENT]->(:BehaviorEvent {action, hour, day_of_week, command, ts})
"""

import concurrent.futures
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MEMGRAPH_HOST_OVERRIDE: str = os.environ.get("MEMGRAPH_HOST", "")
_MEMGRAPH_PORT: int = int(os.environ.get("MEMGRAPH_PORT", "7687"))
_MEMGRAPH_SECRET_ARN: str = os.environ.get("MEMGRAPH_SECRET_ARN", "")

_RETRY_SECONDS: float = float(os.environ.get("MEMGRAPH_RETRY_SECONDS", "60"))

_driver = None
_cred_cache: Optional[Dict[str, str]] = None
_resolved_host: Optional[str] = None  # None = not yet resolved; "" = not found
_host_lock = threading.Lock()
_retry_after: float = 0.0  # _clock() before which no discovery or connect is tried
_clock = time.monotonic

# Module-level executor: persists across Lambda warm-container reuses.
# Lambda freezes (not kills) the process after each invocation, so work
# submitted here can complete before the next request unfreezes the container.
# max_workers=1 keeps Bolt writes sequential on a single-node Memgraph.
_graph_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="graph-write"
)


# ---------------------------------------------------------------------------
# Runtime EC2 host discovery
# ---------------------------------------------------------------------------

def _resolve_host() -> str:
    """
    Return the Memgraph private IP, or "" when there is none right now.

    Priority:
      1. MEMGRAPH_HOST env var (static override from CloudFormation / local dev).
      2. EC2 describe-instances filtered by tag memgraph=true + running state.

    A found IP is kept until the connection to it is lost. "Not found" is
    kept only until _retry_after.
    """
    global _resolved_host, _retry_after
    if _MEMGRAPH_HOST_OVERRIDE:
        return _MEMGRAPH_HOST_OVERRIDE
    if _resolved_host or (_resolved_host == "" and _clock() < _retry_after):
        return _resolved_host
    with _host_lock:
        if _resolved_host or (_resolved_host == "" and _clock() < _retry_after):
            return _resolved_host
        try:
            import boto3
            resp = boto3.client("ec2").describe_instances(
                Filters=[
                    {"Name": "tag:memgraph", "Values": ["true"]},
                    {"Name": "instance-state-name", "Values": ["running"]},
                ]
            )
            reservations = resp.get("Reservations", [])
            ip = ""
            if reservations:
                ip = reservations[0]["Instances"][0].get("PrivateIpAddress", "") or ""
            _resolved_host = ip
            if ip:
                logger.info("Memgraph EC2 discovered at %s (tag memgraph=true)", ip)
            else:
                _retry_after = _clock() + _RETRY_SECONDS
                logger.warning(
                    "No running EC2 instance with tag memgraph=true — behavior scoring "
                    "disabled; retrying in %.0fs", _RETRY_SECONDS,
                )
        except Exception as exc:
            _resolved_host = ""
            _retry_after = _clock() + _RETRY_SECONDS
            logger.warning(
                "EC2 Memgraph host discovery failed: %s — behavior scoring disabled; "
                "retrying in %.0fs", exc, _RETRY_SECONDS,
            )
    return _resolved_host


def _connection_lost(exc: BaseException) -> bool:
    """True for errors that mean the server is gone, not that a query was bad."""
    if isinstance(exc, OSError):
        return True
    try:
        from neo4j.exceptions import ServiceUnavailable, SessionExpired
    except ImportError:  # pragma: no cover - neo4j is a deployed dependency
        return False
    return isinstance(exc, (ServiceUnavailable, SessionExpired))


def _drop_connection() -> None:
    """Forget the driver and the host, and wait _RETRY_SECONDS before trying again."""
    global _driver, _resolved_host, _retry_after
    with _host_lock:
        driver, _driver = _driver, None
        _resolved_host = None
        _retry_after = _clock() + _RETRY_SECONDS
    if driver is not None:
        try:
            driver.close()
        except Exception:  # noqa: BLE001 - closing a dead driver
            pass


def _on_query_error(where: str, exc: Exception) -> None:
    if _connection_lost(exc):
        _drop_connection()
        logger.warning("graph_engine.%s: Memgraph connection lost (%s) — retrying in %.0fs",
                       where, type(exc).__name__, _RETRY_SECONDS)
    else:
        logger.warning("graph_engine.%s failed: %s", where, exc)


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
    global _driver, _retry_after
    if _driver is not None:
        return _driver
    if _clock() < _retry_after:
        return None
    host = _resolve_host()
    if not host:
        return None
    driver = None
    try:
        from neo4j import GraphDatabase
        creds = _get_credentials()
        auth = (creds["username"], creds["password"]) if creds["password"] else None
        driver = GraphDatabase.driver(
            f"bolt://{host}:{_MEMGRAPH_PORT}",
            auth=auth,
            connection_timeout=3,
        )
        # The driver connects lazily; the schema check is the first real
        # round trip. Keep the driver only once it has answered.
        _ensure_schema(driver)
        _driver = driver
        logger.info("Memgraph connected — %s:%d", host, _MEMGRAPH_PORT)
        return _driver
    except Exception as exc:
        if driver is not None:
            try:
                driver.close()
            except Exception:  # noqa: BLE001 - closing a driver that never connected
                pass
        # Every request would otherwise pay the connect timeout again.
        # Forget the host too: a replaced instance has a new IP.
        _drop_connection()
        logger.warning("Memgraph unavailable (%s) — behavior scoring disabled; "
                       "retrying in %.0fs", exc, _RETRY_SECONDS)
        return None


def _ensure_schema(driver) -> None:
    """Create indexes on first connection so queries stay fast."""
    with driver.session() as session:
        _create_index_if_missing(session, "CREATE INDEX ON :Device(device_id);")
        _create_index_if_missing(
            session, "CREATE INDEX ON :BehaviorEvent(action);"
        )


def _create_index_if_missing(session, statement: str) -> None:
    """
    Run Memgraph index DDL in a backwards-compatible, idempotent way.

    Some Memgraph versions reject `IF NOT EXISTS` in `CREATE INDEX` syntax.
    We execute vanilla `CREATE INDEX` and tolerate "already exists" errors.
    """
    try:
        session.run(statement)
    except Exception as exc:
        msg = str(exc).lower()
        if "already exists" in msg or "equivalent index already exists" in msg:
            logger.debug("Index already exists for statement: %s", statement)
            return
        raise


# ---------------------------------------------------------------------------
# Write — record a behaviour event after successful execution
# ---------------------------------------------------------------------------

def record_event(device_id: str, action: str, command: str) -> None:
    """
    Fire-and-forget: submit a behavior event write to the module-level executor
    and return immediately so the Lambda response is never delayed by Memgraph.

    The executor persists across Lambda warm-container reuses: work submitted
    here can finish during the frozen container window before the next request
    arrives.  Best-effort — silently skipped when Memgraph is unavailable.
    """
    _graph_executor.submit(_write_event, device_id, action, command)


def _write_event(device_id: str, action: str, command: str) -> None:
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
        _on_query_error("record_event", exc)


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
        _on_query_error("query_behavior_history", exc)
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
        _on_query_error("query_top_actions", exc)
        return []


def is_available() -> bool:
    """Return True if a Memgraph connection is active."""
    return _get_driver() is not None
