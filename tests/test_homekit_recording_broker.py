"""Frozen recording admission and per-packet private transfer ownership."""

import asyncio
from dataclasses import asdict, replace
from fractions import Fraction
from uuid import uuid4

import pytest
from test_homekit_broker import permit, request, setup_broker
from test_homekit_bulk_cancel import until

from halo_server.homekit.broker import RecordingEvent
from halo_server.homekit.bulk import BulkTransferError
from halo_server.homekit.ipc import validate_reply
from halo_server.homekit.models import HomeKitOperationError
from halo_server.media.h264 import VideoConfiguration
from halo_server.media.recording import RecordingConfiguration, RecordingPacket
from halo_server.media.recording_policy import RecordingAuthority


def wire(configuration):
    value = asdict(configuration)
    value["video"] = {
        name: value["video"][name]
        for name in ("width", "height", "fps", "profile", "level", "max_bit_rate")
    }
    return value


class Prepared:
    def __init__(self, **parameters):
        self.parameters = parameters
        self.chunks = []
        self.success = self.cancelled = False
        self.fail_cancel = False
        self.acknowledged = False

    @property
    def peer_completed(self):
        return self.acknowledged or self.success

    @property
    def retirement_complete(self):
        return (self.success or self.cancelled) and not self.fail_cancel

    async def send(self, source):
        async for chunk in source:
            if not self.parameters["valid"]():
                raise BulkTransferError("invalidated")
            self.chunks.append(bytes(chunk))
        self.success = True

    async def cancel(self, code="cancelled"):
        self.cancelled = True
        if self.fail_cancel:
            raise HomeKitOperationError("cleanup_pending")


class Channel:
    def __init__(self):
        self.handles = []
        self.busy = False

    def prepare_send(self, **parameters):
        if self.busy:
            raise BulkTransferError("busy")
        handle = Prepared(**parameters)
        self.handles.append(handle)
        return handle


class Stream:
    def __init__(self):
        self.current = True
        self.gate = None
        self.finished = False
        self.packets = [
            RecordingPacket("init", b"init" * 20000, 0),
            RecordingPacket("fragment", b"fragment" * 20000, 1, Fraction(2)),
        ]

    def is_current(self):
        return self.current

    def finish(self):
        self.finished = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.gate is not None:
            await self.gate.wait()
        if not self.packets:
            raise StopAsyncIteration
        return self.packets.pop(0)


class Manager:
    ready = True

    def __init__(self):
        self.streams = {}
        self.opens = []
        self.fail_stop = False
        self.gate = None
        self.events = []

    async def open(self, boot, scope, identity, configuration, *, event_at=None):
        self.opens.append(configuration)
        self.events.append(event_at)
        stream = self.streams[identity] = Stream()
        if self.gate is not None:
            await self.gate.wait()
        return stream

    async def stop(self, boot, scope, identity):
        stream = self.streams.get(identity)
        if stream is not None:
            stream.current = False
        if self.fail_stop:
            raise HomeKitOperationError("cleanup_pending")
        self.streams.pop(identity, None)


class Policy:
    ready = True

    def authorize_open(self, boot, scope, configuration):
        if not self.ready:
            raise HomeKitOperationError("prebuffer_unavailable")


async def configured(tmp_path):
    broker, boot, peer, _, _, _ = await setup_broker(tmp_path)
    scope = await permit(broker, boot, recording=True)
    configuration = RecordingConfiguration(VideoConfiguration(1280, 720, 15, "main", "4.0", 2000))
    state = [RecordingAuthority(True, False, configuration, True, True)]
    broker._recording_authority = lambda scope: state[0]
    manager = broker.recording_manager = Manager()
    channel = peer.bulk_channel = Channel()
    policy = broker.recording_policy = Policy()
    now = asyncio.get_running_loop().time()
    broker._recording_event = lambda scope: RecordingEvent(1, now - 0.001, now + 30)
    data = {"scope": scope, "job_id": str(uuid4()), "configuration": wire(configuration)}
    return broker, boot, state, manager, channel, policy, data


async def rpc(broker, boot, kind, data):
    envelope = request(boot, kind, data)
    payload = await broker.request(envelope)
    reply_kind = {
        "recording_open": "recording_open_result",
        "recording_next": "recording_next_result",
        "recording_stop": "recording_stop_result",
    }[kind]
    reply = envelope | {"kind": reply_kind, "payload": payload}
    validate_reply(envelope, reply)
    return envelope, reply


