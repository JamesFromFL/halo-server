"""Codec source-time accounting and finite independent local wire bounds."""

import math

import pytest

from halo_server.media.video_budget import (
    MAX_AU_PACKETS,
    MAX_AU_WIRE_BYTES,
    MAX_CODEC_ROWS,
    CodecRateMonitor,
    VideoBudgetError,
    VideoWirePolicy,
)


@pytest.mark.parametrize(
    "selected,peak",
    [(1, 2000000), (132, 2000000), (299, 2000000), (600, 2400000), (20000, 64000000)],
)
def test_selected_codec_limit_is_not_the_protected_wire_peak(selected, peak):
    policy = VideoWirePolicy(selected)
    assert policy.peak_bps == peak
    assert policy.service(1200) == 1200 * 8 / peak


def test_large_reference_au_fits_without_spending_codec_budget_on_srtp_overhead():
    policy = VideoWirePolicy(299)
    lengths = (1200,) * 15 + (942,)
    result = policy.reserve(lengths, now=10.1, next_packet_at=10, deadline=10.5)
    assert result.protected_bytes == 18942 and result.packets == 16
    assert result.finish == pytest.approx(10.1 + 18942 * 8 / 2000000)
    assert 10 + 18942 * 8 / 299000 > 10.5  # The former conflated model rejects it.
    assert policy.reserve(lengths, now=10.4, next_packet_at=10.45, deadline=10.5) is None


def test_idle_time_never_banks_unbounded_tokens_or_waives_pending_work():
    policy = VideoWirePolicy(132)
    result = policy.reserve((1200,), now=1000, next_packet_at=0, deadline=1000.5)
    assert result.start == 1000 and result.finish > 1000
    assert policy.reserve((1200,), now=1000, next_packet_at=1000.5, deadline=1000.5) is None


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, 1e300])
def test_invalid_clock_metadata_cannot_admit_egress(value):
    with pytest.raises(VideoBudgetError):
        VideoWirePolicy(299).reserve((1200,), now=value, next_packet_at=0, deadline=1000)


def test_count_bytes_and_protected_datagram_limits_are_independent():
    policy = VideoWirePolicy(299)
    for lengths in ((), (1,) * (MAX_AU_PACKETS + 1), (1201,), (True,), (0,)):
        with pytest.raises(VideoBudgetError):
            policy.reserve(lengths, now=0, next_packet_at=0, deadline=100)
    lengths = (1200,) * (MAX_AU_WIRE_BYTES // 1200 + 1)
    assert len(lengths) < MAX_AU_PACKETS
    with pytest.raises(VideoBudgetError):
        policy.reserve(lengths, now=0, next_packet_at=0, deadline=100)


def observe(monitor, pts, size=100, *, epoch=1, generation=1, selected=8):
    monitor.observe(
        epoch=epoch,
        generation=generation,
        selected_kbps=selected,
        source_pts=pts,
        encoded_bytes=size,
    )


def test_codec_windows_use_source_pts_not_burst_receipt_or_protected_packet_bytes():
    monitor = CodecRateMonitor()
    assert monitor.snapshot()["window_payload_bits"] is None
    for index in range(11):
        observe(monitor, index * 9000)
    snapshot = monitor.snapshot()
    assert snapshot["complete_windows"] == 1
    assert snapshot["window_payload_bits"] == snapshot["peak_window_payload_bits"] == 8000
    assert snapshot["above_selected_windows"] == 0
    observe(monitor, 99000, 101)
    assert monitor.snapshot()["above_selected_windows"] == 1
    assert monitor.snapshot()["window_payload_bits"] == 8008
    assert not any(isinstance(value, (bytes, bytearray)) for value in monitor.__dict__.values())


def test_startup_is_partial_and_duplicate_pts_counts_all_original_encoded_bytes():
    monitor = CodecRateMonitor()
    observe(monitor, 0, 600)
    observe(monitor, 0, 600)
    snapshot = monitor.snapshot()
    assert snapshot["complete_windows"] == 0 and snapshot["above_selected_windows"] == 1
    assert snapshot["window_payload_bits"] == 9600
    assert len(monitor._rows) == 1


def test_true_epoch_or_configuration_changes_reset_measurement_not_the_wire_clock():
    monitor = CodecRateMonitor()
    observe(monitor, 0, 1500)
    observe(monitor, 90000)
    assert monitor.snapshot()["complete_windows"] == 1
    observe(monitor, 90000, generation=2, selected=299)
    assert monitor.snapshot()["measurement_resets"] == 1
    assert monitor.snapshot()["selected_bps"] == 299000
    assert monitor.snapshot()["complete_windows"] == 0
    observe(monitor, 0, epoch=2, generation=2, selected=299)
    assert monitor.snapshot()["measurement_resets"] == 2


def test_backwards_pts_and_pathological_sample_density_cannot_fake_complete_coverage():
    monitor = CodecRateMonitor()
    observe(monitor, 0)
    observe(monitor, 90000)
    observe(monitor, 45000)
    assert monitor.snapshot()["unknown_windows"] == 1
    previous_complete = monitor.snapshot()["complete_windows"]
    for index in range(MAX_CODEC_ROWS + 2):
        observe(monitor, 45001 + index)
    assert len(monitor._rows) <= MAX_CODEC_ROWS
    assert monitor.snapshot()["unknown_windows"] == 2
    assert monitor.snapshot()["complete_windows"] == previous_complete
    assert math.isfinite(monitor.snapshot()["window_payload_bits"])
