"""Actual pinned signal dispatch, synthetic data only; no account or media."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from ring_doorbell.doorbot import RingDoorBell
from ring_doorbell.webrtcstream import RingWebRtcStream
from test_ring_media import FakeDevice, install_media_fakes

from halo_server.media import ring_media, ring_stream_info
from halo_server.media.ring_stream_info import RingStreamInfo


def fixture():
    stream = RingWebRtcStream(None, 123)
    stream.session_id = "synthetic-session"
    device = SimpleNamespace(_webrtc_streams={"attempt": stream})
    current = SimpleNamespace(value=True)
    now = [100.0]
    owner = RingStreamInfo(device, "attempt", current=lambda: current.value, clock=lambda: now[0])
    return stream, device, current, now, owner


def info(**overrides):
    body = {
        "doorbot_id": 123,
        "session_id": "synthetic-session",
        "transcoding": True,
        "transcoding_reason": "codec_mismatch",
        **overrides,
    }
    return json.dumps({"method": "stream_info", "body": body})


@pytest.mark.asyncio
async def test_actual_sdk_unknown_method_becomes_typed_report_without_changing_dispatch():
    stream, _device, _current, now, owner = fixture()
    original = stream.handle_message.__func__
    assert await owner.install()
    try:
        assert owner.snapshot()["state"] == "not_seen"
        assert owner.snapshot()["installed_before_reader"] is True
        assert await stream.handle_message(info()) is None
        now[0] += 0.25
        assert owner.snapshot() == {
            "state": "observed",
            "installed_before_reader": True,
            "transcoding": True,
            "reason": "codec_mismatch",
            "observations": 1,
            "invalid_messages": 0,
            "oversized_messages": 0,
            "age_ms": 250,
        }
        await stream.handle_message(info(transcoding=False, transcoding_reason="private-url-token"))
        assert owner.snapshot()["transcoding"] is False
        assert owner.snapshot()["reason"] == "other"
        assert "private-url-token" not in json.dumps(owner.snapshot())
    finally:
        owner.fence()
    assert owner.snapshot() is None
    assert "handle_message" not in stream.__dict__
    assert stream.handle_message.__func__ is original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"doorbot_id": True},
        {"doorbot_id": 999},
        {"session_id": "other-session"},
        {"transcoding": 1},
        {"transcoding": "false"},
        {"transcoding": None},
    ],
)
async def test_unknown_or_wrong_authority_never_becomes_false(body):
    stream, _device, _current, _now, owner = fixture()
    assert await owner.install()
    try:
        await stream.handle_message(info(**body))
        snapshot = owner.snapshot()
        assert snapshot["state"] == "not_seen" and snapshot["transcoding"] is None
        assert snapshot["invalid_messages"] == 1
    finally:
        owner.fence()


@pytest.mark.asyncio
async def test_oversized_duplicate_and_hostile_reason_do_not_escape_fixed_schema():
    stream, _device, _current, _now, owner = fixture()
    assert await owner.install()
    try:
        await stream.handle_message(info(transcoding_reason="private" * 1000))
        assert owner.snapshot()["oversized_messages"] == 1
        unicode_oversize = json.dumps(
            json.loads(info(transcoding_reason="界" * 2000)), ensure_ascii=False
        )
        await stream.handle_message(unicode_oversize)
        assert owner.snapshot()["oversized_messages"] == 2
        duplicate = info().replace('"transcoding": true', '"transcoding": false,"transcoding":true')
        await stream.handle_message(duplicate)
        assert owner.snapshot()["invalid_messages"] == 1
        for _ in range(500):
            await stream.handle_message(info(transcoding_reason={"secret": ["private-value"]}))
        assert owner.snapshot()["observations"] == 500
        assert owner.snapshot()["reason"] == "unknown"
        assert "private" not in json.dumps(owner.snapshot())
        assert owner._reason == "unknown" and not hasattr(owner, "_message")
    finally:
        owner.fence()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "", {"private": "payload"}, "secret" * 100])
async def test_missing_or_invalid_reason_is_unknown_not_a_known_other_reason(reason):
    stream, _device, _current, _now, owner = fixture()
    assert await owner.install()
    try:
        message = json.loads(info())
        if reason is None:
            del message["body"]["transcoding_reason"]
        else:
            message["body"]["transcoding_reason"] = reason
        await stream.handle_message(json.dumps(message))
        assert owner.snapshot()["transcoding"] is True
        assert owner.snapshot()["reason"] == "unknown"
    finally:
        owner.fence()


@pytest.mark.asyncio
async def test_original_failure_and_failed_diagnostic_preserve_dispatch(monkeypatch):
    stream, _device, _current, _now, owner = fixture()
    with pytest.raises(json.JSONDecodeError) as before:
        await stream.handle_message("not-json")
    assert await owner.install()
    try:
        with pytest.raises(json.JSONDecodeError) as after:
            await stream.handle_message("not-json")
        assert (after.value.msg, after.value.pos) == (before.value.msg, before.value.pos)
        assert owner.snapshot()["observations"] == 0

        def broken(_message):
            raise asyncio.CancelledError

        monkeypatch.setattr(owner, "_observe", broken)
        assert await stream.handle_message(info()) is None
    finally:
        owner.fence()


@pytest.mark.asyncio
async def test_delegate_once_preserves_return_cancel_and_inflight_retirement(monkeypatch):
    stream, _device, current, _now, owner = fixture()
    original = RingWebRtcStream.handle_message
    entered, release, calls = asyncio.Event(), asyncio.Event(), []

    async def held(self, message_str):
        calls.append(message_str)
        entered.set()
        await release.wait()
        return "synthetic-return"

    monkeypatch.setattr(RingWebRtcStream, "handle_message", held)
    monkeypatch.setattr(ring_stream_info, "_stream_type", lambda: RingWebRtcStream)
    assert await owner.install()
    task = asyncio.create_task(stream.handle_message(info()))
    await entered.wait()
    current.value = False
    owner.fence()
    release.set()
    assert await task == "synthetic-return" and len(calls) == 1
    assert owner.snapshot() is None
    assert stream.handle_message.__func__ is held
    current.value = True
    owner = RingStreamInfo(
        SimpleNamespace(_webrtc_streams={"a": stream}), "a", current=lambda: True
    )
    assert await owner.install()
    release.clear()
    task = asyncio.create_task(stream.handle_message(info()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 2
    owner.fence()
    monkeypatch.setattr(RingWebRtcStream, "handle_message", original)


@pytest.mark.asyncio
async def test_absent_unsupported_cancel_and_registry_replacement_are_explicit(monkeypatch):
    stream, device, _current, _now, owner = fixture()
    device._webrtc_streams.clear()
    assert not await owner.install(timeout=0.01)
    assert owner.snapshot()["state"] == "absent"
    assert owner.snapshot()["transcoding"] is None
    owner = RingStreamInfo(device, "attempt", current=lambda: True)
    task = asyncio.create_task(owner.install())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner.snapshot() is None

    device._webrtc_streams["attempt"] = stream
    owner = RingStreamInfo(device, "attempt", current=lambda: True)
    monkeypatch.setattr(ring_stream_info.metadata, "version", lambda _name: "unsupported")
    assert not await owner.install()
    assert owner.snapshot()["state"] == "unavailable"
    monkeypatch.undo()
    owner = RingStreamInfo(device, "attempt", current=lambda: True)
    assert await owner.install()
    device._webrtc_streams["attempt"] = RingWebRtcStream(None, 123)
    assert owner.snapshot() is None
    assert "handle_message" not in stream.__dict__


@pytest.mark.asyncio
async def test_late_install_never_claims_startup_coverage_or_clobbers_another_wrapper():
    stream, _device, _current, _now, owner = fixture()
    started = asyncio.Event()

    async def reader():
        started.set()
        await asyncio.Event().wait()

    stream.read_task = asyncio.create_task(reader())
    await started.wait()
    assert await owner.install()
    assert owner.snapshot()["installed_before_reader"] is False
    wrapper = stream.handle_message

    async def another(message):
        return await wrapper(message)

    stream.handle_message = another
    assert owner.snapshot() is None
    owner.fence()
    assert stream.handle_message is another
    await another(info())
    assert owner._observations == 0
    stream.read_task.cancel()
    await asyncio.gather(stream.read_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_created_reader_is_covered_and_guard_failure_remains_available_as_unknown(
    monkeypatch,
):
    stream, _device, _current, _now, owner = fixture()

    async def reader():
        await asyncio.Event().wait()

    stream.read_task = asyncio.create_task(reader())
    assert await owner.install()  # No await suspension before the existing stream is wrapped.
    assert owner.snapshot()["installed_before_reader"] is True
    owner.fence()
    stream.read_task.cancel()
    await asyncio.gather(stream.read_task, return_exceptions=True)
    stream, _device, _current, _now, owner = fixture()

    def broken(_name):
        raise RuntimeError("private dependency error")

    monkeypatch.setattr(ring_stream_info.metadata, "version", broken)
    assert not await owner.install()
    assert owner.snapshot()["state"] == "unavailable"
    assert owner.snapshot()["transcoding"] is None
    assert "private" not in json.dumps(owner.snapshot())
    assert "handle_message" not in stream.__dict__


@pytest.mark.asyncio
async def test_actual_sdk_creation_prefix_arms_during_generate_before_reader(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    device = SimpleNamespace(_ring=None, device_api_id=123, _webrtc_streams={})

    async def close(_session_id):
        pass

    device.close_webrtc_stream = close

    async def generate(stream, _offer):
        entered.set()
        await release.wait()
        stream.session_id = "synthetic-session"
        await stream.handle_message(info())

    monkeypatch.setattr(RingWebRtcStream, "generate", generate)
    owner = RingStreamInfo(device, "attempt", current=lambda: True)
    installer = asyncio.create_task(owner.install())
    generation = asyncio.create_task(
        RingDoorBell.generate_async_webrtc_stream(
            device, "synthetic-offer", "attempt", lambda _msg: None
        )
    )
    await entered.wait()
    assert await installer
    assert owner.snapshot()["installed_before_reader"] is True
    release.set()
    await generation
    assert owner.snapshot()["observations"] == 1
    owner.fence()


@pytest.mark.asyncio
async def test_receiver_lifetime_joins_installer_and_sampler_even_when_stats_fail(monkeypatch):
    stop, snapshots = threading.Event(), []

    async def generate(session_id, notify):
        stream = device._webrtc_streams[session_id]
        while "handle_message" not in stream.__dict__:
            await asyncio.sleep(0)
        stream.device_api_id = 123
        stream.session_id = "synthetic-session"
        await stream.handle_message(info())
        notify(SimpleNamespace(answer="synthetic-answer"))

    device = FakeDevice(generate)
    pc = install_media_fakes(monkeypatch)

    async def accept(_answer):
        pass

    pc.setRemoteDescription = accept

    class Pipeline:
        def __init__(self, *_args):
            pass

        def install_lower_boundaries(self):
            pass

        def wake_lag(self, _seconds):
            pass

        def snapshot(self):
            return None

        def fence(self):
            pass

    from halo_server.media import receiver_metrics

    monkeypatch.setattr(receiver_metrics, "ReceiverPipeline", Pipeline)
    monkeypatch.setattr(receiver_metrics, "install_rtp_observer", lambda *_args: None)

    def sampled(snapshot, _lag, _duration):
        snapshots.append(snapshot)
        stop.set()

    receiver = ring_media.RingStreamReceiver(device, lambda *_args: None, on_receiver_stats=sampled)
    await asyncio.wait_for(receiver.receive(stop), 2)
    assert snapshots[0]["audio"] is None
    assert snapshots[0]["stream_info"]["state"] == "observed"
    assert snapshots[0]["stream_info"]["installed_before_reader"] is True
    assert pc.closed and len(device.closed_sessions) == 1 and not device._webrtc_streams
