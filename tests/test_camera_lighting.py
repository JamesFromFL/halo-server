"""Camera light projections from synthetic GETs; no Ring/device commands."""

import asyncio
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

import halo_server.camera_lighting as lighting
import halo_server.ring.client as ring_client
from halo_server.account_scope import AccountScope
from halo_server.camera_lighting import (
    LIGHTING_STATE_TTL,
    CameraLightingReceipt,
    camera_lighting_receipt,
    camera_lighting_view,
)
from halo_server.ring.client import RingClient


class Camera:
    family = "stickup_cams"
    kind = "future-camera-kind"

    def __init__(self, identifier="42", *, duration=60, light="off", capable=True):
        self.id = identifier
        self.capable = capable
        self._attrs = {
            "led_status": light,
            "settings": {
                "floodlight_settings": {
                    "duration": duration,
                    "always_on": False,
                    "always_on_duration": 0,
                    "always_on_duration_secs": 0,
                }
            },
        }

    def has_capability(self, name):
        return self.capable and name == "light"

    def async_set_light(self, *_):
        pytest.fail("Projection must not send a physical command")


def fixture(monkeypatch, *, duration=60, light="off"):
    clock = [1000.0]
    monotonic = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(lighting, "time", monotonic)
    monkeypatch.setattr(ring_client, "time", monotonic)
    camera = Camera(duration=duration, light=light)
    client = RingClient()
    devices = [camera]
    client._ring = SimpleNamespace(devices=lambda: SimpleNamespace(all_devices=devices))
    client._authenticated = True
    scope = AccountScope(str(uuid4()), 1)
    service = SimpleNamespace(
        _media_client=client,
        _media_scope=scope,
        _sessions=SimpleNamespace(active_client=client, active_scope=scope),
        _account_integrations_ready=True,
    )
    service._current_account_scope = lambda: service._sessions.active_scope
    service._account_authority_current = lambda: (
        service._media_client is service._sessions.active_client
        and service._media_scope == service._sessions.active_scope
    )
    service._find_media_device = lambda camera_id: next(
        (item for item in service._media_client.all_devices if str(item.id) == camera_id), None
    )
    client._record_inventory_receipt(0)
    return service, client, camera, devices, clock


def assert_unknown(view, reason="source_unknown"):
    assert view["fresh"] is False
    assert view["light"] is None
    assert view["motion_duration_seconds"] is None
    assert view["manual_timer_status"] == "unverified"
    assert view["activation_source"] is None
    assert view["reason"] == reason


def test_current_inventory_is_minimal_read_only_and_never_claims_manual_policy(
    monkeypatch, tmp_path
):
    service, client, camera, _, _ = fixture(monkeypatch)
    client.refresh_devices = lambda *_: pytest.fail("A view must not refresh Ring")
    client.submit = lambda *_: pytest.fail("A view must not submit Ring work")
    before = tuple(tmp_path.iterdir())
    assert camera_lighting_view(service, "42", camera) == {
        "fresh": True,
        "observed_age_seconds": 0.0,
        "light": False,
        "motion_duration_seconds": 60,
        "manual_timer_status": "unverified",
        "activation_source": None,
        "reason": None,
    }
    assert tuple(tmp_path.iterdir()) == before
    row = client._camera_lighting_receipt.rows[0]
    assert row.always_on is False and row.always_on_duration == row.always_on_duration_secs == 0


def test_receipt_does_not_follow_mutable_wrappers_or_nested_settings(monkeypatch):
    service, client, camera, _, _ = fixture(monkeypatch)
    receipt = client._camera_lighting_receipt
    camera._attrs["led_status"] = "on"
    camera._attrs["settings"]["floodlight_settings"]["duration"] = 900
    camera._attrs["settings"]["floodlight_settings"]["always_on"] = True
    with pytest.raises(FrozenInstanceError):
        receipt.rows[0].motion_duration_seconds = 900
    assert camera_lighting_view(service, "42", camera)["motion_duration_seconds"] == 60
    assert camera_lighting_view(service, "42", camera)["light"] is False
    assert receipt.rows[0].always_on is False


@pytest.mark.parametrize(
    "duration", [None, True, False, 0, 29, 901, 60.0, 60.5, "60", float("nan"), float("inf")]
)
def test_malformed_motion_duration_does_not_coerce_or_imply_unlimited(monkeypatch, duration):
    service, _, camera, _, _ = fixture(monkeypatch, duration=duration)
    view = camera_lighting_view(service, "42", camera)
    assert view["fresh"] is True and view["light"] is False
    assert view["motion_duration_seconds"] is None
    assert view["reason"] == "source_unknown" and view["manual_timer_status"] == "unverified"


