"""CipherWeave access tokens on /mcp, and the legacy token behind ACCEPT_LEGACY_TOKEN.

Tokens are minted by cipherweave_verify.testing.LocalIssuer — the issuer's
exact format (CipherWeave's own tests hold the two together) with a key that
exists only in this process. Its JWKS is served in memory in place of the
HTTPS fetch, so nothing here touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cipherweave_verify import jwks as cw_jwks
from cipherweave_verify import reset_default_verifier
from cipherweave_verify.testing import LocalIssuer

import mcp_handler

LEGACY = "legacy-static-token-value"
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def issuer(monkeypatch):
    issuer = LocalIssuer()
    monkeypatch.setenv("CIPHERWEAVE_JWKS_URL", "https://cipherweave.test/prod/.well-known/jwks.json")
    monkeypatch.setenv("CIPHERWEAVE_ISSUER", "urn:cipherweave:prod")
    monkeypatch.delenv("ACCEPT_LEGACY_TOKEN", raising=False)
    monkeypatch.setattr(cw_jwks, "http_fetcher", issuer.fetcher)
    monkeypatch.setattr(mcp_handler, "_load_expected_token", lambda: LEGACY)
    reset_default_verifier()
    yield issuer
    reset_default_verifier()


def _health(token):
    event = {
        "requestContext": {"http": {"method": "POST", "path": "/mcp"}},
        "headers": {"authorization": f"Bearer {token}"} if token is not None else {},
        "body": json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "health", "arguments": {}},
        }),
    }
    return mcp_handler.handler(event, None)


def _is_healthy(response) -> bool:
    if response["statusCode"] != 200:
        return False
    result = json.loads(response["body"])["result"]
    return json.loads(result["content"][0]["text"])["status"] == "healthy"


def test_cipherweave_token_calls_the_health_tool(issuer, monkeypatch):
    def _no_secret():
        raise AssertionError("a valid JWT must not read the legacy secret")

    monkeypatch.setattr(mcp_handler, "_load_expected_token", _no_secret)
    assert _is_healthy(_health(issuer.mint(audience="deviceweave")))


@pytest.mark.parametrize("audience", ["modelweave", "contextweave", "toolweave"])
def test_token_for_another_weave_is_401(issuer, audience):
    response = _health(issuer.mint(audience=audience))
    assert response["statusCode"] == 401
    assert json.loads(response["body"])["error"]["code"] == -32001


def test_expired_and_forged_tokens_are_401(issuer):
    assert _health(issuer.mint(audience="deviceweave", now=1_000_000_000))["statusCode"] == 401
    impostor = LocalIssuer()
    forged = impostor.mint(audience="deviceweave", header_overrides={"kid": issuer.kid})
    assert _health(forged)["statusCode"] == 401
    assert _health(issuer.mint(audience="deviceweave", header_overrides={"alg": "none"}))["statusCode"] == 401


def test_legacy_token_still_works_by_default(issuer):
    assert _is_healthy(_health(LEGACY))


def test_legacy_flag_off_rejects_the_static_token_and_keeps_jwts(issuer, monkeypatch):
    monkeypatch.setenv("ACCEPT_LEGACY_TOKEN", "false")

    def _no_secret():
        raise AssertionError("with the flag off the legacy secret is never read")

    monkeypatch.setattr(mcp_handler, "_load_expected_token", _no_secret)
    assert _health(LEGACY)["statusCode"] == 401
    assert _is_healthy(_health(issuer.mint(audience="deviceweave")))


def test_without_a_jwks_url_only_the_legacy_token_works(issuer, monkeypatch):
    monkeypatch.delenv("CIPHERWEAVE_JWKS_URL")
    reset_default_verifier()
    assert _health(issuer.mint(audience="deviceweave"))["statusCode"] == 401
    assert _is_healthy(_health(LEGACY))


def test_rejection_body_never_echoes_the_token(issuer):
    token = issuer.mint(audience="modelweave")
    response = _health(token)
    for part in token.split("."):
        assert part not in response["body"]


def test_template_and_workflow_wire_the_verifier():
    template = (REPO_ROOT / "template.yaml").read_text(encoding="utf-8")
    for needle in ("CipherWeaveJwksUrl", "CIPHERWEAVE_JWKS_URL", "AcceptLegacyToken", "ACCEPT_LEGACY_TOKEN"):
        assert needle in template
    workflow = (REPO_ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "requirements-cipherweave.txt" in workflow
    assert "--target src/" in workflow
    assert "IssuerJwksUrl" in workflow
    pin = (REPO_ROOT / "requirements-cipherweave.txt").read_text(encoding="utf-8")
    # Pinned to a full commit, not a branch that can move under a deploy.
    assert "#subdirectory=cipherweave_verify" in pin
    assert any(len(part.split("#")[0]) == 40 for part in pin.split("@"))
