"""Persistent nominal-rate correction uses source time without holding real frames."""

from dataclasses import asdict

import av
import pytest
from test_video_wire_encoder import encode_moving_source

from halo_server.media.h264 import VideoConfiguration, parse_sps, split_annexb
from halo_server.media.transcode_worker import (
    RATE_COOLDOWN,
    RATE_GAP,
    RATE_MAINTENANCE_SPAN,
    RATE_SAMPLES,
    Encoder,
    RateCadence,
)


@pytest.mark.parametrize("initial", (13, 14, 16, 18))
def test_wrong_nominal_converges_to_sustained_irregular_fifteen_without_intermediate_rates(initial):
    rate = RateCadence(30)
    for index in range(initial * 2):
        stamp = index * 90000 // initial
        rate.observe(stamp)
    assert rate.target == initial
    start = stamp
    choices = []
    steps = (3600, 5400, 7200, 5400, 9000, 5400)  # Actual 15 fps; 40–100 ms steps.
    for index in range(240):
        stamp += steps[index % len(steps)]
        previous = rate.target
        rate.observe(stamp)
        if rate.target != previous:
            choices.append((stamp, rate.target))
        assert len(rate.samples) <= RATE_SAMPLES
    assert [target for _stamp, target in choices] == [15]
    assert choices[0][0] - start <= 11 * 90000
    assert rate.target == 15


def test_long_source_window_resists_short_irregular_windows_and_arrival_bursts():
    rate = RateCadence(30)
    for index in range(40):
        rate.observe(index * 6000)
    assert rate.target == 15
    stamp = 39 * 6000
    changed = rate.changed_at
    # Arrival timing is deliberately absent: only real source PTS enter this
    # class. This six-step pattern spans instantaneous 10–25 fps but averages 15.
    steps = (5400, 7200, 3600, 9000, 6000, 4800)
    for index in range(6000):
        stamp += steps[index % len(steps)]
        assert rate.observe(stamp) == 15
        assert len(rate.samples) <= RATE_SAMPLES
    assert rate.changed_at == changed


def test_true_rate_change_has_one_final_refresh_after_full_window_and_sustained_vote():
    rate = RateCadence(30)
    for index in range(46):
        rate.observe(index * 6000)
    assert rate.target == 15
    calibrated = rate.changed_at
    changes = []
    for index in range(1, 271):
        stamp = 270000 + index * 3000
        before = rate.target
        rate.observe(stamp)
        if rate.target != before:
            changes.append((stamp, rate.target))
    assert len(changes) == 1 and changes[0][1] == 30
    assert changes[0][0] - calibrated >= RATE_COOLDOWN
    assert changes[0][0] - 270000 >= RATE_MAINTENANCE_SPAN


def test_gap_boundary_and_full_source_span_do_not_count_one_gap_as_lower_cadence():
    rate = RateCadence(30)
    for index in range(40):
        stamp = index * 6000
        rate.observe(stamp)
    before = rate.changed_at
    stamp += RATE_GAP + 1
    assert rate.observe(stamp) == 15 and list(rate.samples) == [stamp]
    for index in range(1, 100):
        assert rate.observe(stamp + index * 6000) == 15
    assert rate.changed_at == before
    assert rate.samples[-1] - rate.samples[0] >= RATE_MAINTENANCE_SPAN
    exact = RateCadence(1)
    assert all(exact.observe(index * RATE_GAP) == 1 for index in range(10))
    assert exact.calibrated


@pytest.mark.parametrize("initial", (13, 18))
def test_native_wrong_nominal_correction_keeps_exact_pts_force_fifo_and_decoder(initial):
    config = VideoConfiguration(640, 360, 30, "high", "3.1", 132)
    source = encode_moving_source(15)
    worker = Encoder(asdict(config))
    decoder = worker.decoder
    independent = av.CodecContext.create("h264", "r")
    independent.thread_count = 1
    stamp = 0
    seed_frames = initial * 2
    refreshes, rows, tokens, decoded = [], [], [], 0
    last_refresh = 0
    signaled_rates = []
    for index in range(seed_frames + 165):
        if index:
            stamp += 90000 // initial if index < seed_frames else 6000
        token = index + 1 if index in {3, seed_frames + 95, seed_frames + 96} else 0
        before = worker.refreshes
        result = worker.feed(source[index % len(source)][1], stamp, token)
        assert len(result) == 1 and result[0][0] == stamp
        assert not worker.pending and worker.decoder is decoder
        assert result[0][1] == token
        if token:
            tokens.append(token)
        if worker.refreshes != before:
            refreshes.append((stamp, worker.nominal_fps))
            assert worker.refreshes == before + 1
            if last_refresh:
                assert stamp - last_refresh >= RATE_COOLDOWN
            last_refresh = stamp
        output = result[0][2]
        nals = split_annexb(output)
        if token or worker.refreshes != before:
            assert {5, 7, 8} <= {nal[0] & 31 for nal in nals}
        if any(nal[0] & 31 == 7 for nal in nals):
            signaled_rates.append(
                parse_sps(next(nal for nal in nals if nal[0] & 31 == 7)).frame_rate
            )
        decoded += len(independent.decode(av.Packet(output)))
        rows.append((stamp, len(output), worker.nominal_fps))
    assert decoded == seed_frames + 165
    assert [rate for _stamp, rate in refreshes] == [initial, 15]
    assert worker.nominal_fps == 15 and worker.refreshes == 2
    assert len(tokens) == 3 and not worker.forces
    assert signaled_rates[0] == 30 and signaled_rates[-1] == 15
    assert not worker.inputs and not worker.pending
    assert len(worker.rate.samples) <= RATE_SAMPLES
    # Measure codec bytes on original source time, including the corrective
    # IDR. The separate rate-envelope investigation covers that transitional
    # window; stable corrected-rate windows still include explicit consecutive
    # forced IDRs and are required to fit this qualified 132-kbps scene.
    windows = [
        (stamp, sum(size for other, size, _rate in rows if stamp - 90000 < other <= stamp) * 8)
        for stamp, _size, _rate in rows
    ]
    assert any(stamp < refreshes[-1][0] + 90000 for stamp, _bits in windows)
    assert (
        max(bits for stamp, bits in windows if stamp >= refreshes[-1][0] + 90000)
        <= config.max_bit_rate * 1000
    )


def test_short_real_cadence_excursion_does_not_refresh_an_adjacent_rounded_rate():
    rate = RateCadence(30)
    for index in range(300):
        stamp = index * 6000
        rate.observe(stamp)
    assert rate.target == 15
    changed = rate.changed_at
    for _index in range(20):  # Only 1.54 source seconds at 13 fps.
        stamp += 90000 // 13
        assert rate.observe(stamp) == 15
    for _index in range(180):
        stamp += 6000
        assert rate.observe(stamp) == 15
    assert rate.changed_at == changed
