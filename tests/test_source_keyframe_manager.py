"""Home feedback remains nonblocking while current-source PLI is bounded."""

import asyncio
import threading
from types import SimpleNamespace as NS

import pytest
from aiortc.rtp import RtcpPsfbPacket, RtcpRrPacket
from test_homekit_live import native_crypto, prepared

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media.supervisor import supervise_receiver


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_authenticated_home_pli_queues_only_and_rr_reader_survives_pending_source_request():
    manager, controller, identity, result, endpoint = await prepared()
    session = manager._sessions[identity]
    entered, release = asyncio.Event(), asyncio.Event()
    local, source = [], []
    session.adapter = NS(mode="copy", request_keyframe=lambda: local.append(True))
    session.handle = controller.handle

    async def request(*, capture_epoch):
        source.append(capture_epoch)
        entered.set()
        await release.wait()
        return True

    session.handle.request_keyframe = request
    peer = native_crypto(500)
    feedback = asyncio.create_task(manager._receive_feedback(session))
    operation = None
    try:
        pli = bytes(RtcpPsfbPacket(fmt=1, ssrc=500, media_ssrc=result.ssrc))
        endpoint.sendto(peer.protect_rtcp(pli), ("127.0.0.1", result.port))
        await until(lambda: len(local) == 1)
        assert not source and session.upstream_keyframe_pending
        operation = asyncio.create_task(manager._request_upstream_keyframe(session))
        await entered.wait()
        endpoint.sendto(
            peer.protect_rtcp(bytes(RtcpRrPacket(ssrc=500))), ("127.0.0.1", result.port)
        )
        await until(lambda: session.metrics.snapshot()["counters"]["feedback_datagrams"] == 2)
        assert not operation.done() and len(source) == 1
        release.set()
        await operation
        assert session.metrics.snapshot()["counters"]["source_keyframe_attempted"] == 1
    finally:
        release.set()
        feedback.cancel()
        await asyncio.gather(
            feedback, *(tuple([operation]) if operation else ()), return_exceptions=True
        )
        session.adapter = None
        endpoint.close()
        await manager.close()


@pytest.mark.asyncio
async def test_manager_coalesces_busy_and_adapted_output_needs_only_local_force():
    manager, controller, identity, _, endpoint = await prepared()
    session = manager._sessions[identity]
    clock, calls = [100.0], []
    manager._clock = lambda: clock[0]
    session.handle = controller.handle
    session.adapter = NS(mode="copy", request_keyframe=lambda: None)

    async def request(*, capture_epoch):
        calls.append(capture_epoch)
        if len(calls) == 1:
            raise HomeKitOperationError("busy")
        return True

    session.handle.request_keyframe = request
    try:
        await manager._request_upstream_keyframe(session)
        assert session.upstream_keyframe_pending and not session.retired
        await manager._request_upstream_keyframe(session)
        assert len(calls) == 1
        clock[0] += 0.5
        await manager._request_upstream_keyframe(session)
        assert len(calls) == 2
        session.adapter.mode = "adapt"
        session.upstream_keyframe_pending = True
        clock[0] += 1
        await manager._request_upstream_keyframe(session)
        assert len(calls) == 2 and not session.upstream_keyframe_pending
    finally:
        session.adapter = None
        endpoint.close()
        await manager.close()


@pytest.mark.asyncio
async def test_supervisor_cannot_publish_an_old_attempt_requester_after_replacement():
    stop = threading.Event()
    published, callbacks = [], []

    class Receiver:
        def __init__(self, *_args, on_keyframe_requester, **_kwargs):
            self.publish = on_keyframe_requester
            callbacks.append(self.publish)

        async def receive(self, _stop):
            self.publish(len(callbacks))
            if len(callbacks) == 2:
                assert callbacks[0]("stale") is False
                stop.set()

    await supervise_receiver(
        object(),
        None,
        None,
        stop,
        receiver_factory=Receiver,
        receiver_kwargs={"on_keyframe_requester": published.append},
        retry_min=0,
        retry_max=0,
    )
    assert published == [1, 2]


@pytest.mark.asyncio
async def test_source_request_runs_between_batches_never_in_front_of_already_read_au():
    import time

    from test_homekit_live import CONFIG, encoded_picture

    from halo_server.media.buffer import BufferRead, MediaStreamKind
    from halo_server.media.live_timeline import LiveTimeline

    manager, controller, identity, _, endpoint = await prepared()
    session = manager._sessions[identity]
    session.handle, session.config = controller.handle, CONFIG
    session.capture_epoch = controller.handle.capture_epoch
    session.awaiting_idr = False
    session.last_source = session.last_renewal = manager._clock()
    session.timeline = LiveTimeline(audio_rate=None, video_origin=session.output_base)
    calls = []
    original = NS(
        stream=MediaStreamKind.VIDEO,
        payload=encoded_picture(),
        rtp_timestamp=90000,
        received_at=time.monotonic(),
        capture_epoch=session.capture_epoch,
    )

    async def read(*, timeout):
        calls.append("read")
        if calls.count("read") > 1:
            raise asyncio.CancelledError
        return BufferRead((original,), False, session.capture_epoch)

    async def feed(payload, timestamp, *, capture_epoch, received_at):
        calls.append("feed")
        session.upstream_keyframe_pending = True  # Home asks while processing this batch.
        return (
            NS(
                payload=payload,
                timestamp=timestamp,
                mode="copy",
                is_keyframe=True,
                capture_epoch=capture_epoch,
                configuration_generation=1,
                received_at=received_at,
            ),
        )

    async def request(*, capture_epoch):
        calls.append("request")
        assert capture_epoch == session.capture_epoch
        return True

    async def send(*_args, **_kwargs):
        calls.append("send")
        return True

    session.handle.read = read
    session.handle.request_keyframe = request
    session.adapter = NS(mode="copy", configuration_generation=1, feed=feed)
    manager._access_unit = send
    try:
        with pytest.raises(asyncio.CancelledError):
            await manager._send_video(session)
        assert calls == ["read", "feed", "send", "request", "read"]
    finally:
        session.adapter = None
        endpoint.close()
        await manager.close()
