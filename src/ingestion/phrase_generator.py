"""
Claude-powered sample phrase generator for discovered devices.

Calls Claude Haiku 4.5 via Bedrock cross-region inference to produce
natural-language control phrases for each device, then writes them to
the learning table so the device resolver picks them up automatically.

Phrases are written with source="generated" and only if the phrase does
not already exist (attribute_not_exists condition), so repeated ingestion
runs are safe and manual/learned phrases are never overwritten.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import List

logger = logging.getLogger(__name__)

_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_LEARNING_TABLE = os.environ.get("LEARNING_TABLE_NAME", "")

_SYSTEM_PROMPT = (
    "You are a smart home voice assistant expert. Generate diverse natural-language "
    "phrases a user might say to control a smart home device. Vary formality, "
    "phrasing, and context. Include both device-name phrases and context-only phrases. "
    "Return ONLY a valid JSON array of strings — no explanation, no markdown."
)


def generate_phrases(name: str, device_type: str, capabilities: List[str]) -> List[str]:
    """Ask Claude Haiku 4.5 for sample control phrases. Returns [] on any error."""
    import boto3

    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    user_msg = (
        f"Generate 12 natural-language control phrases for:\n"
        f"  Name: {name}\n"
        f"  Type: {device_type}\n"
        f"  Capabilities: {', '.join(capabilities)}\n\n"
        f"Include on/off phrases and any capability-specific phrases "
        f"(brightness, colour, etc.)."
    )
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 512,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    })

    try:
        resp = client.invoke_model(modelId=_MODEL_ID, body=body)
        text = json.loads(resp["body"].read())["content"][0]["text"].strip()
        phrases = json.loads(text)
        if not isinstance(phrases, list):
            raise ValueError(f"Expected list, got {type(phrases).__name__}")
        return [p.strip() for p in phrases if isinstance(p, str) and p.strip()]
    except Exception as exc:
        logger.error("Phrase generation failed for %r: %s", name, exc, exc_info=True)
        return []


def save_generated_phrases(device_id: str, phrases: List[str]) -> int:
    """Write phrases to the learning table. Skips duplicates. Returns count saved."""
    if not _LEARNING_TABLE or not phrases:
        return 0

    import boto3
    from botocore.exceptions import ClientError

    table = boto3.resource("dynamodb").Table(_LEARNING_TABLE)
    now = datetime.now(timezone.utc).isoformat()
    saved = 0

    for phrase in phrases:
        normalized = phrase.lower()
        try:
            table.put_item(
                Item={
                    "device_id": device_id,
                    "phrase": normalized,
                    "source": "generated",
                    "confidence": "1.0",
                    "created_at": now,
                    "use_count": 0,
                },
                ConditionExpression="attribute_not_exists(phrase)",
            )
            saved += 1
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                logger.warning(
                    "Failed to save phrase %r for %s: %s", normalized, device_id, exc
                )

    logger.info(
        "Phrases for %s — generated=%d saved=%d skipped=%d",
        device_id, len(phrases), saved, len(phrases) - saved,
    )
    return saved


def enrich_device(device_id: str, name: str, device_type: str, capabilities: List[str]) -> int:
    """Generate and persist sample phrases for one device. Returns count of new phrases saved."""
    logger.info("Generating phrases for %s (%s)…", device_id, name)
    phrases = generate_phrases(name, device_type, capabilities)
    if phrases:
        logger.info("Claude generated %d phrases for %s: %s", len(phrases), name, phrases)
    return save_generated_phrases(device_id, phrases)
