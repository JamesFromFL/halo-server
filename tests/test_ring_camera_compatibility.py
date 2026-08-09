from types import SimpleNamespace

import pytest
from ring_doorbell import const as ring_const
from ring_doorbell.doorbot import RingDoorBell
from ring_doorbell.stickup_cam import RingStickUpCam

from halo_server.media.ring_speaker import (
    VALIDATED_INTERCOM_VIDEO_KINDS,
    supports_ring_camera_intercom,
)
from halo_server.ring.devices import summarize_device, supports_ring_camera_snapshot

PINNED_DOORBELL_KINDS = frozenset(
    {
        "cocoa_doorbell",
        "cocoa_doorbell_v2",
        "df_doorbell_clownfish",
        "doorbell",
        "doorbell_graham_cracker",
        "doorbell_oyster",
        "doorbell_portal",
        "doorbell_scallop",
        "doorbell_scallop_lite",
        "doorbell_v3",
        "doorbell_v4",
        "doorbell_v5",
        "doorbot",
        "jbox_v1",
        "lpd_v1",
        "lpd_v2",
        "lpd_v3",
        "lpd_v4",
    }
)
PINNED_STICKUP_KINDS = frozenset(
    {
        "cocoa_camera",
        "cocoa_floodlight",
        "cocoa_spotlight",
        "floodlight_pro",
        "floodlight_v2",
        "hp_cam_v1",
        "hp_cam_v2",
        "spotlightw_v2",
        "stickup_cam",
        "stickup_cam_elite",
        "stickup_cam_longfin",
        "stickup_cam_lunar",
        "stickup_cam_mini",
        "stickup_cam_mini_ptz_v1",
        "stickup_cam_mini_v2",
        "stickup_cam_v3",
        "stickup_cam_v4",
        "stickup_cam_wired",
    }
)
PINNED_DOORBELL_BATTERY_KINDS = frozenset(
    {
        "cocoa_doorbell",
        "cocoa_doorbell_v2",
        "df_doorbell_clownfish",
        "doorbell",
        "doorbell_oyster",
        "doorbell_portal",
        "doorbell_scallop",
        "doorbell_scallop_lite",
        "doorbell_v3",
        "doorbell_v4",
        "doorbell_v5",
        "doorbot",
    }
)
PINNED_STICKUP_BATTERY_KINDS = frozenset(
    {
        "cocoa_camera",
        "stickup_cam",
        "stickup_cam_lunar",
        "stickup_cam_v3",
        "stickup_cam_v4",
    }
)
PINNED_LIGHT_KINDS = frozenset(
    {
        "cocoa_floodlight",
        "cocoa_spotlight",
        "floodlight_pro",
        "floodlight_v2",
        "hp_cam_v1",
        "hp_cam_v2",
        "spotlightw_v2",
        "stickup_cam_longfin",
        "stickup_cam_v4",
    }
)
PINNED_SDK_SIREN_KINDS = frozenset(
    {
        "cocoa_camera",
        "cocoa_floodlight",
        "cocoa_spotlight",
        "floodlight_pro",
        "floodlight_v2",
        "hp_cam_v1",
        "hp_cam_v2",
        "spotlightw_v2",
        "stickup_cam_elite",
        "stickup_cam_longfin",
        "stickup_cam_lunar",
        "stickup_cam_mini",
        "stickup_cam_mini_ptz_v1",
        "stickup_cam_mini_v2",
        "stickup_cam_v4",
        "stickup_cam_wired",
    }
)

_DOORBELL_GROUPS = (
    ring_const.DOORBELL_KINDS,
    ring_const.DOORBELL_2_KINDS,
    ring_const.DOORBELL_3_KINDS,
    ring_const.DOORBELL_4_KINDS,
    ring_const.DOORBELL_3_PLUS_KINDS,
    ring_const.DOORBELL_PRO_KINDS,
    ring_const.DOORBELL_PRO_2_KINDS,
    ring_const.DOORBELL_ELITE_KINDS,
    ring_const.DOORBELL_WIRED_KINDS,
    ring_const.DOORBELL_BATTERY_KINDS,
    ring_const.PEEPHOLE_CAM_KINDS,
    ring_const.DOORBELL_GEN2_KINDS,
)
_STICKUP_GROUPS = (
    ring_const.FLOODLIGHT_CAM_KINDS,
    ring_const.FLOODLIGHT_CAM_PRO_KINDS,
    ring_const.FLOODLIGHT_CAM_PLUS_KINDS,
    ring_const.INDOOR_CAM_KINDS,
    ring_const.INDOOR_CAM_GEN2_KINDS,
    ring_const.INDOOR_CAM_PTZ_KINDS,
    ring_const.SPOTLIGHT_CAM_BATTERY_KINDS,
    ring_const.SPOTLIGHT_CAM_WIRED_KINDS,
    ring_const.SPOTLIGHT_CAM_PLUS_KINDS,
    ring_const.SPOTLIGHT_CAM_PRO_KINDS,
    ring_const.STICKUP_CAM_KINDS,
    ring_const.STICKUP_CAM_BATTERY_KINDS,
    ring_const.STICKUP_CAM_ELITE_KINDS,
    ring_const.STICKUP_CAM_GEN3_KINDS,
)


