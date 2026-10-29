"""True Ring-format SILK through the owned native pipeline and independent SRTP."""

import asyncio
import ctypes
import ctypes.util
import socket
import time
from fractions import Fraction
from types import SimpleNamespace

import av
import numpy as np
import pylibsrtp
import pytest
from aiortc.rtp import RtpPacket

from halo_server.media.homekit_audio import AudioEndpointRequest, AudioTrack
from halo_server.media.homekit_audio_codec import AudioAdapter
from halo_server.media.opus import AudioConfiguration, parse_opus, repacketize


def source_packets(count):
    encoder = av.CodecContext.create("libopus", "w")
    encoder.thread_count = 1
    encoder.sample_rate, encoder.layout, encoder.format = 16000, "mono", "s16"
    encoder.bit_rate, encoder.time_base = 56000, Fraction(1, 16000)
    encoder.options = {"application": "voip", "frame_duration": "60", "vbr": "off"}
    encoder.open()
    random = np.random.default_rng(17)
    result = []
    for index in range(count):
        data = (random.standard_normal(960) * 1800).astype("int16").reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(data, format="s16", layout="mono")
        frame.sample_rate, frame.pts, frame.time_base = 16000, index * 960, Fraction(1, 16000)
        result.extend(repacketize((bytes(packet),)) for packet in encoder.encode(frame))
    assert len(result) == count
    assert all(
        payload[0] == 0x58
        and parse_opus(payload).duration_samples == parse_opus(payload).frame_samples == 2880
        for payload in result
    )
    return result


