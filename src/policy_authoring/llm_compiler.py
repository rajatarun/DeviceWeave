"""
LLM Policy Compiler — natural language → Policy DSL JSON via Claude Sonnet.

The LLM acts as a *compiler*, not a classifier. It receives a natural language
IoT automation rule and must emit a structurally complete Policy DSL object that
conforms exactly to the allowed schema — or an explicit rejection object when
the rule is ambiguous, incomplete, or maps to concepts outside the schema.

Design decisions:
- Claude Sonnet 4.5 is used over Haiku: policy compilation requires nuanced
  semantic understanding and schema adherence — accuracy outweighs cost here.
- The system prompt is prescriptive, not suggestive: every field, every allowed
  value, and every rejection trigger is enumerated explicitly.
- The model is instructed to return raw JSON only. Markdown fences are stripped
  defensively in case the model ignores the instruction (rare but possible).
- A failed JSON parse returns None; callers treat None as a hard 502 error
  rather than a silent rejection to distinguish "LLM broken" from "rule rejected".

Cost profile: ~300–500 tokens per rule at Sonnet 4.5 rates (~$0.003/call).
"""

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

_SYSTEM_PROMPT = """\
You are a Policy Compiler for DeviceWeave, an IoT home automation platform.
Your ONLY task is to convert a natural language automation rule into a strict \
Policy DSL JSON object.

## OUTPUT SCHEMA — your response MUST exactly follow this structure:

{
  "rule_id": "auto",
  "scope": {
    "device_type": "<EXACTLY ONE OF: fan | light | ac | plug | heater>"
  },
  "conditions": [
    {
      "field": "<EXACTLY ONE OF: temperature | humidity | time_hour | is_home>",
      "operator": "<EXACTLY ONE OF: > | < | >= | <= | == | !=",
      "value": <number for temperature/humidity/time_hour  |  boolean for is_home>
    }
  ],
  "action": {
    "type": "<EXACTLY ONE OF: block | allow | modify>",
    "reason": "<concise explanation — required, must be non-empty>",
    "params": {}
  },
  "confidence": <float 0.0 to 1.0>
}

## STRICT FIELD RULES — violation means you MUST return a rejection:

1.  rule_id        : always "auto" — the system assigns the UUID.
2.  device_type    : MUST be one of: fan, light, ac, plug, heater.
                     No other device types are accepted.
3.  conditions     : MUST contain at least 1 item.  Empty list → reject.
4.  field          : MUST be one of: temperature, humidity, time_hour, is_home.
5.  operator       : MUST be one of: >, <, >=, <=, ==, !=
6.  value types    :
      temperature  → Fahrenheit float/int  (e.g. 65.0 for "cold", 85 for "hot")
      humidity     → percentage float/int  (0-100, e.g. 60 for "humid")
      time_hour    → 24-hour integer       (0-23, e.g. 22 for "10 PM")
      is_home      → boolean               (true or false, NOT a string)
7.  action.type    : MUST be one of: block, allow, modify.
8.  action.reason  : non-empty string explaining the rule's intent.
9.  params         : always an empty object {} unless you have explicit \
modification parameters.
10. confidence     : 0.0 to 1.0.  Use 0.0 only in the rejection format below.

## SEMANTIC REFERENCE (apply these mappings when unambiguous):

  "cold" / "freezing" / "too cold"            → temperature < 65
  "cool"                                       → temperature < 72
  "warm" / "hot" / "too hot" / "summer heat"  → temperature > 85
  "humid" / "stuffy" / "muggy"                → humidity > 60
  "dry"                                        → humidity < 30
  "after 10 PM" / "late night"                → time_hour >= 22
  "after 9 PM"                                → time_hour >= 21
  "in the morning" / "early morning"          → time_hour <= 9
  "at night" / "nighttime"                    → time_hour >= 21
  "nobody home" / "no one home" / "away"      → is_home == false
  "when I'm home" / "when home" / "at home"   → is_home == true
  "don't turn on" / "prevent" / "block"       → action.type = block
  "always on" / "keep on" / "ensure"          → action.type = allow
  "dim" / "reduce" / "lower"                  → action.type = modify

## REJECTION — return this exact structure when you cannot compile reliably:

{
  "rejected": true,
  "reason": "<specific reason — which part of the rule was unresolvable>",
  "confidence": 0.0
}

Reject when:
- The target device is NOT one of: fan, light, ac, plug, heater
  (e.g. "dishwasher", "TV", "thermostat", "doorbell" → reject)
- The condition cannot be mapped to any allowed field
  (e.g. "when it rains", "if the door is open", "when I'm sleeping" → reject)
- Multiple valid interpretations exist with different outcomes
- The rule is grammatically present but semantically empty
  ("do something with the fan" → reject)
- Confidence would be below 0.85 even if you attempted to compile it

## ABSOLUTE RULES:

- Return ONLY a raw JSON object.  NO markdown.  NO code fences.  NO prose.
- The ONLY allowed top-level keys are: rule_id, scope, conditions, action, confidence.
  Do NOT add params, device_type, or any other key at the top level.
  params belongs INSIDE action, nowhere else.
- NEVER include fields not listed in the schema above.
- NEVER invent device types, condition fields, or operators.
- NEVER auto-correct an ambiguous rule — if uncertain, reject.
- If confidence would be < 0.85, return the rejection format instead.
"""


def compile_rule(natural_language_rule: str) -> Optional[Dict[str, Any]]:
    """
    Invoke Claude Sonnet via Bedrock to compile a natural language rule into
    Policy DSL JSON.

    Returns the raw parsed JSON dict from the model (not yet validated by the
    validator layer).  Returns None only on hard infrastructure failure
    (network error, Bedrock unavailable, unparseable model response) so callers
    can distinguish "LLM refused the rule" (rejected dict) from "LLM broken"
    (None → 502).
    """
    if not natural_language_rule or not natural_language_rule.strip():
        return {"rejected": True, "reason": "Empty rule text provided.", "confidence": 0.0}

    user_message = (
        f'Compile this IoT automation rule into Policy DSL JSON:\n\n'
        f'"{natural_language_rule.strip()}"\n\n'
        f'Return only the raw JSON object — no markdown, no explanation.'
    )

    import boto3
    client = boto3.client("bedrock-runtime", region_name="us-east-1")

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 512,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_message}],
    })

    try:
        resp = client.invoke_model(modelId=_MODEL_ID, body=body)
        payload = json.loads(resp["body"].read())
        text = payload["content"][0]["text"].strip()

        # Defensively strip markdown code fences in case the model ignores
        # the instruction (```json ... ``` or ``` ... ```).
        if text.startswith("```"):
            lines = text.splitlines()
            # Drop the opening fence line and the closing fence line
            inner_lines = lines[1:]
            if inner_lines and inner_lines[-1].strip() == "```":
                inner_lines = inner_lines[:-1]
            text = "\n".join(inner_lines).strip()

        raw = json.loads(text)

        logger.info(
            "LLM compiler response: rejected=%s confidence=%s device_type=%s",
            raw.get("rejected", False),
            raw.get("confidence"),
            raw.get("scope", {}).get("device_type") if isinstance(raw.get("scope"), dict) else "N/A",
        )
        return raw

    except json.JSONDecodeError as exc:
        logger.error(
            "LLM compiler produced non-JSON output for rule %r: %s",
            natural_language_rule, exc,
        )
        return None

    except Exception as exc:
        logger.error(
            "LLM compiler Bedrock call failed for rule %r: %s",
            natural_language_rule, exc,
            exc_info=True,
        )
        return None
