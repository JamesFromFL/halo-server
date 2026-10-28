"""Native Opus, original-byte delivery, timing, gain and process retirement."""

import asyncio
import signal
from fractions import Fraction

import av
import numpy as np
import pytest
from test_media_opus import oracle

from halo_server.media import homekit_audio_codec as codec
from halo_server.media.homekit_audio_codec import (
    AudioAdapter,
    AudioDecoder,
    AudioTransformError,
    AudioWorker,
)
from halo_server.media.opus import AudioConfiguration, OpusError, parse_opus


def source_audio(
    *, rate=48000, duration=20, mode="cbr", channels=1, count=12, gain=12000, edge_tones=False
):
    encoder = av.CodecContext.create("libopus", "w")
    encoder.sample_rate = rate
    encoder.layout = "mono" if channels == 1 else "stereo"
    encoder.format = "s16"
    encoder.bit_rate = 24000
    encoder.time_base = Fraction(1, rate)
    encoder.options = {
        "application": "lowdelay",
        "frame_duration": str(duration),
        "vbr": "off" if mode == "cbr" else "constrained",
    }
    encoder.open()
    result = []
    for index in range(count):
        size = rate * duration // 1000
        t = (np.arange(size) + index * size) / rate
        frequency = 440 if not edge_tones or index < 2 else (1000 if index >= count - 2 else 0)
        data = (np.sin(2 * np.pi * frequency * t) * gain).astype("int16")
        if channels == 2:
            data = np.column_stack((data, data)).reshape(-1)
        frame = av.AudioFrame.from_ndarray(data.reshape(1, -1), format="s16", layout=encoder.layout)
        frame.sample_rate = rate
        frame.pts = index * size
        frame.time_base = Fraction(1, rate)
        result.extend(bytes(packet) for packet in encoder.encode(frame))
    return result


def decode_audio(packets):
    decoder = av.CodecContext.create("opus", "r")
    decoder.thread_count = 1
    decoder.extradata = b"OpusHead\x01\x01\0\0" + (48000).to_bytes(4, "little") + b"\0\0\0"
    frames = [frame for payload in packets for frame in decoder.decode(av.Packet(payload))]
    return frames


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [8000, 16000, 24000])
@pytest.mark.parametrize("duration", [20, 30, 40, 60])
@pytest.mark.parametrize("mode", ["cbr", "vbr"])
async def test_every_advertised_audio_rate_duration_mode_is_actually_encoded_parsed_and_decoded(
    rate, duration, mode
):
    worker = AudioWorker(AudioConfiguration(rate, duration, mode, 24))
    result = []
    try:
        for index, payload in enumerate(source_audio(count=9)):
            result.extend(await worker.feed(payload, index * 960))
    finally:
        await worker.close()
    assert result and worker.pid is None
    parsed = [parse_opus(payload) for _, _, payload in result]
    assert all(
        packet.duration_samples == duration * 48
        and packet.channels == 1
        and packet.bandwidth_rate <= rate
        for packet in parsed
    )
    assert all(
        oracle()(payload) == (packet.toc, packet.frames)
        for (_, _, payload), packet in zip(result, parsed, strict=True)
    )
    assert all(len(payload) <= 1178 for _, _, payload in result)
    assert [pts for pts, _, _ in result] == list(
        range(result[0][0], result[0][0] + len(result) * duration * 48, duration * 48)
    )
    frames = decode_audio(payload for _, _, payload in result)
    assert sum(frame.samples for frame in frames) == len(result) * duration * 48
    assert all(frame.sample_rate == 48000 and len(frame.layout.channels) == 1 for frame in frames)
    assert max(float(np.max(np.abs(frame.to_ndarray()))) for frame in frames) > 0.05
    if mode == "cbr":
        assert len({len(payload) for _, _, payload in result}) == 1


