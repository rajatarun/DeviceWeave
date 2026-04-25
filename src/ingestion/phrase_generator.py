"""
Claude-powered sample phrase generator for discovered devices.

Calls Claude Haiku 4.5 via Bedrock cross-region inference to produce
natural-language control phrases for each device, then writes them to
the learning table so the device resolver picks them up automatically.

Phrases are written with source="generated" and only if the phrase does
not already exist (attribute_not_exists condition), so repeated ingestion
runs are safe and manual/learned phrases are never overwritten.
"""

import logging
import os
from datetime import datetime, timezone
from typing import List

from llm_provider import get_llm_provider

logger = logging.getLogger(__name__)

_LEARNING_TABLE = os.environ.get("LEARNING_TABLE_NAME", "")

_SYSTEM_PROMPT = """\
You are a home automation command interpreter. Your job is to produce training \
phrases that a real person would speak or type to control a smart home device.

Rules:
- Output ONLY a raw JSON array of strings. No markdown, no code fences, no explanation.
- Each phrase must be something a person would naturally say to a voice assistant or \
chat interface (e.g. Alexa, Google Home, or a custom assistant).
- Cover a variety of intents: turning on/off, checking status, adjusting settings \
(brightness, speed, colour temperature), and situational/contextual triggers.
- Mix short commands ("lights off"), full sentences ("can you turn the fan on?"), \
and context phrases ("it's too dark in here", "getting warm").
- Include phrases that mention the device by name AND phrases that don't \
(context-only, location-only).
- Do NOT include phrases about schedules, timers, or routines.

Example output format:
["turn on the kitchen light", "switch off the fan", "dim the bedroom light to 50%", \
"it's too bright", "I'm cold, turn on the heater"]"""


def generate_phrases(name: str, device_type: str, capabilities: List[str]) -> List[str]:
    """Ask the configured LLM provider for sample control phrases. Returns [] on any error."""
    import json

    cap_list = ", ".join(capabilities)
    location_hint = ""
    name_lower = name.lower()
    for word in ("bedroom", "living", "kitchen", "office", "bathroom",
                 "garage", "staircase", "gameroom", "hallway", "basement"):
        if word in name_lower:
            location_hint = f"  Location context: {word}\n"
            break

    user_msg = (
        f"Generate 15 home automation control phrases for this device:\n"
        f"  Device name: {name}\n"
        f"  Device type: {device_type}\n"
        f"  Capabilities: {cap_list}\n"
        f"{location_hint}"
        f"\n"
        f"Include: on/off commands, status queries, capability-specific commands "
        f"({'brightness levels, dimming' if 'set_brightness' in capabilities else ''}"
        f"{'colour and colour temperature' if 'set_color' in capabilities else ''}"
        f"), and situational phrases a person might say when they want this device "
        f"to act without naming it directly.\n"
        f"\n"
        f"Return a raw JSON array of strings only."
    )

    try:
        llm = get_llm_provider()
        text = llm.invoke(_SYSTEM_PROMPT, user_msg, max_tokens=768)
        # Strip markdown code fences if the model wrapped the JSON
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]  # drop opening fence line
            text = text.rsplit("```", 1)[0]  # drop closing fence
            text = text.strip()
        if not text:
            logger.warning("Bedrock returned empty text for %r — skipping.", name)
            return []
        phrases = json.loads(text)
        if not isinstance(phrases, list):
            raise ValueError(f"Expected list, got {type(phrases).__name__}")
        return [p.strip() for p in phrases if isinstance(p, str) and p.strip()]
    except Exception as exc:
        logger.error("Phrase generation failed for %r: %s", name, exc, exc_info=True)
        return []


def save_generated_phrases(device_id: str, phrases: List[str]) -> int:
    """Write phrases to the learning table. Skips duplicates. Returns count saved."""
    if not _LEARNING_TABLE:
        logger.error(
            "LEARNING_TABLE_NAME env var is not set — cannot save phrases for %s.", device_id
        )
        return 0
    if not phrases:
        logger.warning("No phrases to save for %s.", device_id)
        return 0

    logger.info(
        "Writing %d phrases for device %s to table %s…", len(phrases), device_id, _LEARNING_TABLE
    )

    import boto3
    from botocore.exceptions import ClientError

    table = boto3.resource("dynamodb").Table(_LEARNING_TABLE)
    now = datetime.now(timezone.utc).isoformat()
    saved = skipped = 0

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
            code = exc.response["Error"]["Code"]
            if code == "ConditionalCheckFailedException":
                skipped += 1
            elif code == "AccessDeniedException":
                logger.error(
                    "DynamoDB AccessDenied writing to %s — role missing DynamoDBCrudPolicy "
                    "for LearningTable. Patch the role or redeploy the stack. phrase=%r",
                    _LEARNING_TABLE, normalized,
                )
                break
            else:
                logger.warning(
                    "Failed to save phrase %r for %s: %s", normalized, device_id, exc
                )

    logger.info(
        "Phrases for %s — total=%d saved=%d skipped(dup)=%d",
        device_id, len(phrases), saved, skipped,
    )
    return saved


def enrich_device(device_id: str, name: str, device_type: str, capabilities: List[str]) -> int:
    """Generate and persist sample phrases for one device. Returns count of new phrases saved.

    Skips the Bedrock call entirely if the device already has any phrases in the
    learning table — handles re-ingestion of already-discovered devices without
    wasting model calls.
    """
    if _has_phrases(device_id):
        logger.debug("Device %s already has phrases — skipping generation.", device_id)
        return 0
    logger.info("Generating phrases for %s (%s)…", device_id, name)
    phrases = generate_phrases(name, device_type, capabilities)
    if phrases:
        logger.info("Claude generated %d phrases for %s: %s", len(phrases), name, phrases)
    return save_generated_phrases(device_id, phrases)


def _has_phrases(device_id: str) -> bool:
    """Return True if the learning table already has at least one phrase for this device."""
    if not _LEARNING_TABLE:
        return False
    import boto3
    from boto3.dynamodb.conditions import Key
    try:
        resp = boto3.resource("dynamodb").Table(_LEARNING_TABLE).query(
            KeyConditionExpression=Key("device_id").eq(device_id),
            Limit=1,
            ProjectionExpression="phrase",
        )
        return bool(resp.get("Items"))
    except Exception as exc:
        logger.warning("_has_phrases check failed for %s: %s — will generate", device_id, exc)
        return False
