"""Source progress follows established decoder continuity, not prebuffer readiness."""

import asyncio
from types import SimpleNamespace

import av
import pytest
from test_media_buffer_native import decode_packets, decoder, encode_source
from test_media_session import FakeClient, FakeClock, _receiver_kwargs, fake_supervisor
from test_ring_media import FakeDevice, install_media_fakes

from halo_server.media import ring_media
from halo_server.media.buffer import EncodedMediaBuffer
from halo_server.media.h264 import annexb, split_annexb
from halo_server.media.leases import LeasePurpose, MediaLeaseCoordinator
from halo_server.media.session import CameraMediaSession


def capture_session(clock, *, max_bytes=32 * 1024 * 1024):
    client = FakeClient()
    capture = CameraMediaSession(
        client,
        SimpleNamespace(id=42, name="Test camera"),
        buffer=EncodedMediaBuffer(clock=clock, max_bytes=max_bytes),
        lease_coordinator=MediaLeaseCoordinator(clock=clock),
        receiver_supervisor=fake_supervisor,
        legacy_audio_enabled=False,
        clock=clock,
    )
    lease = capture.acquire(LeasePurpose.HOMEKIT_LIVE)
    return capture, lease, _receiver_kwargs(client.futures[0])["on_encoded_video"]


@pytest.mark.asyncio
async def test_real_decoder_progress_survives_long_gop_without_retained_idr(monkeypatch):
    source = encode_source(count=34, keyint=30)
    clock = FakeClock(100)
    capture, lease, admit = capture_session(clock)
    peer = install_media_fakes(monkeypatch)
    callbacks = {}
    accepted, decoded, not_ready, continuity = [], [], [], []
    continuous, reference = decoder(), decoder()
    cursor = None

    def install(_receiver, kind, callback, **_kwargs):
        callbacks[kind] = callback

    monkeypatch.setattr(ring_media, "install_encoded_frame_adapter", install)

    async def apply_answer(_answer):
        peer.connectionState = "connected"
        peer.answer_started.set()

    peer.setRemoteDescription = apply_answer

    def deliver(index):
        nonlocal cursor
        clock.value = 100 + index
        capture.renew(lease.id)
        accepted.append(callbacks["video"](source[index], index * 90000))
        batch = capture.buffer.read(after_sequence=cursor, timeout=0)
        assert not batch.resynchronized
        actual = decode_packets(continuous, batch.packets)
        expected = reference.decode(av.Packet(source[index]))
        assert len(actual) == len(expected) == 1
        assert actual[0].to_ndarray().tobytes() == expected[0].to_ndarray().tobytes()
        decoded.extend(actual)
        cursor = batch.packets[-1].sequence
        if not capture.buffer.ready:
            not_ready.append(index)
            continuity.append(capture.buffer.video_continuity)
            assert not capture.buffer.read(after_sequence=None, timeout=0).packets

    async def generated(_session_id, notify):
        notify(SimpleNamespace(answer="answer", candidate=None, error_code=None))
        await peer.answer_started.wait()
        deliver(0)

    device = FakeDevice(on_generate=generated)
    receiver = ring_media.RingStreamReceiver(
        device,
        None,
        on_encoded_video=admit,
        decode_video=False,
        decode_audio=False,
    )
    real_loop = asyncio.get_running_loop()
    owner = asyncio.current_task()

    class LoopClock:
        def time(self):
            return clock.value

        def __getattr__(self, name):
            return getattr(real_loop, name)

    class ReceiverAsyncio:
        def get_running_loop(self):
            return LoopClock()

        async def sleep(self, delay):
            # Only advance the actual receiver's watchdog polling loop. Other
            # receiver-owned tasks retain their own scheduling and cleanup.
            if asyncio.current_task() is owner and delay == 0.1:
                index = len(accepted)
                if index == len(source):
                    capture._stop.set()
                else:
                    deliver(index)
            await asyncio.sleep(0)

        def __getattr__(self, name):
            return getattr(asyncio, name)

    monkeypatch.setattr(ring_media, "asyncio", ReceiverAsyncio())
    try:
        async with asyncio.timeout(3):
            await receiver.receive(capture._stop)
        assert accepted == [True] * 34
        assert len(decoded) == 34
        assert not_ready == list(range(13, 30))
        assert all(continuity)
        assert clock.value == 133  # Beyond the prior t=22 false-stall deadline.
        assert peer.closed and len(device.closed_sessions) == 1
    finally:
        capture.stop_all()


@pytest.mark.parametrize("reset", ["cold", "configuration", "epoch", "capacity"])
def test_unusable_pictures_do_not_count_as_source_progress(reset):
    source = encode_source(count=2)
    clock = FakeClock(100)
    capture, _lease, admit = capture_session(
        clock, max_bytes=len(source[0]) if reset == "capacity" else 32 * 1024 * 1024
    )
    try:
        if reset == "capacity":
            # The IDR fits alone, but its separately retained parameter cache
            # exceeds the aggregate budget. No reader could have started.
            assert admit(source[0], 0) is False
            assert not capture.buffer._packets
        elif reset != "cold":
            assert admit(source[0], 0)
            assert capture.buffer.video_continuity
            if reset == "epoch":
                capture.buffer.clear()
            else:
                changed = encode_source(count=2, width=160, height=120)
                headers = annexb(nal for nal in split_annexb(changed[0]) if nal[0] & 31 in {7, 8})
                clock.value += 1
                assert admit(headers, 90000) is False
                source = changed
        clock.value += 1
        assert admit(source[1], 180000) is False
        assert not capture.buffer.video_continuity
        assert not capture.buffer.ready
        assert not capture.buffer.read(after_sequence=None, timeout=0).packets
        if reset != "capacity":
            clock.value += 1
            assert admit(source[0], 270000)
            assert capture.buffer.video_continuity and capture.buffer.ready
    finally:
        capture.stop_all()
