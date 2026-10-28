"""Ring-style SILK is checked against the independent reference decoder API."""

import ctypes
import ctypes.util
from fractions import Fraction

import av
import numpy as np
import pytest

from halo_server.media.homekit_audio_codec import AudioWorker
from halo_server.media.opus import AudioConfiguration, parse_opus, repacketize


@pytest.fixture(scope="module")
def silk_wideband_60ms():
    encoder = av.CodecContext.create("libopus", "w")
    encoder.thread_count = 1
    encoder.sample_rate, encoder.layout, encoder.format = 16000, "mono", "s16"
    encoder.bit_rate, encoder.time_base = 16000, Fraction(1, 16000)
    encoder.options = {"application": "voip", "frame_duration": "60", "vbr": "off"}
    encoder.open()
    packets = []
    for index in range(12):
        t = (np.arange(960) + index * 960) / 16000
        pcm = (4000 * np.sin(2 * np.pi * 220 * t) + 2500 * np.sin(2 * np.pi * 880 * t)).astype(
            "int16"
        )
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate, frame.pts, frame.time_base = 16000, index * 960, Fraction(1, 16000)
        # Strip optional CBR padding without changing any compressed SILK data.
        packets.extend(repacketize([bytes(packet)]) for packet in encoder.encode(frame))
    assert len(packets) == 12
    for payload in packets:
        parsed = parse_opus(payload)
        assert payload[0] == 0x58
        assert parsed.channels == 1 and parsed.bandwidth_rate == 16000
        assert parsed.frame_samples == parsed.duration_samples == 2880
        assert len(parsed.frames) == 1
    return tuple(packets)


def reference_pcm(packets):
    """Use the public libopus C API, independently of PyAV/FFmpeg decoding."""
    name = ctypes.util.find_library("opus")
    if name is None:
        pytest.skip("Independent libopus decoder oracle is unavailable")
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
            pcm = (ctypes.c_float * 5760)()
            count = library.opus_decode_float(decoder, payload, len(payload), pcm, 5760, 0)
            assert count == 2880
            result.append(np.ctypeslib.as_array(pcm)[:count].copy())
    finally:
        library.opus_decoder_destroy(decoder)
    return np.concatenate(result)


@pytest.mark.asyncio
async def test_ring_silk_pcm_matches_reference_without_native_decoder_corruption(
    silk_wideband_60ms,
):
    reference = reference_pcm(silk_wideband_60ms)
    worker = AudioWorker(AudioConfiguration(24000, 20, "cbr", 24), mode="decode")
    output = []
    origin = 2_000_000_000
    try:
        for index, payload in enumerate(silk_wideband_60ms):
            output.extend(await worker.feed(payload, origin + index * 2880))
    finally:
        await worker.close()
    assert worker.pid is None
    assert [pts for pts, _duration, _pcm in output] == list(
        range(origin, origin + len(silk_wideband_60ms) * 2880, 960)
    )
    assert all(duration == 960 and len(pcm) == 1920 for _pts, duration, pcm in output)
    actual = np.frombuffer(b"".join(pcm for _pts, _duration, pcm in output), dtype="<i2") / 32768
    assert actual.shape == reference.shape
    # Quantization can differ by a few S16 units across libopus versions. The
    # native decoder diverges much more on this real SILK frame configuration.
    assert float(np.sqrt(np.mean((actual - reference) ** 2))) < 5 / 32768
    assert float(np.corrcoef(actual, reference)[0, 1]) > 0.9999


@pytest.mark.asyncio
async def test_ring_silk_converts_to_selected_20ms_audio_with_intact_signal(silk_wideband_60ms):
    worker = AudioWorker(AudioConfiguration(24000, 20, "cbr", 24))
    output = []
    try:
        for index, payload in enumerate(silk_wideband_60ms):
            output.extend(await worker.feed(payload, index * 2880))
        output.extend(await worker.flush())
    finally:
        await worker.close()
    parsed = [parse_opus(payload) for _pts, _duration, payload in output]
    assert all(item.channels == 1 and item.duration_samples == 960 for item in parsed)
    assert all(len(payload) <= 60 for _pts, _duration, payload in output)
    assert all(b[0] - a[0] == 960 for a, b in zip(output, output[1:], strict=False))
    # Decode independently through the public C API, using its native packet
    # duration rather than reusing Halo's packet parser or PyAV decoder.
    pcm = reference_pcm_20ms(payload for _pts, _duration, payload in output)
    pcm = pcm[4800:-4800]
    spectrum = np.abs(np.fft.rfft(pcm))
    frequencies = np.fft.rfftfreq(len(pcm), 1 / 48000)
    for expected in (220, 880):
        band = np.abs(frequencies - expected) < 40
        peak = frequencies[band][np.argmax(spectrum[band])]
        assert abs(peak - expected) < 5
    assert 0.03 < float(np.sqrt(np.mean(pcm**2))) < 0.2


def reference_pcm_20ms(packets):
    # A 20 ms packet can be grouped into the oracle's 60 ms input without
    # changing compressed frames, while retaining an independent PCM decoder.
    packets = list(packets)
    complete = len(packets) // 3 * 3
    return reference_pcm(repacketize(packets[index : index + 3]) for index in range(0, complete, 3))
