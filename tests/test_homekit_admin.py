"""Trusted selection administration composed with real stores and adapter IPC."""

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_homekit_bridge import ADAPTER, NODE, wait_until
from test_homekit_events import fixture as event_fixture
from test_homekit_events import synchronize
from test_native_service import Camera, service_fixture
from test_service import FakeListener, FakeMediaSession

from halo_server.account_scope import AccountScope
from halo_server.api.admin import AdminProtocolError
from halo_server.configuration import ServerMode
from halo_server.homekit.bridge import AdapterResources
from halo_server.homekit.models import HomeKitOperationError
from halo_server.incidents.coordinator import IncidentCoordinator
from halo_server.ring.events import EventConnectionState, NormalizedRingEvent, RingEventType


def parameters(page, camera_ids):
    return {
        "profile_id": page["profile_id"],
        "account_generation": page["account_generation"],
        "expected_revision": page["revision"],
        "camera_ids": camera_ids,
    }


@pytest.mark.asyncio
async def test_selection_admin_is_explicit_revision_checked_and_retains_missing_identity(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    page = await service.handle_admin("homekit_list", {})
    assert page["total"] == 1 and page["cameras"][0]["selected"] is False
    identity = page["cameras"][0]
    assert identity["name"] == "HallCamera"
    result = await service.handle_admin("homekit_select", parameters(page, ["42"]))
    assert result == {"saved": True, "revision": page["revision"] + 1, "camera_ready": False}
    with pytest.raises(AdminProtocolError) as stale:
        await service.handle_admin("homekit_select", parameters(page, []))
    assert stale.value.code == "configuration_conflict"
    client.all_devices.clear()
    missing = await service.handle_admin("homekit_list", {})
    assert missing["cameras"] == [identity | {"selected": True, "available": False, "name": ""}]
    client.all_devices.append(Camera())
    restored = await service.handle_admin("homekit_list", {})
    assert restored["cameras"] == [identity | {"selected": True}]
    serialized = json.dumps(restored)
    assert "pincode" not in serialized and "account-display-only" not in serialized
    assert list(service.paths.homekit_pairing_directory.glob("*")) == []


@pytest.mark.asyncio
async def test_selection_admin_does_not_reassociate_a_replacement_profile(tmp_path):
    service, _, sessions = service_fixture(tmp_path)
    page = await service.handle_admin("homekit_list", {})
    await service.handle_admin("homekit_select", parameters(page, ["42"]))
    sessions.active_scope = AccountScope(str(uuid4()), 2)
    service._media_scope = sessions.active_scope
    new_page = await service.handle_admin("homekit_list", {})
    assert new_page["profile_id"] != page["profile_id"]
    assert new_page["cameras"][0]["selected"] is False
    assert new_page["cameras"][0]["accessory_id"] != page["cameras"][0]["accessory_id"]
    assert service._homekit_bridge.status()["selected_cameras"] == 0
    with pytest.raises(AdminProtocolError) as stale:
        await service.handle_admin("homekit_select", parameters(page, []))
    assert stale.value.code == "account_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"offset": True}, {"offset": 257}, {"limit": 33}, {"limit": 0}])
async def test_selection_admin_rejects_invalid_page_bounds(tmp_path, params):
    service, _, _ = service_fixture(tmp_path)
    with pytest.raises(AdminProtocolError) as error:
        await service.handle_admin("homekit_list", params)
    assert error.value.code == "bad_params"


@pytest.mark.asyncio
async def test_selection_admin_is_unavailable_in_compatibility_mode(tmp_path):
    service, _, _ = service_fixture(tmp_path, mode=ServerMode.COMPATIBILITY)
    for method in ("homekit_list", "homekit_select"):
        with pytest.raises(AdminProtocolError) as error:
            await service.handle_admin(method, {})
        assert error.value.code == "homekit_disabled"


@pytest.mark.asyncio
async def test_real_child_applies_trusted_selection_without_publishing_or_starting_media(tmp_path):
    service, client, sessions = service_fixture(tmp_path)
    bridge = service._homekit_bridge
    bridge._resources = AdapterResources(NODE, ADAPTER / "main.js")
    service._listener_factory = FakeListener
    service._media_session_factory = FakeMediaSession
    sessions.restore = lambda: client
    sessions.has_stored_credentials = lambda: False
    try:
        await service.start()
        await wait_until(lambda: service.status()["homekit"]["transport_ready"], timeout=25)
        page = await service.handle_admin("homekit_list", {})
        saved = await service.handle_admin("homekit_select", parameters(page, ["42"]))
        await wait_until(
            lambda: (
                bridge._config_signature is not None
                and bridge._config_signature[1] == saved["revision"]
                and len(service.homekit.inventory().cameras) == 1
                and bridge._events.synchronized
            )
        )
        status = bridge.status()
        assert status["selected_cameras"] == 1
        assert status["camera_ready"] is False and status["reason"] == "recording_disabled"
        assert bridge._runtime._pairing_owner is not None
        assert list(service.paths.homekit_pairing_directory.iterdir()) == []
        assert not service._media_sessions and client.submissions == 0
        deselect = await service.handle_admin("homekit_select", parameters(page | saved, []))
        await wait_until(
            lambda: (
                bridge._config_signature is not None
                and bridge._config_signature[1] == deselect["revision"]
                and not service.homekit.inventory().cameras
            )
        )
        assert service.paths.admin_socket.is_socket()
    finally:
        await service.stop()
    assert service._shutdown_complete and bridge._runtime.pid is None
    assert bridge._runtime._pairing_owner is None


@pytest.mark.asyncio
async def test_rejected_configuration_retires_owned_runtime(tmp_path):
    service, _, _ = service_fixture(tmp_path)
    bridge = service._homekit_bridge

    class RejectingRuntime:
        def __init__(self, *_args, **callbacks):
            self.callbacks = callbacks
            self.boot_id = str(uuid4())
            self.state, self.reason, self.restart_count = "ready", None, 0
            self.stopped = False

        async def start(self):
            await self.callbacks["on_ready"](self.boot_id)

        async def send(self, *_args):
            pass

        async def request(self, *_args, **_kwargs):
            return {"accepted": False, "code": "not-ready"}

        async def stop(self):
            await self.callbacks["on_retired"](self.boot_id)
            self.stopped = True
            self.boot_id = None

    bridge._runtime_factory = RejectingRuntime
    bridge._resources = AdapterResources(NODE, ADAPTER / "main.js")
    bridge.start()
    await asyncio.wait_for(asyncio.shield(bridge._task), 8)
    assert bridge.state == "degraded" and bridge.reason == "adapter_control_failed"
    assert bridge._runtime.stopped and service.homekit.boot_id is None
    assert bridge._events.boot_id is None
    with pytest.raises(HomeKitOperationError):
        await service.homekit.set_control(
            str(uuid4()), service._homekit_backend.inventory().cameras[0].scope, "light", True
        )
    await bridge.stop()


def ring_event(kind=RingEventType.MOTION, *, received_at=100.0):
    return NormalizedRingEvent(
        event_id=str(uuid4()),
        camera_id="42",
        camera_name="Hall",
        camera_kind="stickup_cam_mini",
        type=kind,
        source_kind=kind.value,
        source_state="active",
        occurred_at=0,
        received_at=received_at,
        source_expires_in=180,
        is_update=False,
    )


def test_listener_queue_keeps_original_wrapper_and_drops_unknown_cameras(tmp_path):
    service, client, _ = service_fixture(tmp_path)
    service._accept_events = True
    queued = []
    service._main_loop = SimpleNamespace(
        is_closed=lambda: False,
        call_soon_threadsafe=lambda callback, *args: queued.append((callback, args)),
    )
    service._receive_event(service._listener_generation, ring_event())
    assert len(queued) == 1
    client.all_devices[0] = Camera()
    callback, args = queued.pop()
    callback(*args)
    assert not service._incidents
    service._receive_event(service._listener_generation, replace(ring_event(), camera_id="unknown"))
    assert not queued and not service._incidents


def test_source_motion_lifetime_includes_delay_before_daemon_callback():
    coordinator = IncidentCoordinator(clock=lambda: 125.0)
    coordinator.on_motion("new", expires_in=180, received_at=100.0)
    assert coordinator.snapshot().motion_deadline == 130.0
    expired = IncidentCoordinator(clock=lambda: 135.0)
    assert expired.on_motion("old", received_at=100.0) == []
    assert not expired.snapshot().motion_published and expired.snapshot().phase == "idle"


@pytest.mark.asyncio
async def test_doorbell_freshness_includes_delay_before_journal_publication():
    pump, boot, journal, controller, sink, clock, _, _ = event_fixture()
    pump.start(boot)
    try:
        await synchronize(pump, boot, controller)
        clock.value = 10.0
        event = journal.publish("doorbell", camera_id="front-door", payload={})
        pump.notify(event, received_at=7.0)
        await asyncio.sleep(0.01)
        assert all(kind != "camera_event" for kind, _ in sink.messages)
        fresh = journal.publish("doorbell", camera_id="front-door", payload={})
        pump.notify(fresh, received_at=9.0)
        await wait_until(lambda: any(kind == "camera_event" for kind, _ in sink.messages))
        payload = next(payload for kind, payload in sink.messages if kind == "camera_event")
        assert payload["expires_at_ms"] == 11000 and payload["ttl_ms"] == 1000
    finally:
        await pump.stop()


@pytest.mark.asyncio
async def test_listener_passes_original_receipt_before_any_capture_work(tmp_path):
    service, _, _ = service_fixture(tmp_path)
    service._accept_events = True
    service._event_state = EventConnectionState.CONNECTED
    observed = []
    bridge = service._homekit_bridge
    bridge.notify_event = lambda event, *, received_at: observed.append((event.type, received_at))
    service._spawn_media_actions = lambda *_args: observed.append(("capture", None))
    event = ring_event(received_at=time.monotonic() - 1)
    service._process_listener_event(
        service._listener_generation, event, service._find_media_device("42")
    )
    assert observed == [("motion_on", event.received_at), ("capture", None)]
