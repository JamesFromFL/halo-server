"""Actual pinned SDK activation with in-memory signaling and no network."""

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from ring_doorbell.webrtcstream import RingWebRtcStream
from test_ring_media import FakeDevice, install_media_fakes

from halo_server.media import ring_media, ring_stream_options
from halo_server.media.ring_stream_options import RingStreamOptions, RingStreamOptionsError


class Socket:
    def __init__(self):
        self.messages = []
        self.block_method = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.closed = False
        self.fail = False

    async def send(self, value):
        message = json.loads(value)
        if message["method"] == self.block_method:
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.fail:
            raise RuntimeError("private-ticket-and-sdp")
        if self.closed:
            raise ConnectionError("private-signaling-address")
        self.messages.append(message)

    async def close(self):
        self.closed = True
        self.release.set()


@asynccontextmanager
async def source(*, activate=True):
    stream = RingWebRtcStream(None, 1, on_message_callback=lambda _message: None)
    stream.dialog_id = "synthetic-dialog"
    stream.session_id = "synthetic-ring-session"
    stream.sdp_offer = "m=audio 9 RTP/AVP 111\r\na=sendrecv\r\n"
    stream._offered_event.set()
    socket = Socket()
    stream.websocket = socket
    device = SimpleNamespace(_webrtc_streams={"attempt": stream})
    admission = SimpleNamespace(current=True)
    owner = RingStreamOptions(device, "attempt", current=lambda: admission.current)
    if activate:
        await stream.handle_answer_message({"body": {"sdp": stream.sdp_offer}})
    try:
        yield stream, socket, device, admission, owner
    finally:
        owner.fence()
        await stream.close()


@pytest.mark.asyncio
async def test_actual_sdk_activation_then_one_reassertion_despite_concurrent_answers_and_renewals():
    async with source() as (stream, socket, _device, _admission, owner):
        assert sorted(await asyncio.gather(owner.enable(), owner.enable())) == [False, True]
        for _ in range(3):
            await stream.keep_alive()
            assert not await owner.enable()
        assert [item["method"] for item in socket.messages] == [
            "activate_session",
            "stream_options",
        ]
        assert socket.messages[1]["body"] == {
            "doorbot_id": 1,
            "session_id": "synthetic-ring-session",
            "audio_enabled": True,
            "video_enabled": True,
        }


@pytest.mark.asyncio
async def test_answer_notification_does_not_bypass_held_activation_write():
    async with source(activate=False) as (stream, socket, _device, _admission, owner):
        socket.block_method = "activate_session"
        activation = asyncio.create_task(
            stream.handle_answer_message({"body": {"sdp": stream.sdp_offer}})
        )
        await socket.entered.wait()
        assert stream._sdp_answer_event.is_set()
        task = asyncio.create_task(owner.enable())
        await asyncio.sleep(0.02)
        assert not socket.messages and not task.done()
        socket.release.set()
        await activation
        assert await task
        assert [item["method"] for item in socket.messages] == [
            "activate_session",
            "stream_options",
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["stop", "fence", "replacement", "close"])
async def test_waiting_attempt_cannot_send_after_retirement(retirement):
    async with source(activate=False) as (stream, socket, device, admission, owner):
        stream._sdp_answer_event.set()
        task = asyncio.create_task(owner.enable())
        await asyncio.sleep(0)
        if retirement == "stop":
            admission.current = False
        elif retirement == "fence":
            owner.fence()
        elif retirement == "replacement":
            device._webrtc_streams["attempt"] = object()
        else:
            await stream.close()
        with pytest.raises(RingStreamOptionsError) as caught:
            await asyncio.wait_for(task, 0.2)
        assert caught.value.code == "retired"
        assert socket.messages == []


@pytest.mark.asyncio
async def test_timeout_and_cancellation_leave_no_detached_writer_or_replay():
    async with source() as (_stream, socket, _device, _admission, owner):
        socket.block_method = "stream_options"
        with pytest.raises(RingStreamOptionsError) as caught:
            await owner.enable(timeout=0.02)
        assert caught.value.code == "timeout" and socket.cancelled
        socket.release.set()
        with pytest.raises(RingStreamOptionsError) as caught:
            await owner.enable()
        assert caught.value.code == "retired"
        assert [item["method"] for item in socket.messages] == ["activate_session"]
    async with source() as (_stream, socket, _device, _admission, owner):
        socket.block_method = "stream_options"
        task = asyncio.create_task(owner.enable())
        await socket.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert socket.cancelled
        socket.release.set()
        with pytest.raises(RingStreamOptionsError, match="authority ended"):
            await owner.enable()


@pytest.mark.asyncio
async def test_inflight_write_retains_ownership_but_cannot_complete_after_stop():
    async with source() as (_stream, socket, _device, admission, owner):
        socket.block_method = "stream_options"
        task = asyncio.create_task(owner.enable())
        await socket.entered.wait()
        admission.current = False
        owner.fence()
        await asyncio.sleep(0)
        assert not task.done()  # Already-admitted writes must still be joined.
        socket.release.set()
        with pytest.raises(RingStreamOptionsError) as caught:
            await task
        assert caught.value.code == "retired"
        assert len(socket.messages) == 2  # Prior admission cannot be recalled.


