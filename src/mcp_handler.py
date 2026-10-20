"""
DeviceWeave remote MCP server (Streamable HTTP, protocol 2025-06-18).

POST /mcp
    One JSON-RPC 2.0 message. ``initialize``, ``tools/list``, and
    ``tools/call`` are answered with ``Content-Type: application/json``.
    A notification (no ``id``), including ``notifications/initialized``,
    is answered with HTTP 202 and an empty body. JSON-RPC batches are
    rejected: protocol 2025-06-18 does not include them.

GET /mcp
    Streamable HTTP allows a server-sent event stream here. This server
    does not open one and returns HTTP 405, which the transport permits.

Every /mcp request must send ``Authorization: Bearer <token>``, where the
token is one of:

* a CipherWeave access token (ES256 JWT) with ``aud`` = ``deviceweave``,
  verified locally against CipherWeave's JWKS (``CIPHERWEAVE_JWKS_URL``,
  cached 10 minutes); or
* while ``ACCEPT_LEGACY_TOKEN`` is true (the default, for the migration), the
  old static token: the SecretString of the Secrets Manager secret named by
  ``MCP_AUTH_TOKEN_SECRET_NAME`` (template parameter ``McpAuthTokenSecretName``),
  compared on fixed-length digests with ``hmac.compare_digest``.

``cipherweave_verify.BearerAuth`` makes that decision, the same code the other
Weaves run. A missing or wrong token is HTTP 401 with a JSON-RPC error body.

Authorization is per tool (``cipherweave_verify.ToolAuthorizer``). Every tool
needs one scope: ``read`` (readOnlyHint), ``execute`` (state changes), or
``destructive`` (destructiveHint, plus ``ingest``). Destructive tools also
need **step-up**: a token minted at most 60 seconds ago whose ``jti`` has not
been spent on a step-up call before (``TOKEN_JTI_TABLE``, DynamoDB). The
legacy static token can never step up. ``tools/list`` shows only the tools the
token can call. A refused call is HTTP 403 with ``WWW-Authenticate`` naming
the scope (``insufficient_scope``) or the age (``insufficient_user_authentication``,
``max_age=60``), per RFC 6750 and RFC 9470.

``execute`` takes natural language, so it needs only ``execute`` — but a step
it resolves to that grants physical access (unlock, open) is refused unless
the same token would pass a destructive call (``access_control``).

Every tools/call is logged as one JSON line, ``event: mcp_tool_call``, with
the tool, outcome, and the token's ``sub`` and ``jti``: "who did what" is
answerable per token, not only per Weave.

Tools call the same in-process service functions as the REST handlers.
They do not make HTTP requests back to the API.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional

from cipherweave_verify import (
    DESTRUCTIVE,
    EXECUTE,
    READ,
    AuthorizationError,
    BearerAuth,
    DynamoJtiLedger,
    Principal,
    StepUpRequiredError,
    ToolAuthorizer,
    ToolPolicy,
)

import access_control

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.getLogger().setLevel(LOG_LEVEL)
for _noisy in ("botocore", "boto3", "urllib3", "s3transfer", "neo4j"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
# Header values we accept. initialize always answers 2025-06-18.
# 2025-03-26 is accepted on the header so a client that has not finished
# negotiation is not rejected; the response shape is the 2025-06-18 one.
SUPPORTED_HEADER_VERSIONS = frozenset({PROTOCOL_VERSION, "2025-03-26"})
SERVER_VERSION = "1.0.0"
UNAUTHORIZED_CODE = -32001
FORBIDDEN_CODE = -32003
# The aud a CipherWeave token must carry to be accepted here.
TOKEN_AUDIENCE = "deviceweave"
_TOKEN_CACHE_TTL_SECONDS = 300
_TOKEN_CACHE: Dict[str, Any] = {}

_BEARER_RE = re.compile(r"(?i)bearer\s+(\S+)")

INSTRUCTIONS = (
    "DeviceWeave MCP server. Tools call the same service functions as the HTTP API. "
    "health, list_devices, get_device, list_scenes, list_providers, get_presence, "
    "list_room_fingerprints, list_room_checkins, list_room_schedule_rules, get_learnings, "
    "list_policies, and get_policy read state. "
    "execute sends a natural-language command and can actuate devices. "
    "delete_device, delete_scene, delete_policy, delete_learning, delete_room_fingerprint, "
    "delete_room_checkin, and delete_room_schedule_rule remove state. "
    "create_device, update_device, set_presence, calibrate_room, record_room_checkin, "
    "capture_room_reading, label_room_checkin, set_room_schedule_rule, learn_phrase, "
    "author_policy, and ingest change state. "
    "Each tool needs one CipherWeave token scope: read, execute, or destructive. "
    "Destructive tools, and any unlock or open that execute resolves to, also need a "
    "token minted in the last 60 seconds and not used for such a call before."
)

_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False}
_MUTATING = {"readOnlyHint": False, "destructiveHint": False}
_DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True}


class ParseError(Exception):
    """The HTTP body is not a JSON value."""


class InvalidParams(Exception):
    """tools/call arguments do not match the tool's input schema."""


class McpAuthUnavailable(Exception):
    """The bearer token could not be loaded. Fail closed."""


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def tokens_equal(provided: str, expected: str) -> bool:
    """Constant-time equality for bearer tokens.

    Both strings are hashed to a fixed 32-byte digest first, so the compare
    does not leak either length. ``hmac.compare_digest`` then compares the
    digests without short-circuiting on the first differing byte.
    """
    provided_digest = hashlib.sha256(provided.encode("utf-8")).digest()
    expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(provided_digest, expected_digest)


