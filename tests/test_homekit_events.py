"""Private source-event freshness, state restoration and retirement ownership."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from halo_server.api.events import EventJournal
from halo_server.homekit.events import EventPump
from halo_server.homekit.models import (
    CameraCapabilities,
    HomeKitOperationError,
    MediaScope,
    NativeCamera,
    NativeInventory,
)
from halo_server.incidents.coordinator import IncidentCoordinator, IncidentPhase


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


class Controller:
    def __init__(self):
        self.profile = str(uuid4())
        self.camera = NativeCamera(
            MediaScope(self.profile, "front-door", 1, 1, 1),
            "Front door",
            "doorbots",
            "doorbell",
            "model",
            CameraCapabilities(),
        )
        self.complete = True

    def inventory(self):
        return NativeInventory(1, (self.camera,), self.complete, self.profile, 1)


class Sink:
    def __init__(self):
        self.messages = []
        self.block_kind = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, kind, payload, *, admission):
        if kind == self.block_kind:
            self.entered.set()
            await self.release.wait()
        if not admission():
            raise RuntimeError("delivery admission was retired")
        self.messages.append((kind, deepcopy(payload)))


async def wait_for(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


def fixture(*, history=1024):
    journal, controller, sink, clock = (
        EventJournal(max_events=history),
        Controller(),
        Sink(),
        Clock(),
    )
    coordinator = IncidentCoordinator(clock=clock)
    source = ["connected"]
    pump = EventPump(
        journal,
        controller,
        sink.send,
        incidents=lambda: {controller.camera.scope.camera_id: coordinator.snapshot()},
        source_state=lambda: source[0],
        clock=clock,
    )
    boot = str(uuid4())
    return pump, boot, journal, controller, sink, clock, coordinator, source


async def synchronize(pump, boot, controller, *, sequence=0):
    return await pump.sync(
        boot,
        {
            "scope": {"profile_id": controller.profile, "account_generation": 1},
            "epoch": None,
            "sequence": sequence,
        },
    )


@pytest.mark.asyncio
async def test_initial_state_restores_motion_without_replaying_old_doorbell():
    pump, boot, journal, controller, sink, _, coordinator, _ = fixture()
    coordinator.on_motion("before-attach", expires_in=10)
    old = journal.publish("doorbell", camera_id="front-door", payload={"event_id": "old"})
    pump.start(boot)
    try:
        ack = await synchronize(pump, boot, controller)
        pump.notify(old)
        await wait_for(lambda: bool(sink.messages))
        assert ack["accepted"] and ack["sequence"] == old.sequence
        assert sink.messages == [
            (
                "event_state",
                {
                    "scope": vars(controller.camera.scope),
                    "epoch": journal.epoch,
                    "sequence": old.sequence,
                    "motion": True,
                    "ttl_ms": 10000,
                    "expires_at_ms": 10000,
                    "source_state": "connected",
                    "synchronized": True,
                },
            )
        ]
    finally:
        await pump.stop()


@pytest.mark.asyncio
async def test_source_motion_reaches_sink_while_capture_is_still_warming():
    pump, boot, journal, controller, sink, _, coordinator, _ = fixture()
    pump.start(boot)
    try:
        await synchronize(pump, boot, controller)
        await wait_for(lambda: bool(sink.messages))
        assert sink.messages[-1][1]["motion"] is None
        assert sink.messages[-1][1]["expires_at_ms"] == 0
        coordinator.on_motion("new-motion", expires_in=5)
        event = journal.publish("motion_on", camera_id="front-door", payload={})
        pump.notify(event)
        await wait_for(lambda: sink.messages[-1][1]["motion"] is True)
        assert coordinator.snapshot().phase is IncidentPhase.WARMING
        assert coordinator.snapshot().media_ready is False
    finally:
        await pump.stop()


@pytest.mark.asyncio
async def test_only_live_unique_fresh_doorbells_are_sent():
    pump, boot, journal, controller, sink, _, coordinator, _ = fixture()
    pump.start(boot)
    try:
        await synchronize(pump, boot, controller)
        event = journal.publish(
            "doorbell", camera_id="front-door", payload={"event_id": "secret-id"}
        )
        pump.notify(event)
        pump.notify(event)
        await wait_for(lambda: any(kind == "camera_event" for kind, _ in sink.messages))
        presses = [payload for kind, payload in sink.messages if kind == "camera_event"]
        assert len(presses) == 1
        assert UUID(presses[0]["event_id"])
        assert "secret-id" not in str(presses)
        assert presses[0]["sequence"] == event.sequence and presses[0]["ttl_ms"] == 2000
        assert coordinator.snapshot().motion_published is False
    finally:
        await pump.stop()


@pytest.mark.asyncio
async def test_queued_press_expires_before_transport_admission():
    pump, boot, journal, controller, sink, clock, _, _ = fixture()
    sink.block_kind = "camera_event"
    pump.start(boot)
    try:
        await synchronize(pump, boot, controller)
        event = journal.publish("doorbell", camera_id="front-door", payload={})
        pump.notify(event)
        await sink.entered.wait()
        clock.value = 3
        sink.release.set()
        await asyncio.sleep(0.01)
        assert not any(kind == "camera_event" for kind, _ in sink.messages)
        assert pump.state == "synchronized"
    finally:
        sink.release.set()
        await pump.stop()


@pytest.mark.asyncio
async def test_press_queue_is_bounded_and_drops_oldest_edges_during_burst():
    pump, boot, journal, controller, sink, _, _, _ = fixture()
    pump.start(boot)
    try:
        await synchronize(pump, boot, controller)
        for index in range(100):
            event = journal.publish("doorbell", camera_id="front-door", payload={"number": index})
            pump.notify(event)
        assert len(pump._presses) == 16 and pump.dropped_presses == 84
        await wait_for(
            lambda: len([kind for kind, _ in sink.messages if kind == "camera_event"]) == 16
        )
        sequences = [
            payload["sequence"] for kind, payload in sink.messages if kind == "camera_event"
        ]
        assert sequences == list(range(85, 101))
    finally:
        await pump.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["privacy", "device", "account"])
async def test_queued_old_scope_never_crosses_replacement_delivery_boundary(change):
    pump, boot, journal, controller, sink, _, _, _ = fixture()
    sink.block_kind = "camera_event"
    pump.start(boot)
    try:
        await synchronize(pump, boot, controller)
        event = journal.publish("doorbell", camera_id="front-door", payload={})
        pump.notify(event)
        await sink.entered.wait()
        old = controller.camera.scope
        scope = (
            replace(
                old, **{"privacy_generation" if change == "privacy" else "device_generation": 2}
            )
            if change != "account"
            else replace(old, profile_id=str(uuid4()))
        )
        if change == "account":
            controller.profile = scope.profile_id
        controller.camera = replace(controller.camera, scope=scope)
        sink.release.set()
        pump.notify()
        await wait_for(lambda: not pump.synchronized)
        assert not any(kind == "camera_event" for kind, _ in sink.messages)
    finally:
        sink.release.set()
        await pump.stop()


@pytest.mark.asyncio
async def test_history_gap_requires_state_sync_and_never_replays_buttons():
    pump, boot, journal, controller, sink, _, _, _ = fixture(history=2)
    pump.start(boot)
    try:
        await synchronize(pump, boot, controller)
        for _ in range(3):
            journal.publish("doorbell", camera_id="front-door", payload={})
        pump.notify()  # Missing live notifications may not be reconstructed as edges.
        await wait_for(lambda: not pump.synchronized)
        ack = await synchronize(pump, boot, controller, sequence=0)
        assert ack["sequence"] == 3 and ack["accepted"]
        await wait_for(lambda: bool(sink.messages) and sink.messages[-1][1]["synchronized"])
        assert not any(kind == "camera_event" for kind, _ in sink.messages)
    finally:
        await pump.stop()


@pytest.mark.asyncio
async def test_epoch_reset_invalidates_state_until_resync_without_button_replay():
    pump, boot, journal, controller, sink, _, _, _ = fixture()
    pump.start(boot)
    try:
        old = (await synchronize(pump, boot, controller))["epoch"]
        journal.reset()
        pump.notify()
        await wait_for(lambda: not pump.synchronized)
        assert journal.epoch != old
        ack = await synchronize(pump, boot, controller)
        assert ack["epoch"] == journal.epoch
        await wait_for(lambda: bool(sink.messages) and sink.messages[-1][1]["synchronized"])
        assert not any(kind == "camera_event" for kind, _ in sink.messages)
    finally:
        await pump.stop()


@pytest.mark.asyncio
async def test_motion_expiry_and_source_health_are_independent_of_camera_reachability(monkeypatch):
    monkeypatch.setattr("halo_server.homekit.events.POLL_SECONDS", 0.005)
    pump, boot, _, controller, sink, clock, coordinator, source = fixture()
    coordinator.on_motion("motion", expires_in=1)
    pump.start(boot)
    try:
        await synchronize(pump, boot, controller)
        await wait_for(lambda: bool(sink.messages))
        assert sink.messages[-1][1]["motion"] is True
        source[0] = "offline"
        pump.notify()
        await wait_for(lambda: sink.messages[-1][1]["source_state"] == "offline")
        assert sink.messages[-1][1]["motion"] is True
        clock.value = 2
        await wait_for(lambda: sink.messages[-1][1]["motion"] is None)
        source[0] = "connected"
        pump.notify()
        await wait_for(lambda: sink.messages[-1][1]["motion"] is False)
        assert all("connection_state" not in payload for _, payload in sink.messages)
    finally:
        await pump.stop()


@pytest.mark.asyncio
async def test_distinct_motion_extends_state_ttl_even_without_a_new_journal_transition():
    pump, boot, _, controller, sink, clock, coordinator, _ = fixture()
    coordinator.on_motion("one", expires_in=5)
    pump.start(boot)
    try:
        await synchronize(pump, boot, controller)
        await wait_for(lambda: bool(sink.messages))
        assert sink.messages[-1][1]["ttl_ms"] == 5000
        assert sink.messages[-1][1]["expires_at_ms"] == 5000
        clock.value = 1
        coordinator.on_motion("two", expires_in=10)
        pump.notify()
        await wait_for(lambda: sink.messages[-1][1]["ttl_ms"] == 10000)
        assert sink.messages[-1][1]["expires_at_ms"] == 11000
        assert sink.messages[0][1]["sequence"] == sink.messages[-1][1]["sequence"] == 0
    finally:
        await pump.stop()


@pytest.mark.asyncio
async def test_wrong_account_sync_echoes_request_scope_and_never_grants_authority():
    pump, boot, journal, _, sink, _, _, _ = fixture()
    wrong = {"profile_id": str(uuid4()), "account_generation": 1}
    pump.start(boot)
    try:
        ack = await pump.sync(boot, {"scope": wrong, "epoch": None, "sequence": 0})
        assert ack == {
            "scope": wrong,
            "epoch": journal.epoch,
            "sequence": 0,
            "accepted": False,
            "code": "stale-scope",
        }
        assert not pump.synchronized
        assert all(kind != "camera_event" for kind, _ in sink.messages)
    finally:
        await pump.stop()


@pytest.mark.asyncio
async def test_stop_keeps_boot_barrier_until_noncooperative_send_compensation_finishes(monkeypatch):
    monkeypatch.setattr("halo_server.homekit.events.STOP_SECONDS", 0.01)
    pump, boot, _, controller, _, _, _, _ = fixture()
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def send(_kind, _payload, *, admission):
        assert admission()
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            raise

    pump._send = send
    pump.start(boot)
    try:
        await synchronize(pump, boot, controller)
        await entered.wait()
        with pytest.raises(HomeKitOperationError, match="unavailable"):
            await pump.stop()
        assert cancelled.is_set() and pump.boot_id == boot
        with pytest.raises(HomeKitOperationError):
            pump.start(str(uuid4()))
        with pytest.raises(HomeKitOperationError):
            await pump.stop()
        release.set()
        await pump.stop()
        assert pump.boot_id is None and pump.state == "stopped"
    finally:
        release.set()
        await pump.stop()


@pytest.mark.asyncio
async def test_queued_motion_cannot_be_delivered_after_its_source_expiry():
    pump, boot, _, controller, sink, clock, coordinator, _ = fixture()
    coordinator.on_motion("motion", expires_in=1)
    sink.block_kind = "event_state"
    pump.start(boot)
    try:
        await synchronize(pump, boot, controller)
        await sink.entered.wait()
        clock.value = 2
        sink.release.set()
        await wait_for(lambda: bool(sink.messages))
        assert all(
            payload["motion"] is not True
            for kind, payload in sink.messages
            if kind == "event_state"
        )
    finally:
        sink.release.set()
        await pump.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["event_state", "camera_event"])
async def test_relative_event_lifetime_is_recomputed_after_transport_queueing(kind):
    pump, boot, journal, controller, sink, clock, coordinator, _ = fixture()
    coordinator.on_motion("motion", expires_in=1)
    sink.block_kind = kind
    pump.start(boot)
    try:
        await synchronize(pump, boot, controller)
        if kind == "camera_event":
            pump.notify(journal.publish("doorbell", camera_id="front-door", payload={}))
        await sink.entered.wait()
        clock.value = 0.5
        sink.release.set()
        await wait_for(lambda: any(item == kind for item, _ in sink.messages))
        payload = next(payload for item, payload in sink.messages if item == kind)
        assert payload["ttl_ms"] == (500 if kind == "event_state" else 1500)
        assert payload["expires_at_ms"] == (1000 if kind == "event_state" else 2000)
    finally:
        sink.release.set()
        await pump.stop()