@pytest.mark.asyncio
async def test_copy_preserves_source_bytes_and_repacketization_preserves_compressed_frames():
    packets = source_audio(rate=16000, count=6)
    copy = AudioAdapter(AudioConfiguration(16000, 20, "cbr", 24))
    group = AudioAdapter(AudioConfiguration(16000, 60, "vbr", 24))
    try:
        for index, payload in enumerate(packets):
            result = await copy.feed(payload, index * 960, capture_epoch=5)
            assert len(result) == 1 and result[0].payload == payload and result[0].mode == "copy"
        outputs = []
        for index, payload in enumerate(packets):
            outputs.extend(await group.feed(payload, index * 960, capture_epoch=5))
        assert len(outputs) == 2 and all(item.mode == "repacketize" for item in outputs)
        assert tuple(
            frame for item in outputs for frame in parse_opus(item.payload).frames
        ) == tuple(frame for data in packets for frame in parse_opus(data).frames)
        assert group._worker is None and copy._worker is None
    finally:
        await copy.close()
        await group.close()


@pytest.mark.asyncio
async def test_30ms_is_three_actual_10ms_frames_and_input_stereo_is_downmixed():
    adapter = AudioAdapter(AudioConfiguration(16000, 30, "vbr", 24))
    result = []
    try:
        for index, payload in enumerate(source_audio(channels=2, count=6)):
            result.extend(await adapter.feed(payload, index * 960))
        assert result and all(item.mode == "adapt" for item in result)
        assert all(
            len(parse_opus(item.payload).frames) == 3
            and parse_opus(item.payload).frame_samples == 480
            for item in result
        )
        assert all(
            len(frame.layout.channels) == 1
            for frame in decode_audio(item.payload for item in result)
        )
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bitrate", [6, 64])
@pytest.mark.parametrize("rate", [8000, 16000, 24000])
@pytest.mark.parametrize("duration", [20, 30, 40, 60])
@pytest.mark.parametrize("mode", ["cbr", "vbr"])
async def test_selected_bitrate_boundaries_are_supported_by_actual_native_encoder(
    bitrate, rate, duration, mode
):
    worker = AudioWorker(AudioConfiguration(rate, duration, mode, bitrate))
    result = []
    try:
        for index in range(6):
            result.extend(await worker.feed(b"\xf8\xff\xfe", index * 960))
        assert result and all(len(payload) * 8 <= bitrate * duration for _, _, payload in result)
    finally:
        await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bitrate", [7, 13, 63])
@pytest.mark.parametrize("duration", [20, 30, 60])
async def test_cbr_fractional_byte_budget_is_rounded_down_instead_of_exceeding_selection(
    bitrate, duration
):
    worker = AudioWorker(AudioConfiguration(24000, duration, "cbr", bitrate))
    result = []
    try:
        for index, payload in enumerate(source_audio(count=6)):
            result.extend(await worker.feed(payload, index * 960))
        assert result and all(len(data) * 8 <= bitrate * duration for _, _, data in result)
        assert len({len(data) for _, _, data in result}) == 1
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_native_gain_and_exact_return_frames_and_epoch_reset_never_emit_old_pcm():
    packets = source_audio(count=4)
    loud = AudioDecoder(gain=100)
    quiet = AudioDecoder(gain=25)
    muted = AudioDecoder(gain=0)
    try:
        a = await loud.feed(packets[0], capture_epoch=1)
        b = await quiet.feed(packets[0], capture_epoch=1)
        c = await muted.feed(packets[0], capture_epoch=1)
        assert len(a) == len(b) == len(c) == 1 and all(len(data) == 1920 for data in a + b + c)
        original = np.frombuffer(a[0], dtype="<i2").astype("int32")
        scaled = np.frombuffer(b[0], dtype="<i2").astype("int32")
        assert np.max(np.abs(scaled - original / 4)) <= 1
        assert not any(c[0])
        first = muted._worker.pid
        c = await muted.feed(packets[1], capture_epoch=2)
        assert muted._worker.pid != first and not any(c[0])
        assert np.max(np.abs(original)) <= 32767
    finally:
        await loud.close()
        await quiet.close()
        await muted.close()


@pytest.mark.asyncio
async def test_ten_ms_return_frames_are_combined_without_losing_partial_first_syllable():
    packets = source_audio(duration=10, count=4)
    decoder = AudioDecoder()
    try:
        assert await decoder.feed(packets[0], capture_epoch=1) == ()
        out = await decoder.feed(packets[1], capture_epoch=1)
        assert len(out) == 1 and len(out[0]) == 1920
        assert np.max(np.abs(np.frombuffer(out[0], dtype="<i2"))) > 1000
        assert (
            await decoder.feed(packets[2], capture_epoch=2) == ()
        )  # stale half-frame was retired.
    finally:
        await decoder.close()


