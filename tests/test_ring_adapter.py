"""Ring adapter URL mapping.

Production logged 404 for GET /clients_api/other/{id}: a RingLight discovered
in the ``other`` family was read at ``{family}/{id}``, a path Ring does not
serve for any family. Expected paths are the ring_doorbell 0.9.x constants
(HEALTH_DOORBELL_ENDPOINT, DEVICES_ENDPOINT, LIGHTS_ENDPOINT).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import pytest

from providers import ring_adapter
from providers.base import ProviderError

_BASE = "https://api.ring.com/clients_api"


class _Resp:
    def __init__(self, body: Any) -> None:
        self._body = body
        self.content_length = 0

    async def __aenter__(self) -> "_Resp":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self, content_type: Optional[str] = None) -> Any:
        return self._body


class _Session:
    def __init__(self, listing: Optional[Dict[str, Any]] = None) -> None:
        self.calls: List[Tuple[str, str]] = []
        self._listing = listing or {}

    def get(self, url: str, headers: Dict[str, str]) -> _Resp:
        self.calls.append(("GET", url))
        if url == f"{_BASE}/ring_devices":
            return _Resp(self._listing)
        return _Resp({"device_health": {"network_connection_type": "wifi"}})

    def put(self, url: str, headers: Dict[str, str], json: Any = None) -> _Resp:
        self.calls.append(("PUT", url))
        return _Resp({})


def _run(session: _Session, device_type: str, category: str, action: str) -> Dict[str, Any]:
    adapter = ring_adapter.RingAdapter()
    return asyncio.run(adapter._dispatch(
        session, "tok", "hw", "134879960", "134879960",
        category, device_type, action, {},
    ))


_LISTING = {
    "doorbots": [],
    "stickup_cams": [],
    "other": [{"id": 134879960, "kind": "x", "description": "Porch", "led_status": "on"}],
}


@pytest.mark.parametrize("device_type,category,expected", [
    ("RingDoorbell", "doorbots", f"{_BASE}/doorbots/134879960/health"),
    ("RingCamera", "stickup_cams", f"{_BASE}/stickup_cams/134879960/health"),
    ("RingLight", "other", f"{_BASE}/ring_devices"),
    ("RingLight", "stickup_cams", f"{_BASE}/ring_devices"),
])
def test_get_status_url_per_provider_type(device_type, category, expected):
    listing = {"stickup_cams": [{"id": 134879960, "led_status": "off"}]} \
        if category == "stickup_cams" else _LISTING
    session = _Session(listing)
    _run(session, device_type, category, "get_status")
    assert session.calls == [("GET", expected)]
    assert not any("/other/" in url for _, url in session.calls)


def test_light_status_reads_led_status_from_the_discovered_family():
    session = _Session(_LISTING)
    assert _run(session, "RingLight", "other", "get_status")["state"] == "on"


def test_light_missing_from_listing_is_a_provider_error():
    with pytest.raises(ProviderError):
        _run(_Session({"other": []}), "RingLight", "other", "get_status")


@pytest.mark.parametrize("action,state", [("turn_on", "on"), ("turn_off", "off")])
def test_light_switch_url(action, state):
    session = _Session(_LISTING)
    _run(session, "RingLight", "other", action)
    assert session.calls == [("PUT", f"{_BASE}/doorbots/134879960/floodlight_light_{state}")]
