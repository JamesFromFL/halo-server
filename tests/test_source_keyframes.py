"""Current-lease, bounded PLI requests without accounts or real camera IO."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest
from aiortc.rtcrtpreceiver import RTCRtpReceiver
from aiortc.rtp import RtcpPacket
from test_homekit_controller import POLICY, configured
from test_media_session import _session

import halo_server.service as service_module
from halo_server.homekit.models import HomeKitOperationError
from halo_server.media import keyframes
from halo_server.media.buffer import EncodedMediaBuffer
from halo_server.media.consumers import BufferedMediaConsumer
from halo_server.media.keyframes import SourceKeyframes
from halo_server.media.leases import LeasePurpose
from halo_server.service import HaloService


def receiver(send):
    class Transport:
        state = "connected"
        _stats_id = "offline"

        async def _send_rtp(self, packet):
            await send(packet)

    result = RTCRtpReceiver("video", Transport())
    result._set_rtcp_ssrc(77)
    result._RTCRtpReceiver__active_ssrc = {1001: datetime.now(UTC), 2001: datetime.now(UTC)}
    result._RTCRtpReceiver__remote_streams = {1001: object(), 2001: object()}
    result._RTCRtpReceiver__rtx_ssrc = {2001: 1001}
    return result


def owner(target, current=lambda: True):
    tasks = set()

    def spawn(coroutine, _label):
        task = asyncio.create_task(coroutine)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    return SourceKeyframes(target, current=current, spawn=spawn), tasks


@pytest.mark.asyncio
async def test_real_pinned_pli_targets_only_recent_known_primary_and_coalesces(monkeypatch):
    sent = []

    async def send(packet):
        sent.append(RtcpPacket.parse(packet)[0])

    target = receiver(send)
    target._RTCRtpReceiver__active_ssrc.update(
        {3001: datetime.now(UTC), 4001: datetime.now(UTC) - timedelta(seconds=11)}
    )
    target._RTCRtpReceiver__remote_streams[4001] = object()
    requester, tasks = owner(target)
    try:
        assert await requester.request(lambda: True)
        assert [(packet.fmt, packet.ssrc, packet.media_ssrc) for packet in sent] == [(1, 77, 1001)]
        assert not await requester.request(lambda: True)
        assert requester.snapshot()["coalesced"] == 1
        assert not await requester.request(lambda: False)
        assert len(sent) == 1
        assert "1001" not in repr(requester.snapshot())
        monkeypatch.setattr(keyframes.aiortc, "__version__", "future-version")
        unsupported, _ = owner(target)
        assert not unsupported.supported and not await unsupported.request(lambda: True)
        await unsupported.close()
    finally:
        await requester.close()
    await asyncio.sleep(0)
    assert not tasks


@pytest.mark.asyncio
async def test_pending_callers_do_not_create_more_sender_tasks():
    entered, release = asyncio.Event(), asyncio.Event()
    packets = []

    async def send(packet):
        packets.append(packet)
        entered.set()
        await release.wait()

    requester, tasks = owner(receiver(send))
    first = asyncio.create_task(requester.request(lambda: True))
    try:
        await entered.wait()
        assert not any(await asyncio.gather(*(requester.request(lambda: True) for _ in range(50))))
        assert len(tasks) == len(packets) == 1
        release.set()
        assert await first
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await requester.close()


@pytest.mark.asyncio
async def test_caller_cancel_does_not_orphan_actual_source_task_and_close_joins():
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def send(_packet):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    requester, tasks = owner(receiver(send))
    call = asyncio.create_task(requester.request(lambda: True))
    closing = None
    try:
        await entered.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert requester.snapshot()["pending"] and len(tasks) == 1
        closing = asyncio.create_task(requester.close())
        await cancelled.wait()
        assert not closing.done()
        assert requester.submit(lambda: True).result() is False
        release.set()
        await closing
        assert not requester.snapshot()["pending"]
    finally:
        release.set()
        await asyncio.gather(call, *(tuple([closing]) if closing else ()), return_exceptions=True)
        await requester.close()


@pytest.mark.asyncio
async def test_sender_deadline_and_closed_transport_do_not_claim_source_ack(monkeypatch):
    async def blocked(_packet):
        await asyncio.Event().wait()

    monkeypatch.setattr(keyframes, "KEYFRAME_REQUEST_SECONDS", 0.01)
    requester, _ = owner(receiver(blocked))
    assert not await requester.request(lambda: True)
    assert requester.snapshot()["attempted"] == 1
    await requester.close()

    async def disconnected(_packet):
        raise ConnectionError("sensitive transport details")

    # aiortc intentionally swallows this error. True is a local request attempt,
    # not proof that Ring received RTCP or produced a new IDR.
    requester, _ = owner(receiver(disconnected))
    assert await requester.request(lambda: True)
    assert requester.snapshot()["attempted"] == 1
    await requester.close()


@pytest.mark.asyncio
async def test_source_requires_exact_live_lease_epoch_receiver_and_admission(monkeypatch):
    session, client = _session()
    live = session.acquire(LeasePurpose.HOMEKIT_LIVE)
    other = session.acquire(LeasePurpose.HOMEKIT_RECORDING)
    sent, allowed = [], [True]

    async def send(packet):
        sent.append(packet)

    requester, _ = owner(receiver(send), current=lambda: not session._stop.is_set())
    session._attach_keyframes(live.generation, requester.submit)
    epoch = session.buffer.capture_epoch
    try:
        assert not await asyncio.to_thread(
            session.request_keyframe, live.id, epoch - 1, admission=lambda: True
        )
        assert not await asyncio.to_thread(
            session.request_keyframe, other.id, epoch, admission=lambda: True
        )
        assert not await asyncio.to_thread(
            session.request_keyframe, "unknown", epoch, admission=lambda: True
        )
        assert not await asyncio.to_thread(
            session.request_keyframe, live.id, epoch, admission=lambda: False
        )
        assert not sent
        assert await asyncio.to_thread(
            session.request_keyframe, live.id, epoch, admission=lambda: allowed[0]
        )
        assert len(sent) == 1 and len(client.futures) == 1  # No new receiver/client submission.
        old_submit = session._keyframe_requester[2]
        session._attempt_ended(live.generation, 1, None)
        assert not await asyncio.to_thread(
            session.request_keyframe, live.id, epoch, admission=lambda: True
        )
        assert session._keyframe_requester is None
        # Its attempt token is not reused even if a caller retained a callback.
        assert not await requester.request(lambda: session.buffer.capture_epoch == epoch)
        assert callable(old_submit)
    finally:
        await requester.close()
        session.stop_all()


@pytest.mark.asyncio
async def test_source_final_scope_guard_prevents_admission_after_lease_closes():
    session, _ = _session()
    live = session.acquire(LeasePurpose.HOMEKIT_LIVE)
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []
    loop = asyncio.get_running_loop()

    async def delayed(admission):
        entered.set()
        await release.wait()
        if admission():
            sent.append(True)
            return True
        return False

    session._attach_keyframes(
        live.generation,
        lambda admission: asyncio.run_coroutine_threadsafe(delayed(admission), loop),
    )
    request = asyncio.create_task(
        asyncio.to_thread(
            session.request_keyframe, live.id, session.buffer.capture_epoch, admission=lambda: True
        )
    )
    try:
        await entered.wait()
        await asyncio.to_thread(session.release, live.id)
        release.set()
        assert not await request and not sent
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        session.stop_all()


@pytest.mark.asyncio
async def test_consumer_gateway_handoff_has_no_state_lock_inversion(monkeypatch):
    loop = asyncio.get_running_loop()
    service = NS(_main_loop=loop)
    monkeypatch.setattr(service_module, "GATEWAY_CALL_TIMEOUT", 0.5)
    checked = []

    async def inspect():
        checked.append(consumer.is_current())
        return True

    consumer = BufferedMediaConsumer(
        EncodedMediaBuffer(),
        lambda: True,
        lambda: None,
        keyframe_callback=lambda _epoch: HaloService._run_gateway_call(service, inspect()),
    )
    assert await asyncio.to_thread(consumer.request_keyframe, consumer.buffer.capture_epoch)
    assert checked == [True]
    consumer.close()


@pytest.mark.asyncio
async def test_consumer_stop_fences_immediately_then_joins_keyframe_before_release_retry():
    entered, release = threading.Event(), threading.Event()
    outcomes = []

    def request(_epoch):
        entered.set()
        assert release.wait(2)
        outcomes.append("request")
        return True

    def close():
        outcomes.append("release")
        if outcomes.count("release") == 1:
            raise RuntimeError("synthetic cleanup failure")

    consumer = BufferedMediaConsumer(
        EncodedMediaBuffer(), lambda: True, close, keyframe_callback=request
    )
    pending = asyncio.create_task(
        asyncio.to_thread(consumer.request_keyframe, consumer.buffer.capture_epoch)
    )
    closing = None
    try:
        await asyncio.to_thread(entered.wait, 1)
        closing = asyncio.create_task(asyncio.to_thread(consumer.close))
        while consumer.is_current():
            await asyncio.sleep(0)
        assert not closing.done() and not outcomes
        release.set()
        assert not await pending
        with pytest.raises(RuntimeError):
            await closing
        await asyncio.to_thread(consumer.close)
        consumer.close()
        assert outcomes == ["request", "release", "release"]
    finally:
        release.set()
        await asyncio.gather(
            pending, *(tuple([closing]) if closing else ()), return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["privacy", "epoch", "boot", "stop"])
async def test_native_controller_owns_pending_request_and_rechecks_authority(boundary):
    controller, backend, boot, (scope,) = await configured()
    handle = await controller.open_live(boot, scope)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def request(epoch):
        calls.append(epoch)
        entered.set()
        assert release.wait(2)
        return True

    backend.resources[0].request_keyframe = request
    pending = asyncio.create_task(handle.request_keyframe(capture_epoch=handle.capture_epoch))
    changing = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        if boundary == "epoch":
            backend.resources[0].buffer.clear()
        elif boundary == "privacy":
            changing = asyncio.create_task(controller.update_policy(boot, scope, replace_policy()))
        elif boundary == "boot":
            changing = asyncio.create_task(controller.detach(boot))
        else:
            changing = asyncio.create_task(handle.close())
        await asyncio.sleep(0)
        if changing is not None:
            assert not changing.done()  # Owned native work has not retired.
        release.set()
        result = await asyncio.gather(pending, return_exceptions=True)
        assert isinstance(result[0], (HomeKitOperationError, asyncio.CancelledError))
        if changing is not None:
            await changing
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.gather(
            pending, *(tuple([changing]) if changing else ()), return_exceptions=True
        )
        await controller.close()


def replace_policy():
    from dataclasses import replace

    return replace(POLICY, streaming=False)