@pytest.mark.parametrize("duration", [30, 60, 900])
def test_finite_motion_duration_has_no_new_camera_kind_allowlist(monkeypatch, duration):
    service, _, camera, _, _ = fixture(monkeypatch, duration=duration)
    assert camera_lighting_view(service, "42", camera)["motion_duration_seconds"] == duration


@pytest.mark.parametrize("status", [True, False, None, 1, "ON", "unknown", ""])
def test_light_only_accepts_explicit_native_on_off(monkeypatch, status):
    service, _, camera, _, _ = fixture(monkeypatch, light=status)
    view = camera_lighting_view(service, "42", camera)
    assert view["fresh"] and view["light"] is None and view["reason"] == "source_unknown"
    assert view["motion_duration_seconds"] == 60


@pytest.mark.parametrize(
    "alternate,expected",
    [
        ({"duration": 60}, 60),
        ({"duration": 30}, None),
        ({"duration": True}, None),
        ({"duration": None}, None),
        (None, None),
        ({}, 60),
    ],
)
def test_conflicting_or_malformed_duplicate_surface_is_unknown(monkeypatch, alternate, expected):
    service, client, camera, _, _ = fixture(monkeypatch)
    camera._attrs["settings"]["light_settings"] = alternate
    client._record_inventory_receipt(0)
    assert camera_lighting_view(service, "42", camera)["motion_duration_seconds"] == expected


def test_alternate_surface_cannot_rescue_absent_floodlight_contract(monkeypatch):
    service, client, camera, _, _ = fixture(monkeypatch)
    camera._attrs["settings"] = {"light_settings": {"duration": 60}}
    client._record_inventory_receipt(0)
    assert camera_lighting_view(service, "42", camera)["motion_duration_seconds"] is None


def test_duplicate_device_identifier_removal_and_replacement_are_unknown(monkeypatch):
    service, client, camera, devices, _ = fixture(monkeypatch)
    replacement = Camera()
    devices.append(replacement)
    client._record_inventory_receipt(0)
    assert client._camera_lighting_receipt.rows == ()
    assert_unknown(camera_lighting_view(service, "42", camera))
    devices[:] = [camera]
    client._record_inventory_receipt(0)
    devices[:] = [replacement]
    assert_unknown(camera_lighting_view(service, "42", camera))
    assert_unknown(camera_lighting_view(service, "42", replacement))
    devices.clear()
    assert_unknown(camera_lighting_view(service, "42", camera))


@pytest.mark.parametrize(
    "change", ["client", "scope", "authentication", "readiness", "capability", "family"]
)
def test_current_account_and_device_authority_is_required(monkeypatch, change):
    service, client, camera, _, _ = fixture(monkeypatch)
    if change == "client":
        service._sessions.active_client = object()
    elif change == "scope":
        service._sessions.active_scope = AccountScope(service._media_scope.profile_id, 2)
    elif change == "authentication":
        client._authenticated = False
    elif change == "readiness":
        service._account_integrations_ready = False
    elif change == "capability":
        camera.capable = False
    elif change == "family":
        camera.family = "chimes"
    assert_unknown(camera_lighting_view(service, "42", camera))


def test_expired_future_and_absent_receipts_never_read_as_current(monkeypatch):
    service, client, camera, _, clock = fixture(monkeypatch)
    clock[0] += LIGHTING_STATE_TTL
    assert camera_lighting_view(service, "42", camera)["fresh"]
    clock[0] += 0.001
    view = camera_lighting_view(service, "42", camera)
    assert_unknown(view, "source_stale")
    assert view["observed_age_seconds"] > LIGHTING_STATE_TTL
    clock[0] = 999.0
    assert_unknown(camera_lighting_view(service, "42", camera))
    client._camera_lighting_receipt = None
    assert_unknown(camera_lighting_view(service, "42", camera))


def test_mutation_invalidates_every_light_field_without_copying_the_target(monkeypatch):
    service, client, camera, _, _ = fixture(monkeypatch)
    token = client.lighting_reservation.try_acquire()
    assert client.lighting_reservation.mark_sent(token)
    client.lighting_reservation.release(token)
    assert_unknown(camera_lighting_view(service, "42", camera), "lighting_changed")
    client._record_inventory_receipt(1)
    assert camera_lighting_view(service, "42", camera)["light"] is False