@pytest.mark.asyncio
async def test_frozen_recording_packets_reserve_before_ready_and_pull_one_at_a_time(tmp_path):
    broker, boot, state, manager, channel, _, data = await configured(tmp_path)
    try:
        _, opened = await rpc(broker, boot, "recording_open", data)
        assert opened["payload"]["status"] == "accepted"
        next_data = {name: data[name] for name in ("scope", "job_id")} | {"sequence": 0}
        envelope, reply = await rpc(broker, boot, "recording_next", next_data)
        assert reply["payload"]["status"] == "ready" and reply["payload"]["kind"] == "init"
        assert len(channel.handles) == 1 and not channel.handles[0].chunks
        _, busy = await rpc(broker, boot, "recording_next", next_data | {"sequence": 1})
        assert busy["payload"]["code"] == "busy"
        # A new selected codec config applies to the NEXT job, not this worker.
        state[0] = replace(
            state[0],
            configuration=replace(
                state[0].configuration,
                video=replace(state[0].configuration.video, max_bit_rate=1000),
            ),
        )
        assert broker._recording_current(broker._recording_jobs[data["job_id"]])
        await broker.replied(envelope, reply)
        await until(lambda: channel.handles[0].success)
        assert b"".join(channel.handles[0].chunks) == b"init" * 20000
        envelope, reply = await rpc(broker, boot, "recording_next", next_data | {"sequence": 1})
        assert reply["payload"]["duration_ms"] == 2000
        await broker.replied(envelope, reply)
        await until(lambda: channel.handles[1].success)
        _, eos = await rpc(broker, boot, "recording_next", next_data | {"sequence": 2})
        assert eos["payload"]["status"] == "eos" and not broker.recording_resources
        assert not manager.streams
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_cold_or_mismatched_config_is_rejected_without_waking_recording_source(tmp_path):
    broker, boot, state, manager, _, policy, data = await configured(tmp_path)
    try:
        policy.ready = False
        _, rejected = await rpc(broker, boot, "recording_open", data)
        assert rejected["payload"]["code"] == "not-ready" and not manager.opens
        policy.ready = True
        changed = dict(data["configuration"])
        changed["video"] = changed["video"] | {"max_bit_rate": 1000}
        _, rejected = await rpc(broker, boot, "recording_open", data | {"configuration": changed})
        assert rejected["payload"]["code"] == "policy-denied" and not manager.opens
        state[0] = replace(state[0], active=False)
        _, rejected = await rpc(broker, boot, "recording_open", data)
        assert rejected["payload"]["code"] == "policy-denied"
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["privacy", "source", "owner", "video-capability", "active", "audio", "boot", "stop"]
)
async def test_no_queued_packet_escapes_after_ready_when_authority_changes(tmp_path, reason):
    broker, boot, state, manager, channel, _, data = await configured(tmp_path)
    try:
        await rpc(broker, boot, "recording_open", data)
        scoped = {name: data[name] for name in ("scope", "job_id")}
        envelope, reply = await rpc(broker, boot, "recording_next", scoped | {"sequence": 0})
        prepared = channel.handles[0]
        if reason == "privacy":
            scope = broker.controller.inventory().cameras[0].scope
            broker.controller.fence_camera(boot, scope)
        elif reason == "source":
            manager.streams[data["job_id"]].current = False
        elif reason == "owner":
            state[0] = replace(state[0], owner_opt_in=False)
        elif reason == "video-capability":
            state[0] = replace(state[0], recording_supported=False)
        elif reason == "active":
            state[0] = replace(state[0], active=False)
        elif reason == "audio":
            state[0] = replace(state[0], audio_active=True)
        elif reason == "boot":
            await broker.retired(boot)
        else:
            _, stopped = await rpc(broker, boot, "recording_stop", scoped)
            assert stopped["payload"]["status"] == "stopped"
        await broker.replied(envelope, reply)
        await until(lambda: prepared.cancelled)
        assert not prepared.chunks and not broker.cleanup_failed
        await until(lambda: not broker.recording_resources)
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_stop_before_open_prevents_late_acquire_and_wrong_scope_cannot_stop_owned_job(
    tmp_path,
):
    broker, boot, _, manager, _, _, data = await configured(tmp_path)
    scoped = {name: data[name] for name in ("scope", "job_id")}
    try:
        await rpc(broker, boot, "recording_stop", scoped)
        _, rejected = await rpc(broker, boot, "recording_open", data)
        assert rejected["payload"]["code"] == "conflict" and not manager.opens
        data["job_id"] = str(uuid4())
        await rpc(broker, boot, "recording_open", data)
        wrong = data["scope"] | {"privacy_generation": data["scope"]["privacy_generation"] + 1}
        _, rejected = await rpc(
            broker, boot, "recording_stop", {"scope": wrong, "job_id": data["job_id"]}
        )
        assert rejected["payload"]["code"] == "conflict" and broker.recording_resources == 1
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_stop_fences_blocked_open_and_retains_failed_native_cleanup_until_retry(tmp_path):
    broker, boot, _, manager, _, _, data = await configured(tmp_path)
    manager.gate = asyncio.Event()
    opening = asyncio.create_task(broker.request(request(boot, "recording_open", data)))
    scoped = {name: data[name] for name in ("scope", "job_id")}
    try:
        await until(lambda: bool(manager.opens))
        manager.fail_stop = True
        _, rejected = await rpc(broker, boot, "recording_stop", scoped)
        assert rejected["payload"]["status"] == "rejected"
        assert broker.recording_resources == broker.recording_cleanup_failures == 1
        assert not manager.streams[data["job_id"]].current
        manager.fail_stop = False
        _, stopped = await rpc(broker, boot, "recording_stop", scoped)
        assert stopped["payload"]["status"] == "stopped"
        assert not broker.recording_resources and not manager.streams
    finally:
        manager.fail_stop = False
        manager.gate.set()
        await asyncio.gather(opening, return_exceptions=True)
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_bulk_retirement_failure_keeps_job_reserved_until_explicit_retry(tmp_path):
    broker, boot, _, _, channel, _, data = await configured(tmp_path)
    scoped = {name: data[name] for name in ("scope", "job_id")}
    try:
        await rpc(broker, boot, "recording_open", data)
        await rpc(broker, boot, "recording_next", scoped | {"sequence": 0})
        channel.handles[0].fail_cancel = True
        _, rejected = await rpc(broker, boot, "recording_stop", scoped)
        assert rejected["payload"]["code"] == "not-ready"
        assert broker.recording_resources == broker.recording_cleanup_failures == 1
        channel.handles[0].fail_cancel = False
        _, stopped = await rpc(broker, boot, "recording_stop", scoped)
        assert stopped["payload"]["status"] == "stopped" and not broker.recording_resources
    finally:
        channel.handles[0].fail_cancel = False
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_stop_joins_paused_packet_pull_without_deadlocking_cleanup(tmp_path):
    broker, boot, _, manager, _, _, data = await configured(tmp_path)
    scoped = {name: data[name] for name in ("scope", "job_id")}
    pulling = None
    try:
        await rpc(broker, boot, "recording_open", data)
        stream = manager.streams[data["job_id"]]
        stream.gate = asyncio.Event()
        pulling = asyncio.create_task(
            broker.request(request(boot, "recording_next", scoped | {"sequence": 0}))
        )
        await until(lambda: broker._recording_jobs[data["job_id"]].pulling is not None)
        _, stopped = await asyncio.wait_for(rpc(broker, boot, "recording_stop", scoped), 1)
        assert stopped["payload"]["status"] == "stopped"
        assert pulling.done() and not broker.recording_resources and not manager.streams
    finally:
        if pulling is not None:
            await asyncio.gather(pulling, return_exceptions=True)
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_child_policy_cannot_enable_recording_against_durable_active_or_audio_off(tmp_path):
    broker, boot, state, _, _, _, data = await configured(tmp_path)
    fields = {
        "synchronized": True,
        "streaming": False,
        "live_audio": False,
        "snapshots": False,
        "recording": True,
        "recording_audio": True,
        "talkback": False,
        "controls": False,
    }
    try:
        result = await broker.request(
            request(boot, "policy_update", {"scope": data["scope"], "policy": fields})
        )
        assert result["code"] == "policy-denied"
        state[0] = replace(state[0], active=False)
        result = await broker.request(
            request(
                boot,
                "policy_update",
                {"scope": data["scope"], "policy": fields | {"recording_audio": False}},
            )
        )
        assert result["code"] == "policy-denied"
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_delayed_homekit_open_uses_trusted_incident_start_and_unknown_event_never_wakes(
    tmp_path,
):
    broker, boot, _, manager, _, _, data = await configured(tmp_path)
    now = asyncio.get_running_loop().time()
    incident = RecordingEvent(7, now - 5, now + 10)
    try:
        broker._recording_event = lambda scope: None
        _, denied = await rpc(broker, boot, "recording_open", data)
        assert denied["payload"]["code"] == "not-ready" and not manager.opens
        broker._recording_event = lambda scope: incident
        _, opened = await rpc(broker, boot, "recording_open", data)
        assert opened["payload"]["status"] == "accepted"
        assert manager.events == [incident.event_at]  # Never the later OPEN clock.
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["quiet", "new_incident", "maximum"])
async def test_trusted_event_boundary_finishes_native_stream_then_drains_eos(
    tmp_path, monkeypatch, boundary
):
    from halo_server.homekit import broker as module

    broker, boot, _, manager, channel, _, data = await configured(tmp_path)
    scoped = {name: data[name] for name in ("scope", "job_id")}
    now = asyncio.get_running_loop().time()
    broker._recording_event = lambda scope: RecordingEvent(1, now - 1, now + 30)
    try:
        await rpc(broker, boot, "recording_open", data)
        stream = manager.streams[data["job_id"]]
        if boundary == "quiet":
            monkeypatch.setattr(module, "RECORDING_POSTROLL_SECONDS", 0)
            broker._recording_event = lambda scope: RecordingEvent(1, now - 1, now - 0.1)
        elif boundary == "new_incident":
            broker._recording_event = lambda scope: RecordingEvent(2, now, now + 30)
        else:
            monkeypatch.setattr(module, "RECORDING_EVENT_SECONDS", 0)
        await until(lambda: stream.finished)
        for sequence in range(2):
            envelope, reply = await rpc(
                broker, boot, "recording_next", scoped | {"sequence": sequence}
            )
            await broker.replied(envelope, reply)
            await until(lambda sequence=sequence: channel.handles[sequence].success)
        _, eos = await rpc(broker, boot, "recording_next", scoped | {"sequence": 2})
        assert eos["payload"]["status"] == "eos" and not broker.recording_resources
    finally:
        await broker.retired(boot)