@pytest.mark.asyncio
async def test_new_source_owner_reasserts_but_old_owner_never_adopts_replacement():
    async with source() as (_stream, socket, device, _admission, owner):
        assert await owner.enable()
        async with source() as (replacement, new_socket, _new_device, _new_admission, _new):
            device._webrtc_streams["attempt"] = replacement
            with pytest.raises(RingStreamOptionsError, match="authority ended"):
                await owner.enable()
            fresh = RingStreamOptions(device, "attempt", current=lambda: True)
            assert await fresh.enable()
            fresh.fence()
            assert len(socket.messages) == len(new_socket.messages) == 2


@pytest.mark.asyncio
async def test_unknown_sdk_and_changed_activation_contract_fail_without_write(monkeypatch):
    async with source() as (_stream, socket, _device, _admission, owner):
        monkeypatch.setattr(ring_stream_options.metadata, "version", lambda _name: "0.9.15")
        with pytest.raises(RingStreamOptionsError) as caught:
            await owner.enable()
        assert caught.value.code == "unsupported" and len(socket.messages) == 1
    monkeypatch.undo()
    async with source() as (_stream, socket, _device, _admission, owner):

        async def changed(_self):
            pass

        monkeypatch.setattr(RingWebRtcStream, "_activate", changed)
        with pytest.raises(RingStreamOptionsError) as caught:
            await owner.enable()
        assert caught.value.code == "unsupported" and len(socket.messages) == 1


@pytest.mark.asyncio
async def test_missing_activation_times_out_and_send_errors_do_not_leak_or_retry():
    async with source(activate=False) as (stream, socket, _device, _admission, owner):
        stream._sdp_answer_event.set()
        with pytest.raises(RingStreamOptionsError) as caught:
            await owner.enable(timeout=0.01)
        assert caught.value.code == "timeout" and not socket.messages
    async with source() as (_stream, socket, _device, _admission, owner):
        socket.fail = True
        with pytest.raises(RingStreamOptionsError) as caught:
            await owner.enable()
        assert caught.value.code == "send_failed"
        assert str(caught.value) == "Ring stream options command failed"
        assert caught.value.__cause__ is None
        socket.fail = False
        with pytest.raises(RingStreamOptionsError, match="authority ended"):
            await owner.enable()
        assert len(socket.messages) == 1


@pytest.mark.asyncio
async def test_receiver_integrates_actual_sdk_reassertion_before_ready_and_joins_close(
    monkeypatch, caplog
):
    peer = install_media_fakes(monkeypatch)
    stop = threading.Event()

    async def answer(_description):
        peer.connectionState = "connected"

    peer.setRemoteDescription = answer

    class ActualAnswerDevice(FakeDevice):
        async def generate_async_webrtc_stream(
            self, _sdp, session_id, on_message, *, keep_alive_timeout
        ):
            assert keep_alive_timeout is None
            stream = RingWebRtcStream(None, 1, on_message_callback=on_message)
            stream.dialog_id = "synthetic-dialog"
            stream.session_id = "synthetic-ring-session"
            stream.sdp_offer = "m=audio 9 RTP/AVP 111\r\na=sendrecv\r\n"
            stream._offered_event.set()
            socket = Socket()
            stream.websocket = socket
            self.messages = socket.messages
            self._webrtc_streams[session_id] = stream
            # Actual SDK invokes Halo's synchronous callback BEFORE _activate.
            await stream.handle_answer_message({"body": {"sdp": stream.sdp_offer}})
            async with asyncio.timeout(1):
                while len(self.messages) < 2:
                    await asyncio.sleep(0.001)
            stop.set()

    device = ActualAnswerDevice()
    with caplog.at_level("INFO", logger=ring_media.__name__):
        await ring_media.RingStreamReceiver(device, None).receive(stop)
    assert [item["method"] for item in device.messages] == ["activate_session", "stream_options"]
    assert peer.closed and len(device.closed_sessions) == 1 and not device._webrtc_streams
    assert all(task.done() for task in device.activation_tasks)
    reports = [v.getMessage() for v in caplog.records if "activation options result=" in v.message]
    assert reports == ["Ring WebRTC activation options result=sent"]


@pytest.mark.asyncio
async def test_expired_write_that_suppresses_cancellation_cannot_report_success():
    async with source() as (stream, socket, _device, _admission, owner):
        original_send = socket.send

        async def delayed_send(value):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                await original_send(value)

        socket.send = delayed_send
        with pytest.raises(RingStreamOptionsError) as caught:
            await owner.enable(timeout=0.01)
        assert caught.value.code == "timeout"
        assert len(socket.messages) == 2  # Admitted write returned after its deadline.
        with pytest.raises(RingStreamOptionsError, match="authority ended"):
            await owner.enable()
        assert stream.ping_task is not None