@pytest.mark.asyncio
async def test_only_real_successful_gets_replace_receipt_and_crossed_writes_stay_invalid(
    monkeypatch,
):
    service, client, camera, _, clock = fixture(monkeypatch)
    first = client._camera_lighting_receipt
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def update(_ring):
        calls.append(True)
        entered.set()
        await release.wait()

    client._async_update_inventory = update
    clock[0] += 1
    await client._async_refresh_devices(None, 8)
    assert calls == [] and client._camera_lighting_receipt is first
    task = asyncio.create_task(client._async_refresh_devices(None, -1))
    await entered.wait()
    token = client.lighting_reservation.try_acquire()
    assert client.lighting_reservation.mark_sent(token)
    client.lighting_reservation.release(token)
    release.set()
    await task
    crossed = client._camera_lighting_receipt
    assert crossed.received_at == clock[0] and crossed.mutation_revision == 0
    assert_unknown(camera_lighting_view(service, "42", camera), "lighting_changed")
    clock[0] += 1
    await client._async_refresh_devices(None, -1)
    confirmed = client._camera_lighting_receipt
    assert confirmed.mutation_revision == 1
    assert camera_lighting_view(service, "42", camera)["fresh"]

    async def fail(_ring):
        camera._attrs["led_status"] = "on"
        raise RuntimeError("Synthetic failed inventory GET")

    client._async_update_inventory = fail
    with pytest.raises(RuntimeError):
        await client._async_refresh_devices(None, -1)
    assert client._camera_lighting_receipt is confirmed
    assert camera_lighting_view(service, "42", camera)["light"] is False
    client._detach_session()
    assert client._camera_lighting_receipt is None
    assert_unknown(camera_lighting_view(service, "42", camera))


@pytest.mark.parametrize(
    "field,value",
    [
        ("received_at", float("nan")),
        ("received_at", True),
        ("mutation_revision", True),
        ("mutation_revision", -1),
        ("rows", ({"camera_id": "42"},)),
    ],
)
def test_malformed_receipt_fails_closed(monkeypatch, field, value):
    service, client, camera, _, _ = fixture(monkeypatch)
    client._camera_lighting_receipt = replace(client._camera_lighting_receipt, **{field: value})
    assert_unknown(camera_lighting_view(service, "42", camera))


def test_oversized_inventory_is_not_partially_authoritative():
    devices = [Camera(str(index)) for index in range(lighting.MAX_LIGHTING_CAMERAS + 1)]
    receipt = camera_lighting_receipt(devices, received_at=1000.0, mutation_revision=0)
    assert isinstance(receipt, CameraLightingReceipt) and receipt.rows == ()


def test_mutation_during_projection_is_rechecked_before_return(monkeypatch):
    service, client, camera, _, _ = fixture(monkeypatch)
    calls = []

    def find(_camera_id):
        calls.append(True)
        if len(calls) == 2:
            token = client.lighting_reservation.try_acquire()
            assert client.lighting_reservation.mark_sent(token)
            client.lighting_reservation.release(token)
        return camera

    service._find_media_device = find
    assert_unknown(camera_lighting_view(service, "42", camera), "lighting_changed")
    assert len(calls) == 2


def test_unqualified_raw_manual_fields_cannot_hide_valid_light_evidence(monkeypatch):
    service, client, camera, _, _ = fixture(monkeypatch, light="on")
    native = camera._attrs["settings"]["floodlight_settings"]
    native.update(always_on=1, always_on_duration=10**1000, always_on_duration_secs="0")
    client._record_inventory_receipt(0)
    row = client._camera_lighting_receipt.rows[0]
    assert row.always_on is row.always_on_duration is row.always_on_duration_secs is None
    view = camera_lighting_view(service, "42", camera)
    assert view["light"] is True and view["motion_duration_seconds"] == 60
    assert view["manual_timer_status"] == "unverified" and view["activation_source"] is None


@pytest.mark.parametrize("target,valid", [("42", False), ("43", True), (None, False)])
def test_projection_only_rejects_target_or_global_mutation(monkeypatch, target, valid):
    service, client, camera, _, _ = fixture(monkeypatch)
    reservation = client.lighting_reservation
    token = reservation.try_acquire(target)
    assert reservation.mark_sent(token)
    reservation.acknowledge(token)
    reservation.release(token)
    view = camera_lighting_view(service, "42", camera)
    if valid:
        assert view["fresh"] and view["light"] is False
        assert view["motion_duration_seconds"] == 60
    else:
        assert_unknown(view, "lighting_changed")


def test_retired_reservation_invalidates_even_a_matching_revision(monkeypatch):
    service, client, camera, _, _ = fixture(monkeypatch)
    client.lighting_reservation.retire()
    assert_unknown(camera_lighting_view(service, "42", camera), "lighting_changed")
