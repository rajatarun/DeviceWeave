"""MCP protocol eras: the stateless 2026-07-28 revision and the handshake ones.

Revision 2026-07-28 removed the ``initialize`` handshake. Every request now
carries its own version and the client's capabilities in ``params._meta``, and
on Streamable HTTP mirrors the version, method and name into headers so a
gateway can route on them. A client older than that revision still opens with
``initialize`` and sends neither.

This server is **dual-era**, as the revision allows: a request whose ``_meta``
names a protocol version is *modern* and served statelessly; anything else is
*legacy* and served as before. Which era a request belongs to is decided by
the request alone, never by an earlier one, so there is still no session.

This module holds only the rules the revision adds, so the handler that
dispatches tools stays the one place a tool is authorized and called:

- ``modern_version`` decides the era.
- ``validate_modern`` applies the transport's header checks and the
  per-request ``_meta`` rules, raising ``Rejection`` with the HTTP status and
  JSON-RPC error the revision specifies.
- ``complete``, ``cacheable`` and ``discover_result`` shape modern results.
- ``negotiate_legacy`` answers ``initialize`` for handshake-era clients.

The same file is in ContextWeave, DeviceWeave and ModelWeave, so the three
hand-written servers cannot drift apart. It is standard library only.
"""
from __future__ import annotations

import base64
import binascii
from typing import Any

MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED_VERSIONS = MODERN_VERSIONS + LEGACY_VERSIONS
LATEST_VERSION = MODERN_VERSIONS[0]
LATEST_LEGACY_VERSION = LEGACY_VERSIONS[0]

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# The sub-range -32020..-32099 is reserved for codes the specification defines.
INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601
HEADER_MISMATCH = -32020
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022

# Methods whose Mcp-Name header mirrors a body field, and which field.
_NAMED = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}

# Removed by 2026-07-28; a modern request for one is an unknown method.
REMOVED_IN_MODERN = frozenset({"initialize", "ping", "logging/setLevel"})

_SENTINEL_PREFIX = "=?base64?"
_SENTINEL_SUFFIX = "?="