def _sdk_camera(camera_type, family, kind, *, shared=False):
    ring = SimpleNamespace(
        devices_data={
            family: {
                1: {
                    "id": 1,
                    "kind": kind,
                    "description": kind,
                    "device_id": f"test-{kind}",
                    "battery_life": "75",
                    "battery_life_2": "",
                }
            }
        }
    )
    if camera_type is RingDoorBell:
        return camera_type(ring, 1, shared=shared)
    return camera_type(ring, 1)


def test_pinned_sdk_camera_kind_inventory_is_exact_and_disjoint():
    doorbell_kinds = [kind for group in _DOORBELL_GROUPS for kind in group]
    stickup_kinds = [kind for group in _STICKUP_GROUPS for kind in group]

    assert len(doorbell_kinds) == len(set(doorbell_kinds)) == 18
    assert len(stickup_kinds) == len(set(stickup_kinds)) == 18
    assert frozenset(doorbell_kinds) == PINNED_DOORBELL_KINDS
    assert frozenset(stickup_kinds) == PINNED_STICKUP_KINDS
    assert PINNED_DOORBELL_KINDS.isdisjoint(PINNED_STICKUP_KINDS)
    assert len(PINNED_DOORBELL_KINDS | PINNED_STICKUP_KINDS) == 36


@pytest.mark.parametrize(
    ("camera_type", "family", "kind"),
    [
        *((RingDoorBell, "doorbots", kind) for kind in sorted(PINNED_DOORBELL_KINDS)),
        *((RingStickUpCam, "stickup_cams", kind) for kind in sorted(PINNED_STICKUP_KINDS)),
    ],
)
def test_all_pinned_sdk_camera_kinds_have_halo_media_shape(camera_type, family, kind):
    camera = _sdk_camera(camera_type, family, kind)
    summary = summarize_device(camera)

    assert camera.model not in {"Unknown Doorbell", "Unknown Stickup Cam"}
    assert camera.has_capability("video") is True
    assert camera.has_capability("motion_detection") is True
    assert supports_ring_camera_snapshot(camera) is True
    assert supports_ring_camera_intercom(camera) is True
    assert summary is not None
    assert summary.kind == kind
    assert summary.family == family
    assert summary.has_intercom is True
    battery_kinds = PINNED_DOORBELL_BATTERY_KINDS | PINNED_STICKUP_BATTERY_KINDS
    assert camera.has_capability("battery") is (kind in battery_kinds)
    assert summary.battery_percent == (75 if kind in battery_kinds else None)
    assert camera.has_capability("light") is (kind in PINNED_LIGHT_KINDS)
    assert summary.has_light is (kind in PINNED_LIGHT_KINDS)
    assert camera.has_capability("siren") is (kind in PINNED_SDK_SIREN_KINDS)
    assert summary.has_siren is (kind in PINNED_SDK_SIREN_KINDS)


def test_shared_doorbell_inventory_family_uses_same_pinned_media_contract():
    camera = _sdk_camera(
        RingDoorBell,
        "authorized_doorbots",
        "lpd_v4",
        shared=True,
    )

    summary = summarize_device(camera)

    assert summary is not None
    assert summary.family == "authorized_doorbots"
    assert supports_ring_camera_snapshot(camera) is True
    assert supports_ring_camera_intercom(camera) is True


def test_observed_mini_v3_is_an_explicit_halo_override_not_a_pinned_sdk_kind():
    kind = "stickup_cam_mini_v3"
    camera = _sdk_camera(RingStickUpCam, "stickup_cams", kind)

    summary = summarize_device(camera)

    assert kind not in PINNED_STICKUP_KINDS
    assert camera.has_capability("video") is False
    assert {kind} == VALIDATED_INTERCOM_VIDEO_KINDS
    assert supports_ring_camera_snapshot(camera) is True
    assert supports_ring_camera_intercom(camera) is True
    assert summary is not None
    assert summary.model == "Unknown Stickup Cam"
    assert summary.battery_percent is None
    assert camera.has_capability("siren") is False
    assert summary.has_light is False
    assert summary.has_siren is True
