"""
DynamoDB-backed conversation store for the Bedrock Converse API agent.

Each session is a single DynamoDB item:
  - session_id  (S)  partition key
  - messages    (S)  JSON-serialised list of Converse API message dicts
  - ttl         (N)  Unix epoch; DynamoDB TTL attribute, 24 h from last write

The TTL attribute must be enabled on the table (ConversationTable in template.yaml).
"""

import json
import logging
import os
import time
from typing import Any, Dict, List
from aws_clients import get_dynamodb_resource

logger = logging.getLogger(__name__)

_TABLE_NAME: str = os.environ.get("CONVERSATION_TABLE_NAME", "")
_SESSION_TTL_SECONDS: int = 86_400  # 24 hours


def load_session(session_id: str) -> List[Dict[str, Any]]:
    """Return stored Converse API messages for session_id, or [] if not found."""
    if not _TABLE_NAME:
        logger.warning("CONVERSATION_TABLE_NAME not set — session not persisted")
        return []
    t0 = time.monotonic()
    try:
        table = get_dynamodb_resource().Table(_TABLE_NAME)
        resp = table.get_item(Key={"session_id": session_id})
        item = resp.get("Item")
        if item is None:
            logger.info("Session not found: session_id=%s elapsed_ms=%.0f", session_id, (time.monotonic() - t0) * 1000)
            return []
        messages = json.loads(item.get("messages", "[]"))
        logger.info(
            "Session loaded: session_id=%s turns=%d elapsed_ms=%.0f",
            session_id, len(messages), (time.monotonic() - t0) * 1000,
        )
        return messages
    except Exception as exc:
        logger.warning(
            "Failed to load session %s: %s elapsed_ms=%.0f — starting fresh",
            session_id, exc, (time.monotonic() - t0) * 1000,
        )
        return []


def save_session(session_id: str, messages: List[Dict[str, Any]]) -> None:
    """Persist the full Converse API message list with a 24-hour TTL."""
    if not _TABLE_NAME:
        return
    t0 = time.monotonic()
    try:
        table = get_dynamodb_resource().Table(_TABLE_NAME)
        table.put_item(Item={
            "session_id": session_id,
            "messages": json.dumps(messages, default=str),
            "ttl": int(time.time()) + _SESSION_TTL_SECONDS,
        })
        logger.info(
            "Session saved: session_id=%s messages=%d elapsed_ms=%.0f",
            session_id, len(messages), (time.monotonic() - t0) * 1000,
        )
    except Exception as exc:
        logger.warning(
            "Failed to save session %s: %s elapsed_ms=%.0f",
            session_id, exc, (time.monotonic() - t0) * 1000,
        )
