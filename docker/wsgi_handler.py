"""
WSGI adapter — translates Apache HTTP requests into Lambda-style events
and dispatches to the appropriate DeviceWeave handler.

Route table
-----------
  POST   /execute              → app.handler
  POST   /learn                → app.handler
  GET    /health               → app.handler
  GET    /providers            → app.handler
  GET    /devices              → app.handler
  GET    /devices/{id}         → app.handler
  PUT    /devices/{id}         → app.handler
  DELETE /devices/{id}         → app.handler
  GET    /scenes               → app.handler
  DELETE /scenes/{scene_id}    → app.handler
  GET    /learnings            → app.handler
  DELETE /learnings            → app.handler
  GET    /presence             → app.handler
  POST   /presence             → app.handler
  POST   /policies/author      → policy_authoring.handler.handler
  GET    /policies             → policy_authoring.handler.handler
  GET    /policies/{rule_id}   → policy_authoring.handler.handler
  DELETE /policies/{rule_id}   → policy_authoring.handler.handler
  POST   /ingest               → ingestion_handler.handler
"""

import json
import logging
import os
import sys

sys.path.insert(0, "/app")

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy-import handlers so Apache worker startup doesn't fail if a dep is
# missing — the error surfaces cleanly in the HTTP response instead.
# ---------------------------------------------------------------------------

def _load_handlers():
    import app as _execution
    import policy_authoring.handler as _policy
    import ingestion_handler as _ingestion
    return _execution, _policy, _ingestion

_handlers = None

def _get_handlers():
    global _handlers
    if _handlers is None:
        _handlers = _load_handlers()
    return _handlers


# ---------------------------------------------------------------------------
# Event builder
# ---------------------------------------------------------------------------

def _build_event(environ: dict) -> dict:
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path   = environ.get("PATH_INFO", "/")

    # Body
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    body = environ["wsgi.input"].read(length).decode("utf-8") if length > 0 else None

    # Query string → dict
    qs = environ.get("QUERY_STRING", "")
    query_params: dict = {}
    if qs:
        for pair in qs.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                query_params[k] = v

    # Path parameters — extract from known parameterised patterns
    path_params: dict = {}
    segments = [s for s in path.strip("/").split("/") if s]
    # /devices/{device_id}
    if len(segments) == 2 and segments[0] == "devices":
        path_params["device_id"] = segments[1]
    # /scenes/{scene_id}
    if len(segments) == 2 and segments[0] == "scenes":
        path_params["scene_id"] = segments[1]
    # /policies/{rule_id}  (but not /policies/author)
    if len(segments) == 2 and segments[0] == "policies" and segments[1] != "author":
        path_params["rule_id"] = segments[1]

    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "body": body,
        "queryStringParameters": query_params or None,
        "pathParameters": path_params or None,
    }


# ---------------------------------------------------------------------------
# Response serialiser
# ---------------------------------------------------------------------------

_STATUS_TEXT = {
    200: "200 OK",
    201: "201 Created",
    202: "202 Accepted",
    400: "400 Bad Request",
    403: "403 Forbidden",
    404: "404 Not Found",
    409: "409 Conflict",
    422: "422 Unprocessable Entity",
    500: "500 Internal Server Error",
    502: "502 Bad Gateway",
    503: "503 Service Unavailable",
}


def _respond(lambda_resp: dict, start_response):
    code    = lambda_resp.get("statusCode", 200)
    headers = lambda_resp.get("headers", {"Content-Type": "application/json"})
    body    = (lambda_resp.get("body") or "{}").encode("utf-8")

    # Always add CORS headers for local browser requests
    headers.setdefault("Access-Control-Allow-Origin",  "*")
    headers.setdefault("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
    headers.setdefault("Access-Control-Allow-Headers", "Content-Type, Authorization")

    start_response(
        _STATUS_TEXT.get(code, f"{code} Unknown"),
        list(headers.items()),
    )
    return [body]


# ---------------------------------------------------------------------------
# WSGI application
# ---------------------------------------------------------------------------

def application(environ, start_response):
    # Handle CORS preflight
    if environ.get("REQUEST_METHOD") == "OPTIONS":
        start_response("204 No Content", [
            ("Access-Control-Allow-Origin",  "*"),
            ("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type, Authorization"),
            ("Content-Length", "0"),
        ])
        return [b""]

    event = _build_event(environ)
    path  = environ.get("PATH_INFO", "/")

    try:
        execution, policy, ingestion = _get_handlers()

        if path.startswith("/policies"):
            resp = policy.handler(event, None)
        elif path.startswith("/ingest"):
            resp = ingestion.handler(event, None)
        else:
            resp = execution.handler(event, None)

    except Exception as exc:
        logger.exception("Unhandled error in WSGI adapter")
        resp = {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(exc)}),
        }

    return _respond(resp, start_response)