@pytest.mark.asyncio
async def test_explicit_qualified_drain_keeps_initial_and_final_tones_without_container_preskip():
    adapter = AudioAdapter(AudioConfiguration(16000, 30, "vbr", 24, gain=90))
    result = []
    try:
        for index, payload in enumerate(source_audio(count=10, edge_tones=True)):
            result.extend(await adapter.feed(payload, index * 960))
        result.extend(await adapter.flush())
        data = np.concatenate(
            [
                frame.to_ndarray().reshape(-1)
                for frame in decode_audio(item.payload for item in result)
            ]
        )
        assert data.size >= 10 * 960
        first, middle, last = data[720:1440], data[4800:5760], data[8400:9360]
        assert np.sqrt(np.mean(first**2)) > 0.05
        assert np.sqrt(np.mean(middle**2)) < 0.01
        assert np.sqrt(np.mean(last**2)) > 0.05

        def peak(samples):
            return np.fft.rfftfreq(len(samples), 1 / 48000)[np.argmax(np.abs(np.fft.rfft(samples)))]

        assert abs(peak(first) - 440) < 80 and abs(peak(last) - 1000) < 80
        assert result[0].timestamp == -120  # actual2.5ms lowdelay lookahead, not fabricated PTS.
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_silence_has_full_packet_cadence_and_no_claimed_unused_dtx_option():
    worker = AudioWorker(AudioConfiguration(24000, 20, "vbr", 24))
    try:
        result = [await worker.feed(b"\xf8\xff\xfe", index * 960) for index in range(30)]
        assert len(result[0]) == 0  # finite resampler startup latency, not DTX.
        assert all(len(batch) == 1 for batch in result[1:])
        assert all(parse_opus(batch[0][2]).duration_samples == 960 for batch in result[1:])
    finally:
        await worker.close()


HUNG = """import signal,sys,time,struct
signal.signal(signal.SIGTERM,signal.SIG_IGN)
sys.stdin.buffer.read(4)
sys.stdout.buffer.write(struct.pack('!I',1)+b'S');sys.stdout.buffer.flush()
while True: time.sleep(1)
"""