def decode_every_packet(packets):
    """Independent public reference API; no re-grouping or truncated tail."""
    name = ctypes.util.find_library("opus")
    if name is None:
        pytest.skip("Independent reference decoder oracle unavailable")
    library = ctypes.CDLL(name)
    library.opus_decoder_create.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
    ]
    library.opus_decoder_create.restype = ctypes.c_void_p
    library.opus_decode_float.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.c_int,
    ]
    library.opus_decode_float.restype = ctypes.c_int
    library.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
    error = ctypes.c_int()
    decoder = library.opus_decoder_create(48000, 1, ctypes.byref(error))
    assert decoder and error.value == 0
    result = []
    try:
        for payload in packets:
            pcm = (ctypes.c_float * 960)()
            count = library.opus_decode_float(decoder, payload, len(payload), pcm, 960, 0)
            assert count == 960
            result.append(np.ctypeslib.as_array(pcm).copy())
    finally:
        library.opus_decoder_destroy(decoder)
    return np.concatenate(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("arrival", ["regular", "600ms_burst", "1200ms_burst"])
async def test_native_live_reuses_bursts_without_reset_discard_cascade(arrival):
    count = 41 if arrival == "1200ms_burst" else 31
    source = source_packets(count)
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.setblocking(False)
    receiver.bind(("127.0.0.1", 0))
    queued = asyncio.Queue()
    adapters, received, failures = [], [], []
    key, salt = b"A" * 16, b"S" * 14

    class MeasuredAdapter(AudioAdapter):
        """Test-only logical timeline ledger, not a resampler support claim."""

        input_samples = output_samples = 0
        first_input = first_output = output_end = None

        async def feed(self, payload, timestamp, *, capture_epoch):
            result = await super().feed(payload, timestamp, capture_epoch=capture_epoch)
            if self.first_input is None:
                self.first_input = timestamp
            self.input_samples += parse_opus(payload).duration_samples
            for output in result:
                if self.first_output is None:
                    self.first_output = output.timestamp
                if self.output_end is not None:
                    assert output.timestamp == self.output_end
                self.output_samples += output.duration
                self.output_end = output.timestamp + output.duration
            return result

    def factory(configuration):
        adapter = MeasuredAdapter(configuration)
        adapters.append(adapter)
        return adapter

    class Handle:
        async def read_audio(self, *, timeout):
            try:
                packets = await asyncio.wait_for(queued.get(), timeout)
            except TimeoutError:
                packets = ()
            return SimpleNamespace(packets=packets, capture_epoch=1, resynchronized=False)

    track = AudioTrack(
        AudioEndpointRequest(receiver.getsockname()[1], key, salt),
        bind=("127.0.0.1", 0),
        endpoint=receiver.getsockname(),
        family=socket.AF_INET,
        excluded_ssrc=1,
        check=lambda _permission: None,
        epoch=lambda: 1,
        controller=None,
        boot=None,
        scope=None,
        failure=failures.append,
        presentation=lambda _kind, _timestamp, receipt: receipt,
        touch_feedback=lambda _value: None,
        last_feedback=time.monotonic,
        adapter_factory=factory,
    )
    decoder = pylibsrtp.Session(
        pylibsrtp.Policy(
            key=key + salt,
            ssrc_type=pylibsrtp.Policy.SSRC_SPECIFIC,
            ssrc_value=track.prepared.ssrc,
        )
    )

    async def read_udp():
        while True:
            packet = await asyncio.get_running_loop().sock_recv(receiver, 4096)
            if 192 <= packet[1] <= 223:
                decoder.unprotect_rtcp(packet)
            else:
                received.append(RtpPacket.parse(decoder.unprotect(packet)))

    def append(indices):
        now = time.monotonic()
        queued.put_nowait(
            tuple(
                SimpleNamespace(
                    payload=source[index],
                    rtp_timestamp=index * 2880,
                    received_at=now,
                    capture_epoch=1,
                )
                for index in indices
            )
        )

    drainer = asyncio.create_task(read_udp())
    try:
        track.start(Handle(), AudioConfiguration(24000, 20, "vbr", 24), speaker_volume=100)
        append((0,))
        started = time.monotonic()
        if arrival == "regular":
            for index in range(1, 31):
                await asyncio.sleep(max(0, started + index * 0.06 - time.monotonic()))
                append((index,))
        else:
            size = 20 if arrival == "1200ms_burst" else 10
            for burst in range((count - 1) // size):
                await asyncio.sleep(max(0, started + (burst + 1) * size * 0.06 - time.monotonic()))
                append(range(1 + burst * size, 1 + (burst + 1) * size))
        await asyncio.sleep(0.55)
        assert not failures and track.failure is None
        minimum = 85 if arrival == "regular" else (35 if arrival == "1200ms_burst" else 45)
        assert track.sent_packets == len(received) >= minimum
        values = track.diagnostics()["metrics"]["counters"]
        assert values["source_read_packets"] == count
        assert values["source_initial_skipped_samples"] == 0
        assert values["source_prefix_discarded_samples"] == 0
        assert values["source_unprocessed_samples"] == 0
        assert values["output_unvisited_samples"] == 0
        assert values["adapter_recreates"] <= (0 if arrival == "regular" else 3)
        assert values["source_read_samples"] == (
            values["source_submitted_samples"] + values["source_stale_discarded_samples"]
        )
        assert not track._outputs
        assert values["output_generated_samples"] == sum(
            values[f"output_{name}_samples"]
            for name in (
                "sent",
                "stale",
                "overflow",
                "epoch",
                "send_fenced",
                "retired",
                "unvisited",
                "unfinished",
            )
        )
        # Explain logical conversion tails/priming separately from queue loss.
        # This neither measures FIR support nor upgrades unknown production
        # native_pending_samples into a proved contribution/freshness boundary.
        assert values["source_submitted_samples"] == sum(a.input_samples for a in adapters)
        priming = sum(
            max(0, a.first_input - a.first_output) for a in adapters if a.first_output is not None
        )
        tails = [
            a.input_samples
            + (max(0, a.first_input - a.first_output) if a.first_output is not None else 0)
            - a.output_samples
            for a in adapters
        ]
        assert all(tail >= 0 for tail in tails)
        assert values["source_submitted_samples"] + priming == (
            values["output_generated_samples"] + sum(tails)
        )
        assert track.diagnostics()["native_pending_samples"] is None
        maximum = track.diagnostics()["metrics"]["maxima"]
        assert maximum["retained_output_packets"] <= 32
        assert maximum["retained_output_bytes"] <= 40 * 1024
        assert all(parse_opus(packet.payload).duration_samples == 960 for packet in received)
        assert all(
            (b.sequence_number - a.sequence_number) & 65535 == 1
            for a, b in zip(received, received[1:], strict=False)
        )
        steps = [
            (b.timestamp - a.timestamp) & 0xFFFFFFFF
            for a, b in zip(received, received[1:], strict=False)
        ]
        assert all(0 < step < 2**31 for step in steps)
        if arrival == "regular":
            assert set(steps) == {480}
        pcm = decode_every_packet(packet.payload for packet in received)
        assert len(pcm) == len(received) * 960
        assert float(np.sqrt(np.mean(pcm * pcm))) > 0.001
    finally:
        await track.close()
        drainer.cancel()
        await asyncio.gather(drainer, return_exceptions=True)
        receiver.close()
    assert all(adapter._closed and adapter._worker is None for adapter in adapters)
    assert all(task.done() for task in (*track._tasks, *track._output_tasks))
    assert track._output_bytes == 0 and not track._outputs
