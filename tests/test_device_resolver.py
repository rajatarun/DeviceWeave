"""
Unit tests for device_resolver.resolve_device()'s TF-vector cosine path.

The task refers to this as the "cosine path with a stubbed embedding" —
device_resolver does not actually use embeddings (there is no vector model
call anywhere in this module: it is a deterministic bag-of-words TF-cosine
similarity over sample/learned phrases, per its own module docstring). The
"embedding" stubbed here is the phrase corpus device_resolver builds from
_get_active_catalog()/_get_learned_phrases(), which is what actually feeds
the cosine computation.
"""

import device_resolver as dr

CATALOG = [
    {
        "id": "office_light",
        "name": "Office Light",
        "device_type": "light",
        "capabilities": ["turn_on", "turn_off", "set_brightness"],
        "ip": "",
        "model": "",
        "sample_phrases": [],
    },
    {
        "id": "office_fan",
        "name": "Office Fan",
        "device_type": "fan",
        "capabilities": ["turn_on", "turn_off"],
        "ip": "",
        "model": "",
        "sample_phrases": [],
    },
]

LEARNED = {
    "office_light": ["office light", "desk lamp"],
    "office_fan": ["office fan", "the fan"],
}


def _stub_registry(monkeypatch, catalog=CATALOG, learned=LEARNED):
    monkeypatch.setattr(dr, "_get_active_catalog", lambda: catalog)
    monkeypatch.setattr(dr, "_get_learned_phrases", lambda: learned)


def test_resolve_device_picks_best_cosine_match(monkeypatch):
    _stub_registry(monkeypatch)

    device, confidence = dr.resolve_device("turn on the office light")

    assert device["id"] == "office_light"
    assert confidence == 0.8944


def test_resolve_device_distinguishes_similarly_named_devices(monkeypatch):
    _stub_registry(monkeypatch)

    device, confidence = dr.resolve_device("turn on the fan")

    assert device["id"] == "office_fan"
    assert confidence == 0.8321


def test_resolve_device_returns_none_on_blank_query(monkeypatch):
    _stub_registry(monkeypatch)

    device, confidence = dr.resolve_device("   ")

    assert device is None
    assert confidence == 0.0


def test_resolve_device_returns_none_when_catalog_empty(monkeypatch):
    _stub_registry(monkeypatch, catalog=[], learned={})

    device, confidence = dr.resolve_device("turn on the office light")

    assert device is None
    assert confidence == 0.0


def test_resolve_device_raises_when_registry_not_configured(monkeypatch):
    # _get_active_catalog() itself raises DeviceRegistryError when
    # DEVICE_REGISTRY_TABLE is unset — resolve_device must propagate it
    # rather than swallowing it (app.py depends on this to return a 503).
    import pytest

    # _REGISTRY_TABLE is read from the env once at import time; patch it
    # directly rather than relying on a post-import env var change.
    monkeypatch.setattr(dr, "_REGISTRY_TABLE", "")
    dr.invalidate_device_registry_cache()

    with pytest.raises(dr.DeviceRegistryError):
        dr.resolve_device("turn on the office light")


def test_device_public_view_strips_ip_and_truncates_long_ids():
    device = {
        "id": "a" * 20,
        "name": "Office Light",
        "device_type": "light",
        "capabilities": ["turn_on"],
        "ip": "192.168.1.50",
    }

    view = dr.device_public_view(device)

    assert "ip" not in view
    assert view["id"] == "a" * 20
    assert view["id_truncated"] == "a" * 16 + "…"