@pytest.mark.asyncio
async def test_hung_audio_is_killed_reaped_and_releases_capacity(tmp_path, monkeypatch):
    script = tmp_path / "hung.py"
    script.write_text(HUNG)
    real = asyncio.create_subprocess_exec
    spawned = []

    async def spawn(*args, **kwargs):
        child = await real(args[0], args[1], str(script), **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(codec.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(codec, "FRAME_SECONDS", 0.03)
    monkeypatch.setattr(codec, "STOP_SECONDS", 0.03)
    initial = codec._WORKERS
    worker = AudioWorker(AudioConfiguration(16000, 20, "vbr", 24))
    try:
        with pytest.raises(TimeoutError):
            await worker.feed(b"\xf8\xff\xfe", 0)
        assert spawned[0].returncode == -signal.SIGKILL and initial == codec._WORKERS
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_failed_reap_retains_claim_until_retry_and_blocks_new_worker(monkeypatch):
    monkeypatch.setattr(codec, "MAX_WORKERS", 1)
    monkeypatch.setattr(codec, "STOP_SECONDS", 0.01)
    worker = AudioWorker(AudioConfiguration(16000, 20, "vbr", 24))
    blocked = AudioWorker(worker.configuration)
    initial = codec._WORKERS
    await worker.start()
    process = worker._process
    real = process.wait

    async def hang():
        await asyncio.Event().wait()

    monkeypatch.setattr(process, "wait", hang)
    try:
        with pytest.raises(AudioTransformError):
            await worker.close()
        assert worker in codec._RETIRING_WORKERS and initial + 1 == codec._WORKERS
        with pytest.raises(AudioTransformError):
            await blocked.start()
        monkeypatch.setattr(process, "wait", real)
        await codec.retire_audio_workers()
        assert worker not in codec._RETIRING_WORKERS and initial == codec._WORKERS
    finally:
        monkeypatch.setattr(process, "wait", real)
        await worker.close()
        await blocked.close()


@pytest.mark.asyncio
async def test_malformed_native_input_is_rejected_before_process_allocation():
    worker = AudioWorker(AudioConfiguration(16000, 20, "vbr", 24))
    initial = codec._WORKERS
    try:
        with pytest.raises(OpusError):
            await worker.feed(b"\xfb", 0)
        assert worker.pid is None and initial == codec._WORKERS
    finally:
        await worker.close()


def test_pcm_gain_rounding_and_clipping_have_no_signed_integer_wrap():
    from halo_server.media.audio_worker import apply_gain

    source = np.array([-32768, -32000, -3, 0, 3, 32000, 32767], dtype="<i2")
    assert apply_gain(source.tobytes(), 100) == source.tobytes()
    assert not any(apply_gain(source.tobytes(), 0))
    assert np.frombuffer(apply_gain(source.tobytes(), 200), dtype="<i2").tolist() == [
        -32768,
        -32768,
        -6,
        0,
        6,
        32767,
        32767,
    ]


@pytest.mark.asyncio
async def test_source_epoch_change_discards_partial_compressed_group():
    source = source_audio(rate=16000, count=6)
    adapter = AudioAdapter(AudioConfiguration(16000, 60, "vbr", 24))
    try:
        assert await adapter.feed(source[0], 0, capture_epoch=1) == ()
        assert await adapter.feed(source[1], 960, capture_epoch=1) == ()
        assert await adapter.feed(source[2], 0, capture_epoch=2) == ()
        assert await adapter.feed(source[3], 960, capture_epoch=2) == ()
        out = await adapter.feed(source[4], 1920, capture_epoch=2)
        assert len(out) == 1 and out[0].timestamp == 0
        assert parse_opus(out[0].payload).frames == tuple(
            frame for payload in source[2:5] for frame in parse_opus(payload).frames
        )
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_late_audio_process_creation_is_joined_before_reservation_release(monkeypatch):
    real = asyncio.create_subprocess_exec
    entered = asyncio.Event()
    release = asyncio.Event()
    spawned = []

    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        child = await real(*args, **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(codec.asyncio, "create_subprocess_exec", spawn)
    initial = codec._WORKERS
    worker = AudioWorker(AudioConfiguration(16000, 20, "vbr", 24))
    starting = asyncio.create_task(worker.start())
    try:
        await entered.wait()
        closing = asyncio.create_task(worker.close())
        await asyncio.sleep(0)
        assert not closing.done() and initial + 1 == codec._WORKERS
        release.set()
        await asyncio.wait_for(closing, 2)
        result = await asyncio.gather(starting, return_exceptions=True)
        assert isinstance(result[0], Exception) and spawned[0].returncode is not None
        assert initial == codec._WORKERS
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)
        await worker.close()


@pytest.mark.asyncio
async def test_cancellation_during_native_decode_cannot_return_late_pcm(tmp_path, monkeypatch):
    script = tmp_path / "hung.py"
    script.write_text(HUNG)
    real = asyncio.create_subprocess_exec
    spawned = []

    async def spawn(*args, **kwargs):
        child = await real(args[0], args[1], str(script), **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(codec.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(codec, "STOP_SECONDS", 0.03)
    decoder = AudioDecoder()
    task = asyncio.create_task(decoder.feed(b"\xf8\xff\xfe"))
    try:
        while not spawned or decoder._worker._process is None:
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spawned[0].returncode is not None
    finally:
        await decoder.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["adapter", "decoder"])
async def test_close_during_epoch_reset_cannot_create_a_worker_after_retirement(monkeypatch, kind):
    entered, release = asyncio.Event(), asyncio.Event()

    class OldWorker:
        calls = 0

        async def close(self):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                await release.wait()

    old = OldWorker()
    owner = (
        AudioAdapter(AudioConfiguration(16000, 20, "vbr", 24, gain=50))
        if kind == "adapter"
        else AudioDecoder()
    )
    owner._worker = old
    owner._epoch = 1

    def forbidden(*_args, **_kwargs):
        pytest.fail("A new native worker was created after close returned")

    monkeypatch.setattr(codec, "AudioWorker", forbidden)
    task = asyncio.create_task(
        owner.feed(b"\xf8\xff\xfe", 0, capture_epoch=2)
        if kind == "adapter"
        else owner.feed(b"\xf8\xff\xfe", capture_epoch=2)
    )
    try:
        await entered.wait()
        await owner.close()
        release.set()
        with pytest.raises(AudioTransformError):
            await task
        assert old.calls == 2 and owner._worker is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await owner.close()
