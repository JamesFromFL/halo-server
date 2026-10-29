"""Relative sender-clock evidence, including wrap, gaps and unknown coverage."""

import json
from types import SimpleNamespace as NS

import pytest
from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from test_media_ingress_diagnostics import session_fixture
from test_receiver_metrics import audio_queue, packet, receivers

from halo_server.media.receiver_metrics import install_rtp_observer
from halo_server.media.send_clock import MODULUS, TICKS, SendClock, safe_send_clock


def sample(sequence, stamp, source=123):
    return NS(sequence_number=sequence, ssrc=source, extensions=NS(abs_send_time=stamp))


def counts(owner):
    return owner.snapshot()["counters"]


def timer(owner, name):
    return owner.snapshot()["timings"][name]


def test_same_delayed_arrival_has_different_evidence_before_and_after_sender_stamp():
    after, before = SendClock(), SendClock()
    for owner, send_seconds in ((after, 0.06), (before, 0.7)):
        owner.observe("video", sample(5, 12345), 100)
        owner.observe("video", sample(6, 12345 + round(send_seconds * TICKS)), 100.7)
        assert counts(owner)["video_gap_compared"] == 1
        assert timer(owner, "video_gap_arrival_interval")["sum_us"] == 700000
    assert 639995 <= timer(after, "video_gap_arrival_excess")["sum_us"] <= 640005
    assert timer(before, "video_gap_arrival_excess")["sum_us"] <= 4
    # Arrival excess is a relative boundary comparison, not a measured latency.
    after.observe("video", sample(7, 12345 + round(0.12 * TICKS)), 100.701)
    assert 58995 <= timer(after, "video_send_excess")["sum_us"] <= 59005
    assert counts(after)["video_compared"] == 2


def test_clock_and_sequence_wrap_compare_consecutive_packets_without_reset():
    owner = SendClock()
    owner.observe("video", sample(65535, MODULUS - 6000), 100)
    owner.observe("video", sample(0, 6000), 100 + 12000 / TICKS)
    assert counts(owner)["video_compared"] == 1
    assert counts(owner)["video_clock_reset"] == 0
    assert timer(owner, "video_arrival_excess")["sum_us"] == 0
    assert timer(owner, "video_send_excess")["sum_us"] == 0


@pytest.mark.parametrize("stamp", [True, -1, MODULUS, 0.5, "private-value"])
def test_invalid_stamp_clears_reference_and_never_exports_value(stamp):
    owner = SendClock()
    owner.observe("audio", sample(0, 1000), 1)
    owner.observe("audio", sample(1, stamp), 2)
    owner.observe("audio", sample(2, 2000), 3)
    assert counts(owner)["audio_invalid"] == 1
    assert counts(owner)["audio_compared"] == 0
    assert "private-value" not in json.dumps(owner.snapshot())


def test_absence_is_unknown_and_breaks_comparison_instead_of_a_zero_sender_clock():
    owner = SendClock()
    owner.observe("audio", sample(0, 1000), 1)
    owner.observe("audio", sample(1, None), 2)
    owner.observe("audio", sample(2, 2000), 3)
    assert counts(owner)["audio_absent"] == 1
    assert counts(owner)["audio_present"] == 2
    assert counts(owner)["audio_compared"] == 0


@pytest.mark.parametrize("sequence,source", [(1, 123), (3, 123), (0, 123), (2, 456)])
def test_duplicate_loss_reorder_or_source_change_is_not_treated_as_a_delivery_gap(sequence, source):
    owner = SendClock()
    owner.observe("video", sample(1, 1000), 1)
    owner.observe("video", sample(sequence, 2000, source), 2)
    assert counts(owner)["video_sequence_discontinuity"] == 1
    assert counts(owner)["video_compared"] == 0


@pytest.mark.parametrize("stamp,now", [(999, 2), (1000 + 11 * TICKS, 2), (2000, 0), (2000, 12)])
def test_ambiguous_clock_jump_resets_instead_of_claiming_network_delay(stamp, now):
    owner = SendClock()
    owner.observe("video", sample(1, 1000), 1)
    owner.observe("video", sample(2, stamp), now)
    assert counts(owner)["video_clock_reset"] == 1
    assert counts(owner)["video_compared"] == 0


