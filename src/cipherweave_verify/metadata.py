"""Discovery documents a Weave serves so a client can find out how to call it.

Two standards, one per side of the exchange:

* **RFC 9728, OAuth 2.0 Protected Resource Metadata.** Each Weave serves
  ``/.well-known/oauth-protected-resource`` naming itself (``resource``), the
  authorization server that mints its tokens, the scopes it understands and
  whether it accepts DPoP. The MCP authorization specification requires a
  server to publish this and to point at it from every ``401``.
* **RFC 6750 / RFC 9449 challenges.** ``www_authenticate`` builds the
  ``WWW-Authenticate`` header for a refusal: the scheme (``Bearer`` or
  ``DPoP``), an ``error`` code, the scope that would have worked, and the
  ``resource_metadata`` URL (RFC 9728 §5.1) so an agent that has never seen
  this Weave can bootstrap from a single failed request.

Nothing here touches a token. Values that end up inside a quoted header
parameter are filtered to a safe alphabet, so a scope or URL from
configuration cannot inject a second header.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

WELL_KNOWN_PATH = "/.well-known/oauth-protected-resource"
ENV_AUTHORIZATION_SERVER = "CIPHERWEAVE_AUTHORIZATION_SERVER"
_JWKS_SUFFIX = "/.well-known/jwks.json"
_SAFE = re.compile(r"[^A-Za-z0-9 :/._~?#\[\]@!$&'()*+,;=%-]")


def _quote(value: str) -> str:
    return _SAFE.sub("", value).replace('"', "")


def protected_resource_metadata(
    *,
    resource: str,
    authorization_servers: Iterable[str],
    scopes_supported: Iterable[str] = (),
    resource_name: str | None = None,
    dpop_supported: bool = True,
    dpop_required: bool = False,
    documentation: str | None = None,
    jwks_uri: str | None = None,
) -> dict[str, Any]:
    """The RFC 9728 document. ``resource`` is the Weave's audience URI or name."""
    servers = [s for s in authorization_servers if s]
    if not resource or not servers:
        raise ValueError("resource and at least one authorization server are required")
    doc: dict[str, Any] = {
        "resource": resource,
        "authorization_servers": servers,
        "bearer_methods_supported": ["header"],
        "scopes_supported": sorted(set(scopes_supported)),
        "resource_signing_alg_values_supported": ["ES256"],
    }
    if resource_name:
        doc["resource_name"] = resource_name
    if dpop_supported:
        doc["dpop_signing_alg_values_supported"] = ["ES256"]
        doc["dpop_bound_access_tokens_required"] = bool(dpop_required)
    if documentation:
        doc["resource_documentation"] = documentation
    if jwks_uri:
        doc["jwks_uri"] = jwks_uri
    return doc


def www_authenticate(
    *,
    error: str | None = None,
    error_description: str | None = None,
    scope: str | Iterable[str] | None = None,
    resource_metadata: str | None = None,
    dpop: bool = False,
    realm: str | None = None,
) -> str:
    """A ``WWW-Authenticate`` value. ``dpop`` selects the DPoP scheme (RFC 9449 §7.1)."""
    params: list[str] = []
    if realm:
        params.append(f'realm="{_quote(realm)}"')
    if error:
        params.append(f'error="{_quote(error)}"')
    if error_description:
        params.append(f'error_description="{_quote(error_description)}"')
    if scope:
        text = scope if isinstance(scope, str) else " ".join(scope)
        params.append(f'scope="{_quote(text)}"')
    if resource_metadata:
        params.append(f'resource_metadata="{_quote(resource_metadata)}"')
    if dpop:
        params.append('algs="ES256"')
    scheme = "DPoP" if dpop else "Bearer"
    return scheme + (" " + ", ".join(params) if params else "")


def resource_metadata_url(base_url: str) -> str:
    """``https://host/stage`` → ``https://host/stage/.well-known/oauth-protected-resource``."""
    return base_url.rstrip("/") + WELL_KNOWN_PATH


def default_authorization_server() -> str | None:
    """CipherWeave's base URL: ``CIPHERWEAVE_AUTHORIZATION_SERVER``, else derived.

    Every Weave already has ``CIPHERWEAVE_JWKS_URL`` (``<base>/.well-known/
    jwks.json``), so the base needs no new deploy parameter: strip the suffix.
    None when neither is set -- a Weave then serves no metadata rather than
    metadata naming a server that is not there.
    """
    explicit = (os.environ.get(ENV_AUTHORIZATION_SERVER) or "").strip()
    if explicit:
        return explicit.rstrip("/")
    jwks_url = (os.environ.get("CIPHERWEAVE_JWKS_URL") or "").strip()
    if jwks_url.startswith("https://") and jwks_url.endswith(_JWKS_SUFFIX):
        return jwks_url[: -len(_JWKS_SUFFIX)]
    return None


def _lower_headers(event: Mapping[str, Any]) -> dict[str, str]:
    raw = event.get("headers") or {}
    return {str(k).lower(): str(v) for k, v in raw.items() if v is not None}


def api_gateway_request(event: Mapping[str, Any]) -> tuple[str, str | None, str | None]:
    """(method, absolute URL, base URL) of an API Gateway HTTP API (v2) event.

    The URL is what a DPoP proof's ``htu`` must name: ``https://`` +
    ``requestContext.domainName`` + ``rawPath`` (stage included). The domain
    comes from API Gateway's own request context, not the client's Host
    header. The base is the URL up to and including the stage, for building
    the resource-metadata URL.
    """
    context = event.get("requestContext") or {}
    method = str(((context.get("http") or {}).get("method")) or "GET").upper()
    domain = context.get("domainName")
    raw_path = event.get("rawPath") or (context.get("http") or {}).get("path")
    if not domain or not raw_path:
        return method, None, None
    stage = context.get("stage")
    prefix = f"/{stage}" if stage and stage != "$default" else ""
    return method, f"https://{domain}{raw_path}", f"https://{domain}{prefix}"
