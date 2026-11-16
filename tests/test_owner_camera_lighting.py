"""Lighting observations through owner IPC never become device operations."""

import copy
import time

import pytest
from test_native_service import Camera, service_fixture

from halo_server.api.admin import AdminServer
from halo_server.camera_lighting import camera_lighting_receipt
from halo_server.owner_client import AdminClient, AdminClientError
from halo_server.ring.lighting import client_lighting


@pytest.mark.asyncio
async def test_owner_lighting_roundtrip_is_passive_and_scoped(tmp_path):
    service, ring, _ = service_fixture(
        tmp_path, cameras=[Camera(), Camera("43", capabilities=("video",))]
    )
    camera = ring.all_devices[0]
    camera._attrs.update(led_status="off")
    camera._attrs["settings"]["floodlight_settings"] = {
        "duration": 60,
        "always_on": False,
        "always_on_duration": 0,
        "always_on_duration_secs": 0,
    }
    reservation = client_lighting(ring)
    ring._camera_lighting_receipt = camera_lighting_receipt(
        ring.all_devices, received_at=time.monotonic(), mutation_revision=reservation.revision
    )
    server = AdminServer(tmp_path / "lighting.sock", service.handle_admin)
    await server.start()
    try:
        client = AdminClient(server.socket_path)
        files = set(tmp_path.rglob("*"))
        page = await client.camera_controls()
        lighting = page["cameras"][0]["lighting"]
        assert lighting["fresh"] and lighting["light"] is False
        assert lighting["motion_duration_seconds"] == 60 and lighting["reason"] is None
        assert lighting["manual_timer_status"] == "unverified"
        assert lighting["activation_source"] is None
        assert "lighting" not in page["cameras"][1]
        token = reservation.try_acquire()
        assert reservation.mark_sent(token)
        reservation.release(token)
        changed = (await client.camera_controls())["cameras"][0]["lighting"]
        assert not changed["fresh"] and changed["reason"] == "lighting_changed"
        assert changed["light"] is None and changed["motion_duration_seconds"] is None
        assert set(tmp_path.rglob("*")) == files
        assert ring.submissions == camera.snapshot_calls == 0
        assert camera.light_values == [] and not service._media_sessions
    finally:
        await server.stop()


BASE = {
    "fresh": True,
    "observed_age_seconds": 1.0,
    "light": False,
    "motion_duration_seconds": 60,
    "manual_timer_status": "unverified",
    "activation_source": None,
    "reason": None,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lighting",
    [
        None,
        [],
        BASE | {"fresh": "true"},
        BASE | {"observed_age_seconds": True},
        BASE | {"observed_age_seconds": -1},
        BASE | {"observed_age_seconds": float("nan")},
        BASE | {"observed_age_seconds": 2**4096},
        BASE | {"observed_age_seconds": 91},
        BASE | {"light": "on"},
        BASE | {"motion_duration_seconds": True},
        BASE | {"motion_duration_seconds": 0},
        BASE | {"motion_duration_seconds": 901},
        BASE | {"manual_timer_status": "unlimited"},
        BASE | {"activation_source": "motion"},
        BASE | {"reason": []},
        BASE | {"fresh": False, "reason": "source_stale"},
        BASE | {"reason": "source_unknown"},
        BASE | {"extra": "unrecognized"},
    ],
)
async def test_invalid_lighting_reply_is_rejected_without_echo(lighting):
    client = AdminClient(None)

    async def response(method, **params):
        assert method == "camera_control_list" and not params
        return {
            "profile_id": "private-profile",
            "account_generation": 1,
            "cameras": [{"camera_id": "42", "device_generation": 1, "lighting": lighting}],
        }

    client.request = response
    with pytest.raises(AdminClientError) as error:
        await client.camera_controls()
    assert error.value.code == "bad_response"
    assert "private-profile" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {},  # Compatible with a daemon that predates lighting observations.
        {"lighting": BASE},
        {"lighting": BASE | {"light": None, "reason": "source_unknown"}},
        {"lighting": BASE | {"motion_duration_seconds": None, "reason": "source_unknown"}},
        {
            "lighting": BASE
            | {
                "fresh": False,
                "observed_age_seconds": 100,
                "light": None,
                "motion_duration_seconds": None,
                "reason": "source_stale",
            }
        },
    ],
)
async def test_partial_and_legacy_lighting_replies_remain_available(fields):
    client = AdminClient(None)
    payload = {
        "account_generation": 1,
        "cameras": [{"camera_id": "42", "device_generation": 1} | fields],
    }

    async def response(method, **params):
        return copy.deepcopy(payload)

    client.request = response
    assert await client.camera_controls() == payload
