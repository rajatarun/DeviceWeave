"""
AWS End User Messaging two-way SMS handler.

Flow
----
  User SMS  →  End User Messaging phone number  →  SNS topic  →  this Lambda
  this Lambda  →  bedrock_agent.run_agent()  →  send_text_message  →  User SMS

AWS End User Messaging replaced Amazon Pinpoint SMS.
boto3 client: pinpoint-sms-voice-v2  (NOT the legacy "pinpoint" client)

Conversation pattern
--------------------
The sender's E.164 phone number is used as the session_id (prefixed "sms:")
so every user gets a persistent conversation context in ConversationTable —
the same DynamoDB table used by the HTTP conversational path.
Each new SMS continues the conversation from where it left off.

Users can text "reset", "new", or "clear" to start a fresh conversation.

Low-cost design
---------------
- End User Messaging long code:  ~$1/month per number (us-east-1)
- Inbound SMS:                   ~$0.0075 per message
- Outbound SMS:                  ~$0.0075 per message
- Lambda + DynamoDB:             negligible at household scale
- No new tables:                 reuses ConversationTable from the HTTP path

Response length
---------------
Replies are constrained to ≤160 chars (one SMS part) by the system prompt.
A hard cap of 1600 chars (10 parts) guards against edge-case runaway replies.

Environment variables (set by template.yaml)
--------------------------------------------
  EUM_ORIGINATION_NUMBER  — E.164 number registered in End User Messaging
  CONVERSATION_TABLE_NAME — DynamoDB table (shared with HTTP path)
  DEVICE_REGISTRY_TABLE   — required by device_resolver inside run_agent
  LEARNING_TABLE_NAME     — required by learning_store inside run_agent
  POLICY_TABLE_NAME       — required by policy_engine inside run_agent
  PRESENCE_TABLE_NAME     — required by context_provider inside run_agent
  SCENE_TABLE_NAME        — required by scene_catalog inside run_agent
  LLM_MODEL_ID            — Bedrock model (default Haiku 4.5)
  AWS_REGION              — AWS region
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

_ORIGINATION_NUMBER: str = os.environ.get("EUM_ORIGINATION_NUMBER", "")
_REGION: str = os.environ.get("AWS_REGION", "us-east-1")
_MAX_REPLY_CHARS: int = 1600  # hard cap — 10 SMS parts at 160 chars each

# Appended to the base system prompt to enforce SMS-friendly brevity
_SMS_PROMPT_SUFFIX = """

SMS mode: You are replying via SMS text message.
- Keep every reply under 160 characters when possible — one SMS part is ideal.
- Never use markdown, bullet points, headers, or emoji.
- Use plain, conversational English only.
- If the action succeeded, say so in one short sentence.
- If something failed or is unclear, say so plainly and ask one short question.
"""

# Keywords that reset the conversation without invoking the agent
_RESET_KEYWORDS = {"reset", "new", "start over", "clear", "restart"}


def handler(event: Dict[str, Any], context: Any) -> None:
    for record in event.get("Records", []):
        _handle_record(record)


def _handle_record(record: Dict[str, Any]) -> None:
    # SNS wraps the End User Messaging event as a JSON string in Sns.Message
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

    # Reset keywords clear the session without touching the agent
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
        return  # don't persist a broken history

    save_session(session_id, updated_history)

    if len(reply) > _MAX_REPLY_CHARS:
        reply = reply[:_MAX_REPLY_CHARS - 3] + "..."

    _send_sms(sender, reply)


def _send_sms(destination: str, message: str) -> None:
    """Send an SMS reply via AWS End User Messaging (pinpoint-sms-voice-v2)."""
    if not _ORIGINATION_NUMBER:
        logger.error("EUM_ORIGINATION_NUMBER not configured — SMS not sent")
        return

    import boto3
    client = boto3.client("pinpoint-sms-voice-v2", region_name=_REGION)

    try:
        resp = client.send_text_message(
            DestinationPhoneNumber=destination,
            OriginationIdentity=_ORIGINATION_NUMBER,
            MessageBody=message,
            MessageType="TRANSACTIONAL",
        )
        logger.info(
            "SMS sent to %s — messageId=%s",
            destination,
            resp.get("MessageId", "unknown"),
        )
    except Exception as exc:
        logger.exception("Failed to send SMS to %s", destination)
