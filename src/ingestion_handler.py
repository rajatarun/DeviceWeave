"""
DeviceWeave ingestion Lambda handler.

Accepts three invocation sources with the same provider/mode payload:

  1. POST /ingest (HTTP API)
       event.requestContext.http.method = "POST"
       event.body = '{"provider": "kasa", "mode": "full"}'

  2. EventBridge schedule (CloudWatch Events Rule)
       event = {"provider": "kasa", "mode": "delta"}
       (set via the Rule's Input JSON field)

  3. Direct Lambda invocation / test event
       event = {"provider": "kasa", "mode": "full"}

Defaults: provider="kasa", mode="delta"

The handler is synchronous from Lambda's perspective.  Asyncio is run via
asyncio.run() which creates a fresh event loop per invocation.  Discovery
can take 5–15 s; the Lambda timeout is set to 120 s in template.yaml,
which is more than enough headroom for large fleets.

POST /ingest returns HTTP 202 Accepted immediately with the full result JSON.
EventBridge and direct invocations return the result dict directly.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

from ingestion.pipeline import IngestionPipeline, SyncMode

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.getLogger().setLevel(LOG_LEVEL)  # basicConfig is a no-op in Lambda
for _noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_VALID_MODES = {m.value for m in SyncMode}


def handler(event: Dict[str, Any], context: Any) -> Any:
    """Lambda entry point — dispatch to _run() and shape the response."""
    payload = _extract_payload(event)

    provider: str = payload.get("provider", "kasa").strip().lower()
    mode: str = payload.get("mode", "delta").strip().lower()
    refresh_token: str = payload.get("refresh_token", "").strip()

    if mode not in _VALID_MODES:
        return _http_error(400, f"Invalid mode '{mode}'. Must be one of: {sorted(_VALID_MODES)}.")

    if provider == "ring":
        from ingestion.providers.ring_discovery import inject_refresh_token, RingTwoFactorRequired
        if refresh_token:
            inject_refresh_token(refresh_token)
            logger.info("Ring refresh_token injected from request body.")

    logger.info("Ingestion triggered — provider=%s mode=%s", provider, mode)

    try:
        pipeline = IngestionPipeline(provider, mode)
        result = asyncio.run(pipeline.run())
    except ValueError as exc:
        return _http_error(400, str(exc))
    except Exception as exc:
        if provider == "ring" and type(exc).__name__ == "RingTwoFactorRequired":
            email = getattr(exc, "email", "your_email")
            body = {
                "status": "2fa_required",
                "message": (
                    "Ring requires 2FA. Run the following commands locally to obtain "
                    "your refresh_token, then re-call /ingest with it."
                ),
                "instructions": [
                    f'export RING_EMAIL="{email}"',
                    'export RING_PASS="your_password"',
                    (
                        "python3 -c \""
                        "import os; from ring_doorbell import Auth; "
                        "auth=Auth('DeviceWeave/1.0', None, lambda: input('2FA code: ')); "
                        "auth.fetch_token(os.environ['RING_EMAIL'], os.environ['RING_PASS']); "
                        "print('refresh_token:', auth.token['refresh_token'])"
                        "\""
                    ),
                ],
                "next_step": (
                    'POST /ingest {"provider":"ring","mode":"full",'
                    '"refresh_token":"<token from above>"}'
                ),
            }
            if "requestContext" in event:
                return {
                    "statusCode": 200,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps(body),
                }
            return body
        logger.exception("Ingestion pipeline failed")
        return _http_error(502, f"Pipeline error: {exc}")

    result_dict = result.to_dict()
    logger.info("Ingestion result: %s", result_dict)

    # If this came from API Gateway, return an HTTP response shape.
    if "requestContext" in event:
        return {
            "statusCode": 202,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(result_dict),
        }

    # EventBridge or direct invocation — return the dict directly.
    return result_dict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_payload(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalise the event shape from all three invocation sources into a
    plain {provider, mode} dict.
    """
    if "requestContext" in event:
        # API Gateway invocation — payload is in the request body.
        raw = event.get("body") or "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    # EventBridge or direct invocation — payload IS the event.
    return event


def _http_error(status: int, message: str) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": message}),
    }
