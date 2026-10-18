"""
Invoke the room-calibration Lambda.

MCP tools run inside the VPC and must not write the fingerprint table.
They call the calibration function, which is the only writer. ``start``
returns as soon as that function has queued the sample run.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict


def invoke(action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    name = os.environ.get("EERO_CALIBRATION_FUNCTION_NAME", "").strip()
    if not name:
        return _result(503, {"error": "Room calibration is not configured"})
    import boto3

    resp = boto3.client("lambda").invoke(
        FunctionName=name,
        InvocationType="RequestResponse",
        Payload=json.dumps({"action": action, **payload}).encode("utf-8"),
    )
    if resp.get("FunctionError"):
        return _result(502, {"error": "Room calibration failed"})
    raw = resp["Payload"].read()
    if not raw:
        return _result(502, {"error": "Room calibration returned nothing"})
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return _result(502, {"error": "Room calibration returned nothing"})
    if isinstance(parsed, dict) and "statusCode" in parsed:
        return parsed
    return _result(200, parsed if isinstance(parsed, dict) else {"result": parsed})


def _result(status: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }
