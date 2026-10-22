"""Ring devices get the DeviceWeave type that matches what they can do.

ring-doorbell gives the light capability to floodlight and spotlight cameras,
which live in the stickup_cams family. Registered as RingCamera they could not
be switched. Its "other" family is intercoms, which were registered as
RingLight and sent light commands.
"""
from __future__ import annotations

import pytest

from ingestion.providers import ring_discovery as rd


@pytest.mark.parametrize("kind", ["hp_cam_v1", "floodlight_v2", "floodlight_pro",
                                  "cocoa_floodlight", "stickup_cam_v4", "hp_cam_v2",
                                  "spotlightw_v2", "cocoa_spotlight", "stickup_cam_longfin"])
def test_floodlight_and_spotlight_cams_are_switchable_lights(kind):
    assert rd._device_type("stickup_cams", {"kind": kind}) == "RingLight"
    assert "turn_on" in rd._capabilities("RingLight")


@pytest.mark.parametrize("kind", ["stickup_cam_v3", "cocoa_camera", "stickup_cam_mini"])
def test_cameras_without_a_light_stay_cameras(kind):
    assert rd._device_type("stickup_cams", {"kind": kind}) == "RingCamera"


@pytest.mark.parametrize("kind", ["intercom_handset_audio", "intercom_handset_video",
                                  "chime_pro", "hp_base_v1"])
def test_intercoms_chimes_and_hubs_are_not_registered(kind):
    assert rd._device_type("other", {"kind": kind, "id": 1}) is None


def test_doorbells_and_unknown_other_kinds_are_unchanged():
    assert rd._device_type("doorbots", {"kind": "lpd_v2"}) == "RingDoorbell"
    assert rd._device_type("authorized_doorbots", {"kind": "x"}) == "RingDoorbell"
    assert rd._device_type("other", {"kind": "something_new"}) == "RingLight"
    assert rd._device_type("beams", {"kind": "x"}) is None


def test_the_kind_lists_match_the_installed_ring_doorbell():
    const = pytest.importorskip("ring_doorbell.const")
    lights = set(const.FLOODLIGHT_CAM_KINDS + const.FLOODLIGHT_CAM_PRO_KINDS
                 + const.FLOODLIGHT_CAM_PLUS_KINDS + const.SPOTLIGHT_CAM_BATTERY_KINDS
                 + const.SPOTLIGHT_CAM_WIRED_KINDS + const.SPOTLIGHT_CAM_PLUS_KINDS
                 + const.SPOTLIGHT_CAM_PRO_KINDS)
    assert set(rd._LIGHT_CAM_KINDS) == lights
    assert set(rd._INTERCOM_KINDS) == set(const.INTERCOM_KINDS)
