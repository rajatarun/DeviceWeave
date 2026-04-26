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

    two_fa_code: str = payload.get("two_fa_code", "").strip()

    if provider == "ring":
        from ingestion.providers.ring_discovery import (
            inject_refresh_token, inject_tokens, get_credentials,
            RingTwoFactorRequired, RingAuthExpired,
        )
        if refresh_token:
            inject_refresh_token(refresh_token)
            logger.info("Ring refresh_token injected from request body.")

        if two_fa_code:
            # Complete 2FA before running the pipeline so it runs exactly once.
            creds = get_credentials() or {}
            try:
                token_data = _ring_complete_2fa(
                    creds.get("email", ""), creds.get("password", ""), two_fa_code
                )
            except Exception as auth_exc:
                logger.exception("Ring 2FA completion failed")
                return _http_error(502, f"Ring 2FA completion failed: {auth_exc}")
            inject_tokens(token_data["access_token"], token_data.get("refresh_token", ""))
            from ingestion.providers.ring_discovery import _persist_refresh_token
            _persist_refresh_token(token_data.get("refresh_token", ""), creds)
            logger.info("Ring 2FA complete — tokens injected and persisted.")

    logger.info("Ingestion triggered — provider=%s mode=%s", provider, mode)

    try:
        pipeline = IngestionPipeline(provider, mode)
        result = asyncio.run(pipeline.run())
    except ValueError as exc:
        return _http_error(400, str(exc))
    except Exception as exc:
        if provider == "ring" and type(exc).__name__ in ("RingTwoFactorRequired", "RingAuthExpired"):
            creds = get_credentials() or {}
            try:
                _ring_trigger_2fa(creds.get("email", ""), creds.get("password", ""))
            except Exception as sms_exc:
                logger.exception("Ring 2FA trigger failed")
                return _http_error(502, f"Ring 2FA trigger failed: {sms_exc}")
            body = {
                "status": "2fa_required",
                "message": "Ring sent a verification code to your registered phone.",
                "next_step": 'POST /ingest {"provider":"ring","mode":"full","two_fa_code":"<code>"}',
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


def _ring_trigger_2fa(email: str, password: str) -> None:
    """Use ring_doorbell to initiate 2FA — Ring sends an SMS, then we stop."""
    from ring_doorbell import Auth

    class _SmsSent(Exception):
        pass

    def _stop():
        raise _SmsSent()

    auth = Auth("DeviceWeave/1.0", None, _stop)
    try:
        auth.fetch_token(email, password)
    except _SmsSent:
        pass  # SMS sent — stop here, don't complete auth


def _ring_complete_2fa(email: str, password: str, two_fa_code: str) -> Dict[str, Any]:
    """Use ring_doorbell to complete 2FA and return the token dict."""
    from ring_doorbell import Auth

    auth = Auth("DeviceWeave/1.0", None, lambda: two_fa_code)
    auth.fetch_token(email, password)
    return auth.token


def _http_error(status: int, message: str) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": message}),
    }