def _clear_token_cache() -> None:
    _TOKEN_CACHE.clear()


def _secrets_client():
    import boto3

    return boto3.client("secretsmanager")


def _fetch_secret_string(name: str) -> str:
    try:
        response = _secrets_client().get_secret_value(SecretId=name)
    except Exception as exc:
        logger.exception("Failed to read MCP auth secret")
        raise McpAuthUnavailable("MCP authentication is unavailable") from exc
    secret = response.get("SecretString")
    if isinstance(secret, str):
        return secret
    binary = response.get("SecretBinary")
    if isinstance(binary, (bytes, bytearray)):
        return bytes(binary).decode("utf-8")
    if isinstance(binary, str):
        return binary
    return ""


def _load_expected_token() -> str:
    """Return the configured bearer token, cached briefly per execution environment."""
    name = os.environ.get("MCP_AUTH_TOKEN_SECRET_NAME", "").strip()
    if not name:
        raise McpAuthUnavailable("MCP_AUTH_TOKEN_SECRET_NAME is not set")
    now = time.monotonic()
    if (
        _TOKEN_CACHE.get("name") == name
        and _TOKEN_CACHE.get("value")
        and now < float(_TOKEN_CACHE.get("until") or 0)
    ):
        return str(_TOKEN_CACHE["value"])
    token = _fetch_secret_string(name).strip()
    if not token:
        raise McpAuthUnavailable("MCP auth secret is empty")
    _TOKEN_CACHE["name"] = name
    _TOKEN_CACHE["value"] = token
    _TOKEN_CACHE["until"] = now + _TOKEN_CACHE_TTL_SECONDS
    return token


# The legacy loader is looked up at call time, so a request with a good JWT
# never reads Secrets Manager, and tests can replace _load_expected_token.
_AUTH = BearerAuth(TOKEN_AUDIENCE, legacy_token=lambda: _load_expected_token())

_LEDGER_CACHE: Dict[str, Any] = {}


def _jti_ledger() -> Optional[DynamoJtiLedger]:
    """The step-up replay ledger, or None (then every step-up call is refused)."""
    name = os.environ.get("TOKEN_JTI_TABLE", "").strip()
    if not name:
        return None
    if _LEDGER_CACHE.get("name") != name:
        import boto3

        table = boto3.resource("dynamodb").Table(name)
        _LEDGER_CACHE["name"] = name
        _LEDGER_CACHE["ledger"] = DynamoJtiLedger(table, audience=TOKEN_AUDIENCE)
    return _LEDGER_CACHE["ledger"]


def _bearer_token(headers: Dict[str, str]) -> str:
    raw = headers.get("authorization", "")
    if not raw:
        return ""
    match = _BEARER_RE.fullmatch(raw.strip())
    if match is None:
        return ""
    return match.group(1).strip()


# ---------------------------------------------------------------------------
# HTTP / JSON-RPC helpers
# ---------------------------------------------------------------------------

def _normalize_headers(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    headers: Dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, list):
            value = value[0] if value else ""
        if value is None:
            continue
        headers[str(key).lower()] = str(value)
    return headers


def _raw_body(event: Dict[str, Any]) -> str:
    body = event.get("body")
    if body is None:
        return ""
    if isinstance(body, (bytes, bytearray)):
        body = bytes(body).decode("utf-8")
    if not isinstance(body, str):
        raise ParseError()
    if event.get("isBase64Encoded") and body:
        try:
            body = base64.b64decode(body).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ParseError() from exc
    return body


def _parse_message(event: Dict[str, Any]) -> Any:
    body = event.get("body")
    if isinstance(body, (dict, list)):
        return body
    raw = _raw_body(event)
    if not raw.strip():
        raise ParseError()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError() from exc


def _is_valid_id(value: Any) -> bool:
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))


def _echo_id(message: Any) -> Any:
    if not isinstance(message, dict) or "id" not in message:
        return None
    ident = message["id"]
    return ident if _is_valid_id(ident) else None


def _peek_id(event: Dict[str, Any]) -> Any:
    try:
        return _echo_id(_parse_message(event))
    except ParseError:
        return None