class Rejection(Exception):
    """A modern request the revision says to refuse, with how to refuse it."""

    def __init__(self, status: int, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.data = data

    def error(self) -> dict:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


def _meta(message: dict) -> dict:
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    return meta if isinstance(meta, dict) else {}


def modern_version(message: dict) -> str | None:
    """The protocol version a modern request declares, or None for a legacy one.

    ``initialize`` is legacy whatever it carries: it is the one method a
    modern client never sends, and a dual-era server answers it as before.
    """
    if message.get("method") == "initialize":
        return None
    meta = _meta(message)
    if META_PROTOCOL_VERSION not in meta:
        return None
    version = meta[META_PROTOCOL_VERSION]
    return version if isinstance(version, str) else ""


def header(headers: dict | None, name: str) -> str | None:
    """A request header by case-insensitive name; None when absent."""
    for key, value in (headers or {}).items():
        if isinstance(key, str) and key.lower() == name.lower():
            return value if isinstance(value, str) else None
    return None


def decode_header_value(value: str) -> str:
    """Undo the ``=?base64?...?=`` sentinel encoding; plain values pass through."""
    if value.startswith(_SENTINEL_PREFIX) and value.endswith(_SENTINEL_SUFFIX):
        encoded = value[len(_SENTINEL_PREFIX):-len(_SENTINEL_SUFFIX)]
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise Rejection(400, HEADER_MISMATCH, "Header value is not valid base64") from exc
    return value


def validate_modern(message: dict, headers: dict | None) -> str:
    """Apply the 2026-07-28 request rules; return the request's version.

    Header checks come first: a header that disagrees with the body is refused
    before anything is read from the body, so a gateway routing on the header
    and this server acting on the body can never see two different requests.
    """
    version = modern_version(message)
    if version is None:
        raise ValueError("not a modern request")
    method = message.get("method")
    params = message.get("params")
    params = params if isinstance(params, dict) else {}

    sent_version = header(headers, "MCP-Protocol-Version")
    if sent_version is None:
        raise Rejection(400, HEADER_MISMATCH, "Missing required MCP-Protocol-Version header")
    if sent_version.strip() != version:
        raise Rejection(400, HEADER_MISMATCH, "MCP-Protocol-Version header does not match _meta")
    sent_method = header(headers, "Mcp-Method")
    if sent_method is None:
        raise Rejection(400, HEADER_MISMATCH, "Missing required Mcp-Method header")
    if sent_method.strip() != method:
        raise Rejection(400, HEADER_MISMATCH, "Mcp-Method header does not match the request method")
    field = _NAMED.get(str(method))
    if field is not None:
        sent_name = header(headers, "Mcp-Name")
        if sent_name is None:
            raise Rejection(400, HEADER_MISMATCH, "Missing required Mcp-Name header")
        if decode_header_value(sent_name.strip()) != params.get(field):
            raise Rejection(400, HEADER_MISMATCH, f"Mcp-Name header does not match the request's {field}")

    if version not in MODERN_VERSIONS:
        raise Rejection(
            400,
            UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported protocol version",
            {"supported": list(SUPPORTED_VERSIONS), "requested": version},
        )
    if not isinstance(_meta(message).get(META_CLIENT_CAPABILITIES), dict):
        raise Rejection(400, INVALID_PARAMS, f"_meta is missing {META_CLIENT_CAPABILITIES}")
    return version


def check_legacy_header(headers: dict | None, method: Any = None) -> None:
    """Refuse a handshake-era request whose version header this server cannot serve.

    A modern version in the header with no ``_meta`` is a modern request
    missing its required fields, not a legacy one. ``initialize`` is exempt:
    the version is what it negotiates, so no header can be wrong for it yet.
    """
    if method == "initialize":
        return
    sent = header(headers, "MCP-Protocol-Version")
    if sent is None or not sent.strip():
        return  # 2025-03-26 clients predate the header
    sent = sent.strip()
    if sent in MODERN_VERSIONS:
        raise Rejection(400, INVALID_PARAMS, f"_meta is missing {META_PROTOCOL_VERSION}")
    if sent not in LEGACY_VERSIONS:
        raise Rejection(
            400,
            UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported protocol version",
            {"supported": list(SUPPORTED_VERSIONS), "requested": sent},
        )


def server_info(name: str, version: str) -> dict:
    return {"name": name, "version": version}


def complete(result: dict, info: dict) -> dict:
    """A modern result: ``resultType`` plus the server's identity in ``_meta``."""
    shaped = dict(result)
    shaped["resultType"] = "complete"
    meta = dict(shaped.get("_meta") or {})
    meta[META_SERVER_INFO] = info
    shaped["_meta"] = meta
    return shaped


def cacheable(result: dict, ttl_ms: int, scope: str = "private") -> dict:
    """Add the caching hints list results carry.

    ``private`` whenever the answer depends on the caller: a tool list filtered
    by the token's scopes must not be served to another caller by a shared cache.
    """
    if scope not in ("public", "private"):
        raise ValueError(scope)
    return {**result, "ttlMs": max(0, int(ttl_ms)), "cacheScope": scope}


def discover_result(capabilities: dict, instructions: str | None, ttl_ms: int) -> dict:
    result: dict[str, Any] = {
        "supportedVersions": list(SUPPORTED_VERSIONS),
        "capabilities": capabilities,
    }
    if instructions:
        result["instructions"] = instructions
    # Private: the endpoint is behind a bearer token, and a shared cache that
    # held this would answer callers the server never authenticated.
    return cacheable(result, ttl_ms, "private")


def negotiate_legacy(requested: Any) -> str:
    """The version to answer ``initialize`` with: the client's, if it is a handshake one.

    Otherwise the newest handshake version, and the client decides whether it
    can use it. Never a modern version: ``initialize`` does not exist there.
    """
    return requested if requested in LEGACY_VERSIONS else LATEST_LEGACY_VERSION