@pytest.mark.asyncio
@pytest.mark.parametrize("privacy", [False, True])
async def test_reaper_preserves_real_manager_eos_during_native_close_but_privacy_still_fences(
    tmp_path, monkeypatch, privacy
):
    from test_homekit_recording_manager import Captures, Worker

    from halo_server.homekit import broker as module
    from halo_server.media.homekit_recording import HomeKitRecordingManager

    broker, boot, _, _, channel, _, data = await configured(tmp_path)
    scoped = {name: data[name] for name in ("scope", "job_id")}
    captures = Captures()
    entered, release = asyncio.Event(), asyncio.Event()

    async def close():
        captures.capture.retired = True
        entered.set()
        await release.wait()
        captures.capture.closed = True

    captures.capture.close = close

    class NativeWorker(Worker):
        async def feed(self, sample):
            return tuple(
                RecordingPacket(
                    p.kind,
                    p.payload,
                    p.sequence,
                    Fraction(0) if p.kind == "init" else p.duration_seconds,
                )
                for p in await super().feed(sample)
            )

        async def finish(self):
            return tuple(
                RecordingPacket(p.kind, p.payload, p.sequence, p.duration_seconds)
                for p in await super().finish()
            )

    manager = broker.recording_manager = HomeKitRecordingManager(
        broker.controller,
        capture_manager=captures,
        worker_factory=NativeWorker,
    )
    manager.ready = True
    eos = None
    try:
        await rpc(broker, boot, "recording_open", data)
        monkeypatch.setattr(module, "RECORDING_POSTROLL_SECONDS", 0)
        now = asyncio.get_running_loop().time()
        broker._recording_event = lambda scope: RecordingEvent(1, now - 1, now - 0.1)
        await until(lambda: broker._recording_jobs[data["job_id"]].finish_requested)
        for sequence in range(3):
            envelope, reply = await rpc(
                broker, boot, "recording_next", scoped | {"sequence": sequence}
            )
            await broker.replied(envelope, reply)
            await until(lambda sequence=sequence: channel.handles[sequence].success)
        eos = asyncio.create_task(rpc(broker, boot, "recording_next", scoped | {"sequence": 3}))
        await entered.wait()
        if privacy:
            broker.controller.fence_camera(boot, broker.controller.inventory().cameras[0].scope)
        await asyncio.sleep(0.15)  # Reaper overlaps the real manager's native-close join.
        assert not eos.done()
        assert broker._recording_jobs[data["job_id"]].cancelled is privacy
        release.set()
        if privacy:
            result = (await asyncio.gather(eos, return_exceptions=True))[0]
            assert isinstance(result, asyncio.CancelledError)
        else:
            _, reply = await eos
            assert reply["payload"]["status"] == "eos"
        await until(lambda: not broker.recording_resources)
    finally:
        release.set()
        if eos is not None:
            await asyncio.gather(eos, return_exceptions=True)
        await broker.retired(boot)
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("retired", [True, False])
async def test_immediate_next_joins_only_proven_previous_transfer_retirement(tmp_path, retired):
    broker, boot, _, _, channel, _, data = await configured(tmp_path)
    scoped = {name: data[name] for name in ("scope", "job_id")}
    entered, release = asyncio.Event(), asyncio.Event()
    next_packet = None
    try:
        await rpc(broker, boot, "recording_open", data)
        envelope, reply = await rpc(broker, boot, "recording_next", scoped | {"sequence": 0})
        prepared = channel.handles[0]
        original = prepared.send

        async def paused(source):
            if retired:
                await original(source)
            entered.set()
            await release.wait()
            if not retired:
                await original(source)

        prepared.send = paused
        await broker.replied(envelope, reply)
        await entered.wait()
        next_packet = asyncio.create_task(
            rpc(broker, boot, "recording_next", scoped | {"sequence": 1})
        )
        await asyncio.sleep(0.01)
        if retired:
            assert not next_packet.done() and len(channel.handles) == 1
            release.set()
            following, ready = await next_packet
            assert ready["payload"]["status"] == "ready"
            await broker.replied(following, ready)
            await until(lambda: channel.handles[1].success)
        else:
            _, busy = await next_packet
            assert busy["payload"]["code"] == "busy" and len(channel.handles) == 1
            release.set()
            await until(lambda: prepared.success)
    finally:
        release.set()
        if next_packet is not None:
            await asyncio.gather(next_packet, return_exceptions=True)
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_exact_peer_ack_allows_join_but_never_next_reservation_before_actual_source_cleanup(
    tmp_path,
):
    broker, boot, _, _, channel, _, data = await configured(tmp_path)
    scoped = {name: data[name] for name in ("scope", "job_id")}
    entered, release = asyncio.Event(), asyncio.Event()
    following = None
    try:
        await rpc(broker, boot, "recording_open", data)
        envelope, reply = await rpc(broker, boot, "recording_next", scoped | {"sequence": 0})
        prepared = channel.handles[0]
        original = prepared.send

        async def acknowledged_but_retiring(source):
            prepared.acknowledged = True
            entered.set()
            await release.wait()
            await original(source)

        prepared.send = acknowledged_but_retiring
        await broker.replied(envelope, reply)
        await entered.wait()
        assert prepared.peer_completed and not prepared.retirement_complete
        following = asyncio.create_task(
            rpc(broker, boot, "recording_next", scoped | {"sequence": 1})
        )
        await asyncio.sleep(0.01)
        assert not following.done() and len(channel.handles) == 1
        release.set()
        request_next, ready = await following
        assert ready["payload"]["status"] == "ready" and prepared.retirement_complete
        await broker.replied(request_next, ready)
        await until(lambda: channel.handles[1].success)
    finally:
        release.set()
        if following is not None:
            await asyncio.gather(following, return_exceptions=True)
        await broker.retired(boot)


@pytest.mark.asyncio
async def test_privacy_fence_before_unknown_stop_still_denies_late_open_without_acquiring(tmp_path):
    broker, boot, _, manager, _, _, data = await configured(tmp_path)
    scoped = {name: data[name] for name in ("scope", "job_id")}
    try:
        original_scope = broker.controller.inventory().cameras[0].scope
        broker.controller.fence_camera(boot, original_scope)
        _, stopped = await rpc(broker, boot, "recording_stop", scoped)
        assert stopped["payload"]["status"] == "stopped"
        _, rejected = await rpc(broker, boot, "recording_open", data)
        assert rejected["payload"]["code"] == "conflict"
        assert not manager.opens and not broker.recording_resources
    finally:
        await broker.retired(boot)