def _response(
    status: int,
    payload: Optional[Dict[str, Any]],
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    body = "" if payload is None else json.dumps(payload)
    return {"statusCode": status, "headers": headers, "body": body}


def _rpc_error(
    status: int,
    code: int,
    message: str,
    req_id: Any,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    return _response(
        status,
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        extra_headers,
    )


def _result(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return _response(200, {"jsonrpc": "2.0", "id": req_id, "result": result})


def _accepted() -> Dict[str, Any]:
    return _response(202, None)


def _forbidden(req_id: Any, exc: AuthorizationError, policy: ToolPolicy) -> Dict[str, Any]:
    realm = f'Bearer realm="{TOKEN_AUDIENCE}"'
    if isinstance(exc, StepUpRequiredError):
        challenge = f'{realm}, error="insufficient_user_authentication", max_age="60"'
    else:
        challenge = f'{realm}, error="insufficient_scope", scope="{policy.scope}"'
    return _rpc_error(
        403,
        FORBIDDEN_CODE,
        f"Forbidden: {exc.reason}",
        req_id,
        extra_headers={"WWW-Authenticate": challenge},
    )


def _unauthorized(req_id: Any) -> Dict[str, Any]:
    return _rpc_error(
        401,
        UNAUTHORIZED_CODE,
        "Unauthorized",
        req_id,
        extra_headers={"WWW-Authenticate": 'Bearer realm="deviceweave"'},
    )


def _protocol_header_error(headers: Dict[str, str], req_id: Any) -> Optional[Dict[str, Any]]:
    version = headers.get("mcp-protocol-version")
    if version is None or version == "":
        return None
    if version not in SUPPORTED_HEADER_VERSIONS:
        return _rpc_error(
            400,
            -32600,
            f"Unsupported MCP-Protocol-Version: {version}",
            req_id,
        )
    return None


# ---------------------------------------------------------------------------
# Service calls — in process, never HTTP back to the API
# ---------------------------------------------------------------------------

def _app():
    import app

    return app


def _json_event(
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    query: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "body": None if body is None else json.dumps(body),
        "queryStringParameters": query,
    }


def _require_str(args: Dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidParams(f"Missing required string argument '{key}'")
    return value.strip()


def _optional_str(args: Dict[str, Any], key: str) -> Optional[str]:
    if key not in args or args[key] is None:
        return None
    value = args[key]
    if not isinstance(value, str):
        raise InvalidParams(f"Argument '{key}' must be a string")
    return value.strip()


def _optional_capabilities(args: Dict[str, Any]) -> Optional[List[str]]:
    if "capabilities" not in args or args["capabilities"] is None:
        return None
    caps = args["capabilities"]
    if (
        not isinstance(caps, list)
        or not caps
        or not all(isinstance(item, str) and item.strip() for item in caps)
    ):
        raise InvalidParams("capabilities must be an array of non-empty strings")
    return [item.strip() for item in caps]


def _health(_args: Dict[str, Any]) -> Dict[str, Any]:
    return _app()._route_health()


def _list_providers(_args: Dict[str, Any]) -> Dict[str, Any]:
    return _app()._route_providers()


def _list_devices(_args: Dict[str, Any]) -> Dict[str, Any]:
    return _app()._route_devices()


def _get_device(args: Dict[str, Any]) -> Dict[str, Any]:
    return _app()._route_get_device(_require_str(args, "device_id"))


def _create_device(args: Dict[str, Any]) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "device_id": _require_str(args, "device_id"),
        "name": _require_str(args, "name"),
    }
    for key in ("device_type", "ip", "model"):
        value = _optional_str(args, key)
        if value:
            body[key] = value
    caps = _optional_capabilities(args)
    if caps is not None:
        body["capabilities"] = caps
    app = _app()
    try:
        return app._route_create_device(_json_event("POST", "/devices", body))
    except app.DeviceRegistryError as exc:
        return app._error(503, str(exc))


def _update_device(args: Dict[str, Any]) -> Dict[str, Any]:
    device_id = _require_str(args, "device_id")
    body: Dict[str, Any] = {}
    for key in ("name", "ip", "device_type", "model"):
        value = _optional_str(args, key)
        if value is not None:
            body[key] = value
    caps = _optional_capabilities(args)
    if caps is not None:
        body["capabilities"] = caps
    return _app()._route_update_device(device_id, _json_event("PUT", f"/devices/{device_id}", body))


def _delete_device(args: Dict[str, Any]) -> Dict[str, Any]:
    return _app()._route_delete_device(_require_str(args, "device_id"))


def _execute(args: Dict[str, Any]) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    for key in ("command", "session_id", "source", "description", "camera_id"):
        if key not in args or args[key] is None:
            continue
        if not isinstance(args[key], str):
            raise InvalidParams(f"Argument '{key}' must be a string")
        body[key] = args[key]
    if "observation" in args and args["observation"] is not None:
        if not isinstance(args["observation"], dict):
            raise InvalidParams("Argument 'observation' must be an object")
        body["observation"] = args["observation"]
    if "confidence" in args and args["confidence"] is not None:
        confidence = args["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise InvalidParams("Argument 'confidence' must be a number")
        body["confidence"] = confidence
    return _app()._route_execute(_json_event("POST", "/execute", body))


def _get_presence(_args: Dict[str, Any]) -> Dict[str, Any]:
    return _app()._route_get_presence()


def _calibrate_room(args: Dict[str, Any]) -> Dict[str, Any]:
    import room_calibration_client

    payload: Dict[str, Any] = {
        "person": _require_str(args, "person"),
        "room": _require_str(args, "room"),
    }
    if "samples" in args and args["samples"] is not None:
        samples = args["samples"]
        if isinstance(samples, bool) or not isinstance(samples, int):
            raise InvalidParams("samples must be an integer")
        payload["samples"] = samples
    return room_calibration_client.invoke("start", payload)


def _list_room_fingerprints(args: Dict[str, Any]) -> Dict[str, Any]:
    import room_calibration_client

    payload: Dict[str, Any] = {}
    person = _optional_str(args, "person")
    if person:
        payload["person"] = person
    return room_calibration_client.invoke("list", payload)


def _delete_room_fingerprint(args: Dict[str, Any]) -> Dict[str, Any]:
    import room_calibration_client

    payload: Dict[str, Any] = {"room": _require_str(args, "room")}
    person = _optional_str(args, "person")
    if person:
        payload["person"] = person
    return room_calibration_client.invoke("delete", payload)


def _record_room_checkin(args: Dict[str, Any]) -> Dict[str, Any]:
    import room_calibration_client

    payload: Dict[str, Any] = {
        "person": _require_str(args, "person"),
        "room": _require_str(args, "room"),
    }
    asked_at = _optional_str(args, "asked_at")
    source = _optional_str(args, "source")
    if asked_at:
        payload["asked_at"] = asked_at
    if source:
        payload["source"] = source
    return room_calibration_client.invoke("record_checkin", payload)


def _capture_room_reading(args: Dict[str, Any]) -> Dict[str, Any]:
    import room_calibration_client

    payload: Dict[str, Any] = {"person": _require_str(args, "person")}
    asked_at = _optional_str(args, "asked_at")
    source = _optional_str(args, "source")
    if asked_at:
        payload["asked_at"] = asked_at
    if source:
        payload["source"] = source
    return room_calibration_client.invoke("capture_reading", payload)


def _label_room_checkin(args: Dict[str, Any]) -> Dict[str, Any]:
    import room_calibration_client

    return room_calibration_client.invoke("label_checkin", {
        "checkin_id": _require_str(args, "checkin_id"),
        "room": _require_str(args, "room"),
    })


def _list_room_checkins(args: Dict[str, Any]) -> Dict[str, Any]:
    import room_calibration_client

    payload: Dict[str, Any] = {}
    person = _optional_str(args, "person")
    since = _optional_str(args, "since")
    if person:
        payload["person"] = person
    if since:
        payload["since"] = since
    if "limit" in args and args["limit"] is not None:
        limit = args["limit"]
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise InvalidParams("limit must be an integer")
        payload["limit"] = limit
    return room_calibration_client.invoke("list_checkins", payload)


def _delete_room_checkin(args: Dict[str, Any]) -> Dict[str, Any]:
    import room_calibration_client

    return room_calibration_client.invoke("delete_checkin", {
        "checkin_id": _require_str(args, "checkin_id"),
    })


def _set_room_schedule_rule(args: Dict[str, Any]) -> Dict[str, Any]:
    import room_calibration_client

    payload: Dict[str, Any] = {
        "person": _require_str(args, "person"),
        "room": _require_str(args, "room"),
        "start": _require_str(args, "start"),
        "end": _require_str(args, "end"),
    }
    timezone_name = _optional_str(args, "timezone")
    if timezone_name:
        payload["timezone"] = timezone_name
    return room_calibration_client.invoke("set_schedule", payload)


def _list_room_schedule_rules(args: Dict[str, Any]) -> Dict[str, Any]:
    import room_calibration_client

    payload: Dict[str, Any] = {}
    person = _optional_str(args, "person")
    if person:
        payload["person"] = person
    return room_calibration_client.invoke("list_schedule", payload)


def _delete_room_schedule_rule(args: Dict[str, Any]) -> Dict[str, Any]:
    import room_calibration_client

    return room_calibration_client.invoke("delete_schedule", {
        "rule_id": _require_str(args, "rule_id"),
    })


def _set_presence(args: Dict[str, Any]) -> Dict[str, Any]:
    if "is_home" not in args or not isinstance(args["is_home"], bool):
        raise InvalidParams("Missing required boolean argument 'is_home'")
    return _app()._route_presence(_json_event("POST", "/presence", {"is_home": args["is_home"]}))


def _list_scenes(_args: Dict[str, Any]) -> Dict[str, Any]:
    return _app()._route_scenes()


def _delete_scene(args: Dict[str, Any]) -> Dict[str, Any]:
    return _app()._route_delete_scene(_require_str(args, "scene_id"))


def _policy(method: str, path: str, body: Optional[Dict[str, Any]] = None,
            query: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    import policy_authoring.handler as policy_handler

    return policy_handler.handler(_json_event(method, path, body, query), None)


def _list_policies(args: Dict[str, Any]) -> Dict[str, Any]:
    query: Dict[str, str] = {}
    device_type = _optional_str(args, "device_type")
    if device_type:
        query["device_type"] = device_type
    if "limit" in args and args["limit"] is not None:
        limit = args["limit"]
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise InvalidParams("limit must be an integer from 1 to 100")
        query["limit"] = str(limit)
    return _policy("GET", "/policies", query=query or None)


def _get_policy(args: Dict[str, Any]) -> Dict[str, Any]:
    rule_id = _require_str(args, "rule_id")
    query: Dict[str, str] = {}
    if "version" in args and args["version"] is not None:
        version = args["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise InvalidParams("version must be an integer >= 1")
        query["version"] = str(version)
    return _policy("GET", f"/policies/{rule_id}", query=query or None)


def _author_policy(args: Dict[str, Any]) -> Dict[str, Any]:
    return _policy("POST", "/policies/author", {"rule": _require_str(args, "rule")})


def _delete_policy(args: Dict[str, Any]) -> Dict[str, Any]:
    return _policy("DELETE", f"/policies/{_require_str(args, 'rule_id')}")


def _get_learnings(_args: Dict[str, Any]) -> Dict[str, Any]:
    return _app()._route_list_learnings()


def _learn_phrase(args: Dict[str, Any]) -> Dict[str, Any]:
    return _app()._route_learn(_json_event("POST", "/learn", {
        "device_id": _require_str(args, "device_id"),
        "phrase": _require_str(args, "phrase"),
    }))


def _delete_learning(args: Dict[str, Any]) -> Dict[str, Any]:
    return _app()._route_delete_learning(_json_event("DELETE", "/learnings", {
        "device_id": _require_str(args, "device_id"),
        "phrase": _require_str(args, "phrase"),
    }))


def _ingest(args: Dict[str, Any]) -> Dict[str, Any]:
    import ingestion_handler

    body: Dict[str, Any] = {}
    for key in ("provider", "mode", "refresh_token", "two_fa_code"):
        value = _optional_str(args, key)
        if value:
            body[key] = value
    mode = body.get("mode")
    if mode is not None and mode.lower() not in ("full", "delta"):
        raise InvalidParams("mode must be 'full' or 'delta'")
    return ingestion_handler.handler(_json_event("POST", "/ingest", body), None)


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _str_prop(description: str, enum: Optional[List[str]] = None) -> Dict[str, Any]:
    prop: Dict[str, Any] = {"type": "string", "description": description}
    if enum is not None:
        prop["enum"] = enum
    return prop


def _tool(
    name: str,
    title: str,
    description: str,
    properties: Dict[str, Any],
    call: Callable[[Dict[str, Any]], Dict[str, Any]],
    annotations: Dict[str, bool],
    required: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": _schema(properties, required),
        "annotations": dict(annotations),
        "call": call,
    }


_NO_ARGS: Dict[str, Any] = {}

TOOLS: List[Dict[str, Any]] = [
    _tool(
        "list_devices",
        "List devices",
        "List active devices in the registry. Returns the public view of each device and a count.",
        _NO_ARGS,
        _list_devices,
        _READ_ONLY,
    ),
    _tool(
        "get_device",
        "Get device",
        "Get one device by its registry id.",
        {"device_id": _str_prop("Registry device id.")},
        _get_device,
        _READ_ONLY,
        ["device_id"],
    ),
    _tool(
        "create_device",
        "Create device",
        "Register a manual device in the registry. Required: device_id and name. "
        "Provider-discovered devices should use ingest instead. "
        "The HTTP POST /devices route still returns 405; this tool calls the registry write directly.",
        {
            "device_id": _str_prop("New registry id. Must not already exist."),
            "name": _str_prop("Display name."),
            "device_type": _str_prop("Device type. Defaults to SmartPlug when omitted."),
            "capabilities": {
                "type": "array",
                "description": "Actions the device accepts. Defaults to turn_on, turn_off, and get_status.",
                "items": {"type": "string"},
            },
            "ip": _str_prop("Optional address of the device."),
            "model": _str_prop("Optional model name. Defaults to manual."),
        },
        _create_device,
        _MUTATING,
        ["device_id", "name"],
    ),
    _tool(
        "update_device",
        "Update device",
        "Update mutable registry fields for a device: name, capabilities, ip, device_type, or model. "
        "A name change is pushed to a provider when that provider supports rename.",
        {
            "device_id": _str_prop("Registry device id."),
            "name": _str_prop("New display name."),
            "capabilities": {
                "type": "array",
                "description": "Replacement capability list.",
                "items": {"type": "string"},
            },
            "ip": _str_prop("Replacement address."),
            "device_type": _str_prop("Replacement device type."),
            "model": _str_prop("Replacement model name."),
        },
        _update_device,
        _MUTATING,
        ["device_id"],
    ),
    _tool(
        "delete_device",
        "Delete device",
        "Deactivate a device. Sets its registry status to inactive so it leaves the active catalog.",
        {"device_id": _str_prop("Registry device id to deactivate.")},
        _delete_device,
        _DESTRUCTIVE,
        ["device_id"],
    ),
    _tool(
        "execute",
        "Execute command",
        "Run a natural-language device or scene command. This can actuate physical hardware. "
        "Pass command for a one-shot request. Pass session_id as well for a conversational turn "
        "whose history is stored. Pass source vision with observation or description for a camera intent.",
        {
            "command": _str_prop("Natural-language instruction, for example 'turn on the office light'."),
            "session_id": _str_prop("When set, runs the conversational agent and stores the turn under this id."),
            "source": _str_prop("Intent source. Use vision together with observation or description."),
            "observation": {
                "type": "object",
                "description": "Structured camera observation (device_query, action, confidence, context).",
                "additionalProperties": True,
            },
            "description": _str_prop("Raw vision-model description when source is vision."),
            "confidence": {"type": "number", "description": "Optional confidence supplied with a vision intent."},
            "camera_id": _str_prop("Optional camera id supplied with a vision description."),
        },
        _execute,
        _DESTRUCTIVE,
    ),
    _tool(
        "get_presence",
        "Get presence",
        "Read home occupancy for the policy engine. Includes the eero zone (node), signal, "
        "room, room_confidence, last_seen, and freshness when the eero poller has written them. "
        "room is unknown until calibration classifies the reading. Defaults to home "
        "when nothing has been written.",
        _NO_ARGS,
        _get_presence,
        _READ_ONLY,
    ),
    _tool(
        "calibrate_room",
        "Calibrate room",
        "Stand in one room and record N eero readings (node and coarse signal bucket), about "
        "12 seconds apart. samples defaults to 5 and must be 1-8. Calling this again for the "
        "same person and room replaces that room's samples. Returns as soon as sampling starts; "
        "list_room_fingerprints shows the stored samples after about a minute.",
        {
            "person": _str_prop("Person name, matching the tracked-device mapping."),
            "room": _str_prop("Room label to store. 1-40 letters, digits, spaces, hyphens, or underscores."),
            "samples": {
                "type": "integer",
                "description": "How many eero reads to take. Defaults to 5. Maximum 8.",
                "minimum": 1,
                "maximum": 8,
            },
        },
        _calibrate_room,
        _MUTATING,
        ["person", "room"],
    ),
    _tool(
        "list_room_fingerprints",
        "List room fingerprints",
        "List calibrated rooms and their samples (node, coarse signal bucket, time). "
        "Optional person filter. Does not return device identifiers.",
        {"person": _str_prop("Optional person filter.")},
        _list_room_fingerprints,
        _READ_ONLY,
    ),
    _tool(
        "delete_room_fingerprint",
        "Delete room fingerprint",
        "Delete the samples stored for a room. Optional person limits the delete to that person; "
        "omitting person deletes that room label for every person.",
        {
            "room": _str_prop("Room label to delete."),
            "person": _str_prop("Optional person. Omit to delete this room for every person."),
        },
        _delete_room_fingerprint,
        _DESTRUCTIVE,
        ["room"],
    ),
    _tool(
        "record_room_checkin",
        "Record room check-in",
        "Store a labeled room check-in in DeviceWeave. The server reads the current eero "
        "snapshot for the person's tracked device (node, signal, coarse bucket, freshness, "
        "last_seen, is_home) and stores it with the label. Hour and weekday are asked_at in "
        "America/Chicago, Monday is 0. asked_at defaults to now. The eero reading is always "
        "taken now, so pass asked_at only when it is the question time of this call. "
        "room may be 'Not home' or any other short label. Returns the stored record.",
        {
            "person": _str_prop("Person name, matching the tracked-device mapping."),
            "room": _str_prop("Room label, including 'Not home'. 1-80 characters."),
            "asked_at": _str_prop("Optional ISO-8601 question time. Defaults to now."),
            "source": _str_prop("Optional source label. Defaults to record."),
        },
        _record_room_checkin,
        _MUTATING,
        ["person", "room"],
    ),
    _tool(
        "capture_room_reading",
        "Capture room reading",
        "Snapshot the current eero reading for a person without a room label. Returns "
        "checkin_id. Call label_room_checkin when the answer arrives so the signal matches "
        "ask time. Hour and weekday are asked_at in America/Chicago.",
        {
            "person": _str_prop("Person name, matching the tracked-device mapping."),
            "asked_at": _str_prop("Optional ISO-8601 question time. Defaults to now."),
            "source": _str_prop("Optional source label. Defaults to capture."),
        },
        _capture_room_reading,
        _MUTATING,
        ["person"],
    ),
    _tool(
        "label_room_checkin",
        "Label room check-in",
        "Attach the room answer to a check-in captured earlier. Does not re-read eero. "
        "room may be 'Not home' or any other short label.",
        {
            "checkin_id": _str_prop("Id returned by capture_room_reading or record_room_checkin."),
            "room": _str_prop("Room label, including 'Not home'. 1-80 characters."),
        },
        _label_room_checkin,
        _MUTATING,
        ["checkin_id", "room"],
    ),
    _tool(
        "list_room_checkins",
        "List room check-ins",
        "List labeled and unlabeled check-ins stored in DeviceWeave. Optional person, "
        "since (ISO-8601), and limit (1-200, default 50). Oldest first.",
        {
            "person": _str_prop("Optional person filter."),
            "since": _str_prop("Optional ISO-8601 lower bound on asked_at."),
            "limit": {
                "type": "integer",
                "description": "Maximum rows. Defaults to 50. Maximum 200.",
                "minimum": 1,
                "maximum": 200,
            },
        },
        _list_room_checkins,
        _READ_ONLY,
    ),
    _tool(
        "delete_room_checkin",
        "Delete room check-in",
        "Delete one check-in by id. Deleted is 0 when the id is already gone.",
        {"checkin_id": _str_prop("Check-in id to delete.")},
        _delete_room_checkin,
        _DESTRUCTIVE,
        ["checkin_id"],
    ),
    _tool(
        "set_room_schedule_rule",
        "Set room schedule rule",
        "Store a fixed local-time room. While the person is home inside the window, "
        "the presence poller sets that room with high confidence and room_source "
        "schedule_rule. A window may wrap midnight (start 23:00, end 06:00). "
        "timezone defaults to America/Chicago. Replacing the same start and end updates the room.",
        {
            "person": _str_prop("Person name, matching the tracked-device mapping."),
            "room": _str_prop("Room label. 1-40 letters, digits, spaces, hyphens, or underscores."),
            "start": _str_prop("Window start, HH:MM, 24-hour, inclusive."),
            "end": _str_prop("Window end, HH:MM, 24-hour, exclusive. Earlier than start wraps midnight."),
            "timezone": _str_prop("IANA timezone. Defaults to America/Chicago."),
        },
        _set_room_schedule_rule,
        _MUTATING,
        ["person", "room", "start", "end"],
    ),
    _tool(
        "list_room_schedule_rules",
        "List room schedule rules",
        "List fixed time-window room rules stored in DeviceWeave. Optional person filter.",
        {"person": _str_prop("Optional person filter.")},
        _list_room_schedule_rules,
        _READ_ONLY,
    ),
    _tool(
        "delete_room_schedule_rule",
        "Delete room schedule rule",
        "Delete one schedule rule by id. Deleted is 0 when the id is already gone.",
        {"rule_id": _str_prop("Id returned by set_room_schedule_rule.")},
        _delete_room_schedule_rule,
        _DESTRUCTIVE,
        ["rule_id"],
    ),
    _tool(
        "set_presence",
        "Set presence",
        "Set whether anyone is home. The policy engine reads this on later execute calls.",
        {"is_home": {"type": "boolean", "description": "True when someone is home, false when the home is empty."}},
        _set_presence,
        _MUTATING,
        ["is_home"],
    ),
    _tool(
        "list_scenes",
        "List scenes",
        "List scenes that have not been deleted.",
        _NO_ARGS,
        _list_scenes,
        _READ_ONLY,
    ),
    _tool(
        "delete_scene",
        "Delete scene",
        "Soft-delete a scene so it no longer resolves or appears in list_scenes.",
        {"scene_id": _str_prop("Scene id, for example work_mode.")},
        _delete_scene,
        _DESTRUCTIVE,
        ["scene_id"],
    ),
    _tool(
        "list_policies",
        "List policies",
        "List active automation policies. Optionally filter by device_type.",
        {
            "device_type": _str_prop("Optional filter, for example fan, light, ac, plug, or heater."),
            "limit": {
                "type": "integer",
                "description": "Maximum number of policies to return (1-100). Defaults to 50.",
                "minimum": 1,
                "maximum": 100,
            },
        },
        _list_policies,
        _READ_ONLY,
    ),
    _tool(
        "get_policy",
        "Get policy",
        "Get one policy by rule id. Omit version for the latest stored version.",
        {
            "rule_id": _str_prop("Policy rule id."),
            "version": {
                "type": "integer",
                "description": "Specific version. Omit for the latest.",
                "minimum": 1,
            },
        },
        _get_policy,
        _READ_ONLY,
        ["rule_id"],
    ),
    _tool(
        "author_policy",
        "Author policy",
        "Compile a natural-language automation rule, validate it, and store it when it passes.",
        {"rule": _str_prop("Natural-language rule, for example \"Don't turn on the fan when it's cold\".")},
        _author_policy,
        _MUTATING,
        ["rule"],
    ),
    _tool(
        "delete_policy",
        "Delete policy",
        "Deactivate a policy so the policy engine stops enforcing it.",
        {"rule_id": _str_prop("Policy rule id to deactivate.")},
        _delete_policy,
        _DESTRUCTIVE,
        ["rule_id"],
    ),
    _tool(
        "get_learnings",
        "Get learnings",
        "List phrase-to-device bindings, newest first.",
        _NO_ARGS,
        _get_learnings,
        _READ_ONLY,
    ),
    _tool(
        "learn_phrase",
        "Learn phrase",
        "Bind a phrase to a device so later commands can resolve to it.",
        {
            "device_id": _str_prop("Registry id of an active device."),
            "phrase": _str_prop("Phrase to bind, for example 'desk lamp'."),
        },
        _learn_phrase,
        _MUTATING,
        ["device_id", "phrase"],
    ),
    _tool(
        "delete_learning",
        "Delete learning",
        "Remove one phrase-to-device binding.",
        {
            "device_id": _str_prop("Device id the phrase is bound to."),
            "phrase": _str_prop("Phrase to remove."),
        },
        _delete_learning,
        _DESTRUCTIVE,
        ["device_id", "phrase"],
    ),
    _tool(
        "list_providers",
        "List providers",
        "List registered IoT providers and whether each one is configured.",
        _NO_ARGS,
        _list_providers,
        _READ_ONLY,
    ),
    _tool(
        "ingest",
        "Ingest devices",
        "Discover devices from a provider and sync them into the registry. "
        "mode full upserts every record and marks devices missing from this run offline. "
        "mode delta writes only changed records and does not mark devices offline. "
        "Defaults are provider kasa and mode delta.",
        {
            "provider": _str_prop("Provider id: kasa, switchbot, govee, ring, myq, or wyze. Defaults to kasa."),
            "mode": _str_prop("full reconciles the registry; delta writes changes only.", ["full", "delta"]),
            "refresh_token": _str_prop("Optional Ring refresh token to store before discovery."),
            "two_fa_code": _str_prop("Optional Ring verification code when a previous ingest asked for one."),
        },
        _ingest,
        _MUTATING,
    ),
    _tool(
        "health",
        "Health",
        "Liveness probe: active device count, scene count, and whether the registry, learning store, and behavior graph are available.",
        _NO_ARGS,
        _health,
        _READ_ONLY,
    ),
]

_TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}
if len(_TOOLS_BY_NAME) != len(TOOLS):
    raise RuntimeError("duplicate MCP tool name")

# execute keeps destructiveHint for clients (it can actuate), but it is what the
# execute scope is for: turning on a lamp must not need a 60-second token. The
# steps it resolves to that open something are gated on their own, in
# access_control. ingest is annotated as mutating (it upserts), but a full
# ingest marks every device it did not see offline and can store a provider
# refresh token: it is step-up territory with the deletes.
_POLICY_OVERRIDES: Dict[str, ToolPolicy] = {"execute": EXECUTE, "ingest": DESTRUCTIVE}


def _policy_for(tool: Dict[str, Any]) -> ToolPolicy:
    if tool["name"] in _POLICY_OVERRIDES:
        return _POLICY_OVERRIDES[tool["name"]]
    annotations = tool["annotations"]
    if annotations.get("destructiveHint"):
        return DESTRUCTIVE
    if annotations.get("readOnlyHint"):
        return READ
    return EXECUTE


TOOL_POLICIES: Dict[str, ToolPolicy] = {tool["name"]: _policy_for(tool) for tool in TOOLS}
# Looked up at call time so tests can replace _jti_ledger.
_AUTHORIZER = ToolAuthorizer(TOOL_POLICIES, ledger=lambda: _jti_ledger())


def _public_tool(tool: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": tool["name"],
        "title": tool["title"],
        "description": tool["description"],
        "inputSchema": tool["inputSchema"],
        "annotations": tool["annotations"],
    }


def _initialize_result(_params: Any) -> Dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {
            "name": "deviceweave",
            "title": "DeviceWeave",
            "version": SERVER_VERSION,
        },
        "instructions": INSTRUCTIONS,
    }


def _tool_result(response: Any) -> Dict[str, Any]:
    if not isinstance(response, dict) or "statusCode" not in response:
        return {
            "content": [{"type": "text", "text": json.dumps(response, default=str)}],
            "isError": True,
        }
    status = int(response["statusCode"])
    body = response.get("body")
    if body is None:
        text = ""
    elif isinstance(body, str):
        text = body
    else:
        text = json.dumps(body, default=str)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": status >= 400,
    }


def _tools_list(req_id: Any, params: Any) -> Dict[str, Any]:
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _rpc_error(200, -32602, "Invalid params", req_id)
    if params.get("cursor") not in (None, ""):
        return _rpc_error(200, -32602, "Invalid params: pagination cursor is not supported", req_id)
    auth = access_control.current()
    visible = set(_AUTHORIZER.visible(auth.principal, _TOOLS_BY_NAME)) if auth else set()
    return _result(
        req_id, {"tools": [_public_tool(tool) for tool in TOOLS if tool["name"] in visible]}
    )


def _tools_call(req_id: Any, params: Any) -> Dict[str, Any]:
    if not isinstance(params, dict):
        return _rpc_error(200, -32602, "Invalid params", req_id)
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return _rpc_error(200, -32602, "Invalid params: missing tool name", req_id)
    arguments = params.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _rpc_error(200, -32602, "Invalid params: arguments must be an object", req_id)
    tool = _TOOLS_BY_NAME.get(name)
    if tool is None:
        return _rpc_error(200, -32602, f"Unknown tool: {name}", req_id)
    auth = access_control.current()
    principal = auth.principal if auth else None
    if auth is None:
        # handler() always binds one; reaching here without it is a bug, and
        # a bug in authorization must refuse.
        _audit_call(name, None, "denied", "no_principal")
        return _rpc_error(403, FORBIDDEN_CODE, "Forbidden", req_id)
    try:
        auth.authorize_tool(name)
    except AuthorizationError as exc:
        _audit_call(name, principal, "denied", exc.reason)
        return _forbidden(req_id, exc, _AUTHORIZER.policy(name))
    try:
        service_response = tool["call"](arguments)
    except InvalidParams as exc:
        _audit_call(name, principal, "invalid_params", None)
        return _rpc_error(200, -32602, str(exc), req_id)
    except Exception:
        logger.exception("Tool %s failed", name)
        service_response = {
            "statusCode": 500,
            "body": json.dumps({"error": "Tool execution failed"}),
        }
    result = _tool_result(service_response)
    _audit_call(name, principal, "error" if result["isError"] else "ok", None)
    return _result(req_id, result)


def _audit_call(
    tool: str, principal: Optional[Principal], outcome: str, reason: Optional[str]
) -> None:
    """One line per tools/call: which token (sub, jti) did what. Never the token."""
    fields = principal.log_fields() if principal else {"method": None, "sub": None, "jti": None}
    logger.info(
        json.dumps(
            {"event": "mcp_tool_call", "tool": tool, "outcome": outcome, "reason": reason, **fields},
            sort_keys=True,
        )
    )


def _dispatch(message: Dict[str, Any]) -> Dict[str, Any]:
    if message.get("jsonrpc") != "2.0":
        return _rpc_error(400, -32600, "Invalid Request", _echo_id(message))
    if "id" in message and not _is_valid_id(message["id"]):
        return _rpc_error(400, -32600, "Invalid Request", None)

    rpc_method = message.get("method")
    has_id = "id" in message
    req_id = message["id"] if has_id else None

    if not isinstance(rpc_method, str) or not rpc_method:
        if not has_id or "result" in message or "error" in message:
            return _accepted()
        return _rpc_error(400, -32600, "Invalid Request", _echo_id(message))

    if not has_id:
        logger.debug("MCP notification %s", rpc_method)
        return _accepted()

    logger.info("MCP method %s", rpc_method)
    if rpc_method == "initialize":
        return _result(req_id, _initialize_result(message.get("params")))
    if rpc_method == "ping":
        return _result(req_id, {})
    if rpc_method == "tools/list":
        return _tools_list(req_id, message.get("params"))
    if rpc_method == "tools/call":
        return _tools_call(req_id, message.get("params"))
    return _rpc_error(200, -32601, f"Method not found: {rpc_method}", req_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    method = str(
        event.get("requestContext", {}).get("http", {}).get("method", "POST")
    ).upper()
    headers = _normalize_headers(event.get("headers"))

    provided = _bearer_token(headers)
    try:
        decision = _AUTH.check(provided)
    except McpAuthUnavailable:
        # Only reached on the legacy path: the token was not an acceptable
        # JWT and the static secret could not be read.
        logger.exception("MCP authentication is unavailable")
        req_id = _peek_id(event) if method == "POST" else None
        return _rpc_error(500, -32603, "MCP authentication is unavailable", req_id)

    if not decision.ok:
        logger.warning("MCP unauthorized")
        req_id = _peek_id(event) if method == "POST" else None
        return _unauthorized(req_id)

    if method == "GET":
        header_error = _protocol_header_error(headers, None)
        if header_error is not None:
            return header_error
        return _rpc_error(
            405,
            -32601,
            "Method Not Allowed. This server does not offer an SSE stream. Send JSON-RPC requests with POST.",
            None,
            extra_headers={"Allow": "POST"},
        )
    if method != "POST":
        return _rpc_error(
            405,
            -32601,
            "Method Not Allowed",
            None,
            extra_headers={"Allow": "POST"},
        )

    try:
        message = _parse_message(event)
    except ParseError:
        return _rpc_error(400, -32700, "Parse error", None)

    if isinstance(message, list):
        return _rpc_error(400, -32600, "JSON-RPC batches are not supported", None)
    if not isinstance(message, dict):
        return _rpc_error(400, -32600, "Invalid Request", None)

    header_error = _protocol_header_error(headers, _echo_id(message))
    if header_error is not None:
        return header_error
    principal = decision.principal or Principal("unknown", None, frozenset())
    with access_control.bind(access_control.RequestAuth(principal, _AUTHORIZER)):
        return _dispatch(message)