def test_only_two_numeric_references_retained_and_fence_stops_collection():
    owner = SendClock()
    for sequence in range(10000):
        for kind in ("audio", "video"):
            owner.observe(kind, sample(sequence, sequence * 1000 % MODULUS), sequence / 1000)
    assert len(owner._previous) == 2
    assert all(type(x) in (int, float) for row in owner._previous.values() for x in row)
    before = owner.metrics.snapshot()
    owner.fence()
    owner.observe("video", sample(10000, 10000000), 10)
    assert not owner._previous and owner.snapshot() is None
    assert owner.metrics.snapshot() == before


def test_sanitizer_excludes_unknown_data_and_rejects_invalid_histograms():
    owner = SendClock()
    owner.observe("audio", sample(1, 1000), 1)
    value = owner.snapshot()
    value["secret"] = value["counters"]["secret"] = "private-value"
    value["timings"]["audio_arrival_interval"]["secret"] = "private-value"
    assert "private-value" not in json.dumps(safe_send_clock(value))
    value["timings"]["audio_arrival_interval"]["count"] = 1
    assert safe_send_clock(value) is None


@pytest.mark.asyncio
async def test_real_receiver_delivery_and_session_retirement_preserved_with_extension():
    now = [10.0]
    audio, _, pipeline = receivers(now)
    delivered = audio_queue(audio, pipeline)
    install_rtp_observer(audio, "audio", pipeline)
    for sequence in range(5):
        value = packet(sequence)
        value.extensions.abs_send_time = round(sequence * 0.06 * TICKS)
        now[0] = 10 + sequence * 0.06
        await audio._handle_rtp_packet(value, round(now[0] * 1000))
    assert len(delivered) == 1
    snapshot = pipeline.snapshot()
    assert snapshot["send_clock"]["counters"]["audio_compared"] == 4
    assert snapshot["send_clock"]["counters"]["video_present"] == 0
    session, _, _, callbacks = session_fixture()
    callbacks["on_receiver_stats"]({"pipeline": snapshot}, 0, 0)
    try:
        actual = session.diagnostics()["receiver_pipeline"]["send_clock"]
        assert actual == snapshot["send_clock"]
    finally:
        session.stop_all()
    assert session.diagnostics()["receiver_pipeline"] is None
    pipeline.fence()
    assert pipeline._send_clock.snapshot() is None


@pytest.mark.asyncio
async def test_optional_clock_observation_failure_does_not_change_original_handler():
    now = [10.0]
    audio, _, pipeline = receivers(now)
    delivered = audio_queue(audio, pipeline)
    install_rtp_observer(audio, "audio", pipeline)

    def broken(*_args):
        raise RuntimeError("private observation error")

    pipeline._send_clock.observe_receiver = broken
    for sequence in range(5):
        await audio._handle_rtp_packet(packet(sequence), 10000)
    assert len(delivered) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mime", [None, "audio/rtx", "audio/unknown"])
async def test_unknown_and_repair_payloads_do_not_become_primary_clock_evidence(mime):
    now = [10.0]
    audio, _, pipeline = receivers(now)
    if mime is not None:
        audio._RTCRtpReceiver__codecs[111] = RTCRtpCodecParameters(
            mimeType=mime, clockRate=48000, payloadType=111, parameters={"apt": 112}
        )
    # Keep the real pinned original handler and its early returns. Unknown
    # primary codecs cannot reach decode here because the receiver is disabled.
    audio._enabled = False
    install_rtp_observer(audio, "audio", pipeline)
    for sequence in range(2):
        value = packet(sequence)
        value.extensions.abs_send_time = round(sequence * 0.06 * TICKS)
        now[0] = 10 + sequence * 0.7
        await audio._handle_rtp_packet(value, round(now[0] * 1000))
    result = pipeline.snapshot()["send_clock"]
    assert result["counters"]["audio_excluded"] == 2
    assert result["counters"]["audio_present"] == 0
    assert result["counters"]["audio_gap_compared"] == 0
    assert not pipeline._send_clock._previous
