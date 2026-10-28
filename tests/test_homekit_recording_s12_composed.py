"""Actual HAP encrypted HDS -> private IPC/bulk -> scoped warm native recording."""

import asyncio
import io
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import av
import pytest
from test_homekit_bridge import ADAPTER, NODE
from test_homekit_broker import setup_broker
from test_homekit_recording_composed import Resource, encoded_audio, encoded_video

from halo_server.homekit.broker import RecordingEvent
from halo_server.homekit.models import MediaScope
from halo_server.homekit.recording_settings import (
    CameraRecordingSettings,
    RecordingPreferences,
    RecordingSettingsStore,
    configuration_from_wire,
)
from halo_server.homekit.runtime import HomeKitRuntime
from halo_server.media.buffer import EncodedPacket, MediaStreamKind
from halo_server.media.homekit_recording import HomeKitRecordingManager
from halo_server.media.mp4 import inspect_fragment, inspect_init
from halo_server.media.opus import OriginalOpusBuffer
from halo_server.media.recording import _OWNED
from halo_server.media.recording_capture import RecordingCaptureManager, RecordingSourceBuffer
from halo_server.media.recording_policy import RecordingAuthority, RecordingPolicyOwner

SCENARIO = Path(__file__).with_name("fixtures").joinpath("homekit-recording-s12.mjs").read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["video", "audio", "shutdown", "privacy"])
async def test_native_warm_recording_through_real_HAP_HDS_and_private_channels(tmp_path, mode):
    audio = mode == "audio"
    broker, old_boot, _, service, client, _ = await setup_broker(tmp_path)
    await broker.retired(old_boot)
    loop = asyncio.get_running_loop()
    source, original = RecordingSourceBuffer(), OriginalOpusBuffer()
    video, sps, pps = encoded_video()
    opus = encoded_audio() if audio else []
    resources, opened_configurations = [], []

    async def acquire(scope, *, admission, warm=False):
        assert admission()
        resource = Resource(source, original, sps, pps, loop.time())
        if resources:
            resource.video_configuration_callback = resources[0].video_configuration_callback
        resources.append(resource)
        return resource

    broker.backend.acquire_recording = acquire
    captures = RecordingCaptureManager(service.homekit)
    manager = HomeKitRecordingManager(service.homekit, capture_manager=captures)
    await manager.qualify()
    assert manager.ready
    actual_open = manager.open

    async def observe_open(*args, **kwargs):
        opened_configurations.append(args[3])
        return await actual_open(*args, **kwargs)

    manager.open = observe_open
    scope = broker.backend.inventory().cameras[0].scope
    accessory = str(uuid4())
    store = RecordingSettingsStore(tmp_path / "recording.json")
    saved = store.update(
        lambda value: replace(
            value,
            entries=(CameraRecordingSettings(scope.profile_id, "42", accessory, owner_warm=True),),
        )
    )
    assert saved.revision == 1

    def authority(scope):
        row = store.load().camera(scope.profile_id, scope.camera_id)
        return RecordingAuthority(
            row.preferences.active,
            row.preferences.audio_active,
            row.preferences.configuration,
            row.owner_warm,
            True,
        )

    owner = RecordingPolicyOwner(service.homekit, captures, authority)
    broker.recording_manager, broker.recording_policy = manager, owner
    broker._recording_authority = authority
    event = [None]
    broker._recording_event = lambda scope: event[0]
    prepared, outcome, stalled = loop.create_future(), loop.create_future(), loop.create_future()

    trace = []

    async def request(message):
        if message["kind"] == "recording_settings_update":
            data = message["payload"]
            service.homekit.validate_selected_scope(broker.boot_id, MediaScope(**data["scope"]))
            prefs = RecordingPreferences(
                data["active"], data["audio_active"], configuration_from_wire(data["configuration"])
            )
            saved = store.set_preferences(
                scope.profile_id,
                "42",
                accessory,
                prefs,
                expected_revision=data["expected_revision"],
            )
            await owner.refresh(broker.boot_id)
            return {
                "scope": data["scope"],
                "accepted": True,
                "code": "ok",
                "revision": saved.revision,
            }
        payload = await broker.request(message)
        trace.append(
            {
                "kind": message["kind"],
                "sequence": message["payload"].get("sequence"),
                "status": payload.get("status"),
                "code": payload.get("code"),
            }
        )
        if message["kind"] == "recording_next" and payload.get("status") == "rejected":
            job = broker._recording_jobs.get(message["payload"]["job_id"])
            delivery = job.delivery if job is not None else None
            if delivery is not None:
                outgoing = delivery.prepared._state
                trace[-1]["prior_delivery"] = {
                    "retirement_complete": delivery.prepared.retirement_complete,
                    "normal_result": outgoing.normal_result,
                    "wire_done": outgoing.wire_done.done(),
                    "source_closed": outgoing.source_closed,
                    "cleanup_failed": outgoing.cleanup_failed,
                }
        (tmp_path / "rpc.json").write_text(json.dumps(trace, indent=2))
        if message["kind"] == "policy_update":
            await owner.refresh(broker.boot_id)
        return payload

    async def ready(boot):
        await broker.ready(boot)
        await owner.ready(boot)

    async def retired(boot):
        service.homekit.fence()
        await owner.retired(boot)
        await manager.retired(boot)
        await broker.retired(boot)

    async def notice(message):
        if message["kind"] != "status":
            await broker.notice(message)
            return
        reason = message["payload"]["reason"]
        if reason == "settings-ready" and not prepared.done():
            prepared.set_result(None)
        elif reason == "stalled" and not stalled.done():
            stalled.set_result(None)
        elif not outcome.done():
            outcome.set_result(reason)

    root = Path(__file__).parents[1]
    script = SCENARIO
    for name, value in {
        "__ROOT__": ADAPTER.as_uri(),
        "__HAP__": (
            root / "homekit-adapter/node_modules/@homebridge/hap-nodejs/dist/index.js"
        ).as_uri(),
        "__GUARD__": str(root / "homekit-adapter/test/network-guard.cjs"),
        "__NETWORK__": str(tmp_path / "network.json"),
        "__MODE__": mode,
        "__OUTPUT__": str(tmp_path),
    }.items():
        script = script.replace(name, json.dumps(value))
    entry = tmp_path / "recording.mjs"
    entry.write_text(script)
    runtime = HomeKitRuntime(
        NODE,
        entry,
        build_id="native-recording-hds-fixture",
        on_ready=ready,
        on_request=request,
        on_replied=broker.replied,
        on_notice=notice,
        on_retired=retired,
        max_restarts=0,
    )
    broker.runtime = runtime
    try:
        await asyncio.wait_for(runtime.start(), 4)
        await asyncio.wait_for(prepared, 4)
        async with asyncio.timeout(3):
            while not resources:
                await asyncio.sleep(0.001)
        base, audio_cursor = loop.time() + 0.01, 0
        for index, packet in enumerate(video):
            receipt = base + index / 15
            await asyncio.sleep(max(0, receipt - loop.time()))
            source.append(
                EncodedPacket(
                    index + 1,
                    MediaStreamKind.VIDEO,
                    bytes(packet),
                    index * 6000,
                    receipt,
                    capture_epoch=1,
                )
            )
            if audio:
                while audio_cursor < len(opus) and audio_cursor / 50 <= index / 15:
                    original.append_audio(
                        opus[audio_cursor], audio_cursor * 960, received_at=base + audio_cursor / 50
                    )
                    audio_cursor += 1
            if index == 75:
                current = service.homekit.inventory().cameras[0].scope
                async with asyncio.timeout(2):
                    while not owner.status(current).history_ready:
                        await asyncio.sleep(0.001)
                status = owner.status(current)
                assert status.video_seconds >= 4 and (not audio or status.audio_seconds >= 4)
                now = loop.time()
                event[0] = RecordingEvent(1, now, now + 0.01)
                await runtime.send("status", {"state": "ready", "reason": "warm-ready"})
            if mode in {"shutdown", "privacy"} and stalled.done():
                break
            if outcome.done() and outcome.result() != "done":
                pytest.fail(outcome.result())
        if mode == "shutdown":
            completed, _ = await asyncio.wait(
                (stalled, outcome), timeout=4, return_when=asyncio.FIRST_COMPLETED
            )
            assert stalled in completed, outcome.result() if outcome.done() else "no stalled media"
        else:
            assert await asyncio.wait_for(outcome, 8) == "done"
        process = runtime._process
        await asyncio.wait_for(runtime.stop(), 8)
        assert process.returncode == 0
        assert json.loads((tmp_path / "network.json").read_text())["attempts"] == []
        assert opened_configurations and opened_configurations[0].video.max_bit_rate == 2000
        assert opened_configurations[0].i_frame_interval_ms == 1000
        assert (opened_configurations[0].audio is not None) == audio
        assert (
            not manager.pending_resources
            and not captures.pending_resources
            and not broker.recording_resources
        )
        assert not service.homekit.pending_resources and all(
            resource.closed for resource in resources
        )
        assert not _OWNED
        if mode in {"video", "audio"}:
            packets = [path.read_bytes() for path in sorted(tmp_path.glob("packet-*.mp4"))]
            assert len(packets) >= 3
            movie = inspect_init(packets[0], audio=audio)
            for fragment in packets[1:]:
                assert 0 < inspect_fragment(fragment, movie).duration_seconds <= 4
                with av.open(io.BytesIO(packets[0] + fragment)) as container:
                    assert bool(container.streams.audio) == audio
                    assert list(container.decode(video=0))
            with av.open(io.BytesIO(b"".join(packets))) as container:
                if audio:
                    assert list(container.decode(audio=0))
        if mode == "privacy":
            assert store.load().camera(scope.profile_id, "42").preferences.active is False
    finally:
        await runtime.stop()
        await owner.close()
        await manager.close()
        await service.homekit.close()
