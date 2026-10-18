"""
Minimal client for eero's unofficial cloud API.

This is the same API the open-source ``eero-client`` library and the Home
Assistant eero integration call. It is not a published eero product API and
can change without notice.

Auth (verified against 343max/eero-client, EvanSchalton/eero-client 2.2.6,
and schmittx/home-assistant-eero):

  POST /2.2/login
      body {"login": "<email or phone>"}
      response data.user_token is a short-lived verification session
  POST /2.2/login/verify
      Cookie ``s=<user_token>``
      body {"code": "<sms or email code>"}
      the same user_token is the long-lived session cookie afterwards
      (verify may also return a replacement user_token)
  GET /2.2/account
      Cookie ``s=<session>``
      networks live at data.networks.data[].url (or a bare list)
  GET /2.2/networks/<network id>/devices
      Cookie ``s=<session>``
      data is the client list (or data.devices)

A 401/403, including meta error ``error.session.refresh`` and
``error.session.invalid``, is an auth failure. Callers must not treat that
as "nobody is home".
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

API_BASE = "https://api-user.e2ro.com/2.2"
_MAX_BYTES = 2_000_000
_TIMEOUT_SECONDS = 8
_USER_AGENT = "DeviceWeave eero-presence (unofficial eero-client compatible)"

UrlOpen = Callable[..., Any]


class EeroError(Exception):
    """Base error for the unofficial eero API."""


class EeroAuthError(EeroError):
    """The session is missing, expired, or rejected. Do not mark anyone away."""

    def __init__(self, message: str, status: Optional[int] = None, error_code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.error_code = error_code


class EeroApiError(EeroError):
    """A transport or non-auth API failure. Also not evidence that anyone left."""


def login(identifier: str, urlopen: Optional[UrlOpen] = None) -> str:
    """Start login. Returns the user_token that must be sent to login/verify."""
    identifier = (identifier or "").strip()
    if not identifier:
        raise EeroAuthError("missing email or phone")
    data = request_json("POST", "login", body={"login": identifier}, urlopen=urlopen)
    token = data.get("user_token") if isinstance(data, dict) else None
    if not token:
        raise EeroAuthError("login did not return a user_token")
    return str(token)


def login_verify(code: str, user_token: str, urlopen: Optional[UrlOpen] = None) -> str:
    """
    Finish login. Returns the session token to store.

    The session cookie is the user_token from login, matching eero-client.
    If verify returns a new user_token, that value replaces it.
    """
    code = (code or "").strip()
    if not code:
        raise EeroAuthError("missing verification code")
    if not user_token:
        raise EeroAuthError("missing user_token from login")
    data = request_json(
        "POST",
        "login/verify",
        session_token=user_token,
        body={"code": code},
        urlopen=urlopen,
    )
    if isinstance(data, dict) and data.get("user_token"):
        return str(data["user_token"])
    return user_token


def list_connected_clients(
    session_token: str,
    network_id: Optional[str] = None,
    urlopen: Optional[UrlOpen] = None,
) -> List[Dict[str, Any]]:
    """
    Return the raw client dicts for every network on the account.

    ``network_id`` limits the poll to one network. An id that is not on the
    account is an API error, not an empty home.
    """
    if not session_token:
        raise EeroAuthError("missing session token")
    account = request_json("GET", "account", session_token=session_token, urlopen=urlopen)
    network_ids = extract_network_ids(account)
    if network_id:
        wanted = str(network_id).strip()
        if wanted not in network_ids:
            raise EeroApiError(f"eero account has no network {wanted}")
        network_ids = [wanted]
    if not network_ids:
        raise EeroApiError("eero account returned no networks")
    clients: List[Dict[str, Any]] = []
    for nid in network_ids:
        payload = request_json(
            "GET",
            f"networks/{nid}/devices",
            session_token=session_token,
            urlopen=urlopen,
        )
        clients.extend(unwrap_devices(payload))
    return clients


def request_json(
    method: str,
    path: str,
    *,
    session_token: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
    urlopen: Optional[UrlOpen] = None,
) -> Any:
    """Perform one eero call and return the unwrapped ``data`` payload."""
    if urlopen is None:
        urlopen = urllib.request.urlopen
    url = API_BASE.rstrip("/") + "/" + path.lstrip("/")
    headers = {
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if session_token:
        headers["Cookie"] = f"s={session_token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            status = int(getattr(resp, "status", 200))
            raw = resp.read(_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read(_MAX_BYTES + 1)
    except urllib.error.URLError as exc:
        raise EeroApiError(f"network error calling eero: {exc.reason}") from exc
    if len(raw) > _MAX_BYTES:
        raise EeroApiError("eero response exceeded size limit")
    return _unwrap(status, raw)


def extract_network_ids(account_data: Any) -> List[str]:
    """Pull network ids out of the shapes this API has returned."""
    if not isinstance(account_data, dict):
        return []
    networks = account_data.get("networks")
    if isinstance(networks, dict):
        networks = networks.get("data") or networks.get("networks") or []
    if not isinstance(networks, list):
        return []
    ids: List[str] = []
    for net in networks:
        if not isinstance(net, dict):
            continue
        found = None
        for key in ("id", "network_id"):
            if net.get(key) is not None and str(net.get(key)).strip():
                found = str(net[key]).strip()
                break
        if found is None:
            match = re.search(r"/networks/(\d+)", str(net.get("url") or ""))
            if match:
                found = match.group(1)
        if found and found not in ids:
            ids.append(found)
    return ids


def unwrap_devices(payload: Any) -> List[Dict[str, Any]]:
    """Accept a bare list or an object that wraps the list under devices/data."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("devices", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def parse_secret_string(secret_string: str) -> str:
    """
    Accept either a raw session token or JSON written by scripts/eero_login.py.

    JSON keys, first hit wins: session_token, user_token, token, cookie.
    """
    text = (secret_string or "").strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return ""
        if not isinstance(data, dict):
            return ""
        for key in ("session_token", "user_token", "token", "cookie"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    return text


def _unwrap(status: int, raw: bytes) -> Any:
    try:
        payload = json.loads(raw.decode("utf-8") or "null")
    except json.JSONDecodeError as exc:
        if status in (401, 403):
            raise EeroAuthError(f"HTTP {status}", status=status) from exc
        raise EeroApiError(f"eero returned non-JSON (HTTP {status})") from exc
    if not isinstance(payload, dict):
        raise EeroApiError("eero response was not an object")
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    error = str(meta.get("error") or "")
    code = meta.get("code", status)
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        code_int = status
    if status in (401, 403) or code_int in (401, 403):
        raise EeroAuthError(error or f"HTTP {status}", status=status, error_code=error or None)
    if status >= 400 or code_int >= 400:
        raise EeroApiError(error or f"eero HTTP {status}")
    if "data" in payload:
        return payload["data"]
    return payload
