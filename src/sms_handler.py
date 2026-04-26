"""
AWS Pinpoint two-way SMS handler.

Flow
----
  User SMS  →  Pinpoint long code  →  SNS topic  →  this Lambda
  this Lambda  →  bedrock_agent.run_agent()  →  Pinpoint SendMessages  →  User SMS

Conversation pattern
--------------------
The sender's E.164 phone number is used as the session_id (prefixed "sms:")
so every user gets a persistent conversation context stored in
ConversationTable (same DynamoDB table as the HTTP conversational path).
Each new SMS continues the conversation from where it left off — no explicit
"new session" gesture is needed from the user.

Users can text "reset" or "new" at any time to start a fresh conversation.

Low-cost design
---------------
- Pinpoint long code:  ~$1/month per number (us-east-1)
- Inbound SMS:         ~$0.0075 per message
- Outbound SMS:        ~$0.0075 per message
- Lambda + DynamoDB:   negligible at household scale
- No new tables:       reuses ConversationTable from the HTTP path

Response length
---------------
SMS parts are 160 chars each (GSM-7) or 153 chars (UCS-2 for emoji/unicode).
The system prompt instructs the agent to reply in ≤ 160 chars so a single
part is the norm.  Responses are hard-capped at 1600 chars (10 parts) to
prevent runaway multi-part chains for unusual edge cases.

Environment variables (set by template.yaml)
--------------------------------------------
  PINPOINT_APP_ID              — Pinpoint application ID
  PINPOINT_ORIGINATION_NUMBER  — E.164 number registered in Pinpoint
  CONVERSATION_TABLE_NAME      — DynamoDB table (shared with HTTP path)
  DEVICE_REGISTRY_TABLE        — required by device_resolver inside run_agent
  LEARNING_TABLE_NAME          — required by learning_store inside run_agent
  POLICY_TABLE_NAME            — required by policy_engine inside run_agent
  PRESENCE_TABLE_NAME          — required by context_provider inside run_agent
  SCENE_TABLE_NAME             — required by scene_catalog inside run_agent
  LLM_MODEL_ID                 — Bedrock model (default Haiku 4.5)
  AWS_REGION                   — AWS region
"""

import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.getLogger().setLevel(LOG_LEVEL)
for _lib in ("botocore", "boto3", "urllib3"):
    logging.getLogger(_lib).setLevel(logging.WARNING)

_PINPOINT_APP_ID: str = os.environ.get("PINPOINT_APP_ID", "")
_ORIGINATION_NUMBER: str = os.environ.get("PINPOINT_ORIGINATION_NUMBER", "")
_REGION: str = os.environ.get("AWS_REGION", "us-east-1")
_MAX_REPLY_CHARS: int = 1600  # hard cap — 10 SMS parts

# Appended to the base system prompt to enforce SMS-friendly output
_SMS_PROMPT_SUFFIX = """

SMS mode: You are replying via SMS text message.
- Keep every reply under 160 characters when possible — one SMS part is ideal.
- Never use markdown, bullet points, headers, or emoji.
- Use plain, conversational English only.
- If the action succeeded say so in one short sentence.
- If something failed or is unclear, say so plainly and ask one short question.
"""

# Keywords that reset the conversation (case-insensitive, exact match)
_RESET_KEYWORDS = {"reset", "new", "start over", "clear", "restart"}


def handler(event: Dict[str, Any], context: Any) -> None:
    for record in event.get("Records", []):
        _handle_record(record)


def _handle_record(record: Dict[str, Any]) -> None:
    # SNS wraps the Pinpoint event as a JSON string in Sns.Message
    try:
        body = json.loads(record["Sns"]["Message"])
    except (KeyError, json.JSONDecodeError) as exc:
        logger.error("Could not parse SNS message: %s", exc)
        return

    sender = body.get("originationNumber", "").strip()
    text = (body.get("messageBody") or "").strip()

    if not sender or not text:
        logger.warning("Missing originationNumber or messageBody — ignoring record")
        return

    logger.info("Inbound SMS from %s: %r", sender, text)

    session_id = f"sms:{sender}"

    from conversation_store import load_session, save_session
    from bedrock_agent import run_agent

    # Handle reset keywords — clear session without calling the agent
    if text.lower() in _RESET_KEYWORDS:
        save_session(session_id, [])
        _send_sms(sender, "Conversation cleared. What would you like to do?")
        logger.info("Session reset for %s", sender)
        return

    history = load_session(session_id)

    try:
        reply, updated_history = run_agent(
            text,
            history,
            system_prompt_extra=_SMS_PROMPT_SUFFIX,
        )
    except Exception as exc:
        logger.exception("Agent error for SMS session %s", session_id)
        _send_sms(sender, "Sorry, something went wrong. Please try again.")
        return  # don't save a broken history

    save_session(session_id, updated_history)

    if len(reply) > _MAX_REPLY_CHARS:
        reply = reply[:_MAX_REPLY_CHARS - 3] + "..."

    _send_sms(sender, reply)


def _send_sms(destination: str, message: str) -> None:
    if not _PINPOINT_APP_ID or not _ORIGINATION_NUMBER:
        logger.error(
            "PINPOINT_APP_ID or PINPOINT_ORIGINATION_NUMBER not configured — SMS not sent"
        )
        return

    import boto3
    client = boto3.client("pinpoint", region_name=_REGION)

    try:
        resp = client.send_messages(
            ApplicationId=_PINPOINT_APP_ID,
            MessageRequest={
                "Addresses": {
                    destination: {"ChannelType": "SMS"}
                },
                "MessageConfiguration": {
                    "SMSMessage": {
                        "Body": message,
                        "MessageType": "TRANSACTIONAL",
                        "OriginationNumber": _ORIGINATION_NUMBER,
                    }
                },
            },
        )
        status = (
            resp.get("MessageResponse", {})
                .get("Result", {})
                .get(destination, {})
                .get("DeliveryStatus", "UNKNOWN")
        )
        logger.info("SMS sent to %s — status=%s", destination, status)
    except Exception as exc:
        logger.exception("Failed to send SMS to %s", destination)
