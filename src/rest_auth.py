"""Authentication and per-route scopes for DeviceWeave's REST API.

The REST routes had no authorizer and no check of their own (ADR 40 named it
"the bigger hole"). Anyone holding the API URL could ``POST /execute`` to
switch any device, ``GET /presence`` to learn who is home, ``DELETE
/devices/{id}``, author or delete automation policies, and trigger an ingest.
The URL is not a secret: it is in the stack outputs, the OpenAPI document,
browser history and every client configuration.

Every REST route except ``GET /health`` now needs the same credential as
``/mcp``: a CipherWeave access token for ``deviceweave`` (``Bearer``, or
``DPoP`` with its proof), or the legacy static token while
``ACCEPT_LEGACY_TOKEN`` is true. ``cipherweave_verify.BearerAuth`` decides, so
the REST and MCP doors cannot disagree. Each route then needs a scope:

| Scope | Routes |
|---|---|
| ``read`` | every ``GET`` (devices, scenes, providers, learnings, presence, policies) |
| ``execute`` | ``POST /execute``, ``POST /learn``, ``POST /presence``, ``PUT /devices/{id}``, ``DELETE /learnings``, ``POST /policies/author`` |
| ``destructive`` | ``DELETE /devices/{id}``, ``DELETE /scenes/{id}``, ``DELETE /policies/{id}``, ``POST /ingest`` |

``destructive`` here is a scope without step-up: the REST functions have no
jti ledger, and none of these routes is irreversible in the way an unlock is.
Unlock keeps its own gate in ``execution_planner``, which the authenticated
principal is now bound into for ``/execute`` -- so the REST route unlocks
exactly when the MCP tool would. A delegated token (RFC 8693) cannot use the
``destructive`` routes.

``REST_AUTH_MODE=audit`` logs what would have been refused and lets it
through, for the window while existing REST clients are given a token. It
never applies to ``destructive`` routes or the delegation rule. A non-HTTP
invocation (EventBridge, a direct Lambda invoke) is IAM-authorized already
and is not checked here.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

from cipherweave_verify import (
    EXECUTE,
    READ,
    AuthorizationError,
    BearerAuth,
    Principal,
    ToolAuthorizer,
    ToolPolicy,
    www_authenticate,
)

logger = logging.getLogger(__name__)

TOKEN_AUDIENCE = "deviceweave"
ENV_MODE = "REST_AUTH_MODE"
DESTRUCTIVE_ROUTE = ToolPolicy("destructive", step_up=False, delegable=False)

_auth: Optional[BearerAuth] = None


def _bearer_auth() -> BearerAuth:
    global _auth
    if _auth is None:
        import mcp_handler

        _auth = BearerAuth(TOKEN_AUDIENCE, legacy_token=lambda: mcp_handler._load_expected_token())
    return _auth


def reset() -> None:
    global _auth
    _auth = None


def mode() -> str:
    return "audit" if (os.environ.get(ENV_MODE) or "").strip().lower() == "audit" else "enforce"


def _method_path(event: Dict[str, Any]) -> Tuple[str, str]:
    http = (event.get("requestContext") or {}).get("http") or {}
    return str(http.get("method") or "").upper(), str(http.get("path") or event.get("rawPath") or "")


def is_http(event: Any) -> bool:
    return isinstance(event, dict) and bool((event.get("requestContext") or {}).get("http"))


def route_policy(method: str, path: str) -> Optional[ToolPolicy]:
    """The scope a route needs; None for the one public route (``GET /health``)."""
    trimmed = path.rstrip("/")
    if method == "GET" and trimmed.endswith("/health"):
        return None
    if method in ("GET", "HEAD", "OPTIONS"):
        return READ
    if method == "DELETE" and ("/devices/" in trimmed or "/scenes/" in trimmed or "/policies/" in trimmed):
        return DESTRUCTIVE_ROUTE
    if method == "POST" and trimmed.endswith("/ingest"):
        return DESTRUCTIVE_ROUTE
    # Anything else that changes state, including routes added later and not
    # listed here, needs execute -- never less.
    return EXECUTE


def _json(status: int, body: Dict[str, Any], challenge: Optional[str] = None) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
    if challenge:
        headers["WWW-Authenticate"] = challenge
    return {"statusCode": status, "headers": headers, "body": json.dumps(body)}


def check(event: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Principal]]:
    """(error response, None) or (None, principal-or-None-for-public)."""
    if not is_http(event):
        return None, None
    method, path = _method_path(event)
    policy = route_policy(method, path)
    if policy is None:
        return None, None

    try:
        decision = _bearer_auth().check_event(event)
    except Exception as exc:  # the legacy loader's "secret unavailable"
        logger.error("rest auth unavailable type=%s", type(exc).__name__)
        return _json(503, {"error": "Authentication is unavailable"}), None
    audit = mode() == "audit" and policy.scope != "destructive"
    if not decision.ok:
        if audit:
            logger.warning(
                "rest auth audit-only: would refuse %s %s reason=%s", method, path, decision.reason
            )
            return None, None
        return (
            _json(
                401,
                {"error": "Unauthorized"},
                www_authenticate(realm=TOKEN_AUDIENCE, error="invalid_token"),
            ),
            None,
        )

    principal = decision.principal or Principal("unknown", None, frozenset())
    authorizer = ToolAuthorizer({"route": policy})
    try:
        if audit and policy.scope not in principal.scopes:
            logger.warning(
                "rest auth audit-only: would refuse %s %s reason=insufficient_scope", method, path
            )
        else:
            authorizer.authorize(principal, "route")
    except AuthorizationError as exc:
        return (
            _json(
                403,
                {"error": f"Forbidden: {exc.reason}"},
                www_authenticate(realm=TOKEN_AUDIENCE, error="insufficient_scope", scope=policy.scope),
            ),
            None,
        )
    logger.info(
        "rest_call %s",
        json.dumps({"method": method, "path": path, **principal.log_fields()}, sort_keys=True),
    )
    return None, principal
