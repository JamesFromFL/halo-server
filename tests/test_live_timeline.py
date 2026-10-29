"""Exact source/media clock tests; no process, sockets or real source."""

from types import SimpleNamespace as NS

import pytest

from halo_server.media.live_timeline import LiveTimeline, TimelineError


class Clock:
    now = 100.0
    wall = 1_800_000_000.0

    def timeline(self, rate=24000):
        return LiveTimeline(
            audio_rate=rate,
            video_origin=900000,
            audio_origin=None if rate is None else 480000,
            clock=lambda: self.now,
            wall_clock=lambda: self.wall,
        )


def sr(kind, pts=0, offset=0, receipt=100, epoch=1, source=123):
    return NS(
        kind=kind,
        rtp_timestamp=pts,
        sender_ntp=(2_300_000_000 << 32) + round(offset * 2**32),
        received_at=receipt,
        capture_epoch=epoch,
        ssrc=source,
    )


def observe(timeline, kind, pts=0, receipt=100, epoch=1, clocks=()):
    timeline.observe_input(
        kind, capture_epoch=epoch, source_pts=pts, received_at=receipt, source_clocks=clocks
    )


def rtp(timeline, kind, pts, epoch=1):
    return timeline.rtp_timestamp(kind, capture_epoch=epoch, source_pts=pts)


def presentation(timeline, kind, pts, epoch=1):
    return timeline.presentation(kind, capture_epoch=epoch, source_pts=pts)


@pytest.mark.parametrize("rate", [8000, 16000, 24000])
def test_original_audio_lookahead_and_multiple_outputs_have_distinct_presentation(rate):
    clock = Clock()
    timeline = clock.timeline(rate)
    observe(timeline, "audio")
    pts = (-120, 840, 1800)
    assert [presentation(timeline, "audio", p) for p in pts] == pytest.approx(
        [99.9975, 100.0175, 100.0375], abs=1e-9
    )
    timestamps = [rtp(timeline, "audio", p) for p in pts]
    assert timestamps == [480000 + p * rate // 48000 for p in pts]
    clock.now += 0.4  # Worker completion time cannot refresh original correlation.
    assert presentation(timeline, "audio", pts[0]) == 99.9975
    assert timeline.snapshot()["tracks"]["audio"]["confidence"] == "estimated"


@pytest.mark.parametrize("first", ["audio", "video"])
def test_paired_sr_preserves_known_av_offset_despite_equal_receipts_and_arrival_order(first):
    timeline = Clock().timeline()
    clocks = (sr("video"), sr("audio", offset=0.1))
    observe(timeline, first, clocks=clocks)
    observe(timeline, "video" if first == "audio" else "audio", clocks=clocks)
    assert presentation(timeline, "video", 0) == 100
    assert presentation(timeline, "audio", 0) == pytest.approx(100.1, abs=1e-9)
    assert timeline.snapshot()["mode"] == "source_sender_report"


@pytest.mark.parametrize("initial", [(), (sr("video"),)])
def test_late_source_sr_never_promotes_an_established_receipt_mapping(initial):
    clock = Clock()
    timeline = clock.timeline()
    observe(timeline, "video", clocks=initial)
    assert presentation(timeline, "video", 90000) == 101
    clock.now = 101
    observe(
        timeline,
        "audio",
        48000,
        101,
        clocks=(sr("video", 90000, 1, 101), sr("audio", 48000, 1.3, 101)),
    )
    assert presentation(timeline, "video", 90000) == 101
    assert timeline.snapshot()["mode"] == "receiver_estimated"
    assert timeline.snapshot()["tracks"]["audio"]["counters"]["late_ignored"] == 1


def test_video_only_uses_one_sr_and_huge_incoherent_track_pair_uses_honest_estimate():
    timeline = Clock().timeline(None)
    observe(timeline, "video", clocks=(sr("video"),))
    assert presentation(timeline, "video", 90000) == 101
    assert timeline.snapshot()["mode"] == "source_sender_report"
    paired = Clock().timeline()
    observe(paired, "video", clocks=(sr("video"), sr("audio", offset=20)))
    assert presentation(paired, "video", 0) == 100
    assert paired.snapshot()["mode"] == "receiver_estimated"


def test_same_epoch_reconfigure_and_wrap_preserve_real_elapsed_source_time():
    clock = Clock()
    timeline = clock.timeline(None)
    start = 2**32 - 45000
    observe(timeline, "video", start)
    first = rtp(timeline, "video", start)
    timeline.sent("video", capture_epoch=1, source_pts=start)
    clock.now += 2
    observe(timeline, "video", start + 180000, 102)
    assert rtp(timeline, "video", start + 180000) == first + 180000
    # There is deliberately no reconfigure API that could rebase this map.
    assert presentation(timeline, "video", start + 180000) == 102


def test_signed_report_samples_both_axes_at_one_instant_and_ignores_later_wall_step():
    clock = Clock()
    timeline = clock.timeline()
    observe(timeline, "video", clocks=(sr("video"), sr("audio", offset=0.1)))
    observe(timeline, "audio", clocks=(sr("video"), sr("audio", offset=0.1)))
    wire = rtp(timeline, "audio", 0)
    timeline.sent("audio", capture_epoch=1, source_pts=0)
    instant = timeline.instant()
    report = timeline.report("audio", capture_epoch=1, instant=instant)
    assert report.rtp_timestamp == wire - 2400  # Future sample means signed back-extrapolation.
    assert report.wall_time == clock.wall
    clock.wall -= 60
    clock.now += 1
    later = timeline.report("audio", capture_epoch=1, instant=timeline.instant())
    assert later.rtp_timestamp == report.rtp_timestamp + 24000
    assert later.wall_time == report.wall_time + 1
    assert rtp(timeline, "audio", 0) == wire  # Reports cannot mutate media mapping.
    other = clock.timeline()
    with pytest.raises(TimelineError, match="invalid_instant"):
        timeline.report("audio", capture_epoch=1, instant=other.instant())


def test_new_capture_epoch_preserves_real_gap_but_late_old_output_is_denied():
    clock = Clock()
    timeline = clock.timeline(None)
    observe(timeline, "video")
    first = rtp(timeline, "video", 0)
    timeline.sent("video", capture_epoch=1, source_pts=0)
    clock.now = 103
    observe(timeline, "video", 0, 103, epoch=2)
    assert timeline.report("video", capture_epoch=2, instant=timeline.instant()) is None
    assert rtp(timeline, "video", 0, 2) == first + 270000
    assert timeline.report("video", capture_epoch=2, instant=timeline.instant()) is None
    timeline.sent("video", capture_epoch=2, source_pts=0)
    assert timeline.report("video", capture_epoch=2, instant=timeline.instant()) is not None
    with pytest.raises(TimelineError, match="stale_epoch"):
        rtp(timeline, "video", 90000)
    with pytest.raises(TimelineError, match="stale_epoch"):
        observe(timeline, "video", 90000, 100, epoch=1)
    assert timeline.snapshot()["epoch_changes"] == 1


def test_reordered_duplicate_incoherent_and_stale_sr_never_change_frozen_mapping():
    clock = Clock()
    timeline = clock.timeline(None)
    observe(timeline, "video", clocks=(sr("video"),))
    expected = presentation(timeline, "video", 90000)
    clock.now = 101
    observe(timeline, "video", 90000, 101, clocks=(sr("video", receipt=101),))
    observe(timeline, "video", 90000, 101, clocks=(sr("video", -90000, -1, 101),))
    observe(timeline, "video", 90000, 101, clocks=(sr("video", 90000, 2, 101),))
    assert presentation(timeline, "video", 90000) == expected
    state = timeline.snapshot()["tracks"]["video"]
    assert state["counters"]["duplicate"] == state["counters"]["reordered"] == 1
    assert state["counters"]["incoherent"] == 1
    assert state["confidence"] == "source_held_over"
    clock.now = 131
    assert timeline.snapshot()["tracks"]["video"]["confidence"] == "source_stale"
    assert presentation(timeline, "video", 90000) == expected


def test_report_before_media_and_regressing_new_epoch_are_explicit():
    clock = Clock()
    timeline = clock.timeline(None)
    observe(timeline, "video")
    assert timeline.report("video", capture_epoch=1, instant=timeline.instant()) is None
    rtp(timeline, "video", 90000)
    timeline.sent("video", capture_epoch=1, source_pts=90000)
    observe(timeline, "video", 0, 100, epoch=2)
    with pytest.raises(TimelineError, match="output_regressed"):
        rtp(timeline, "video", 0, 2)
    clock.now = 102
    observe(timeline, "video", 180000, 102, epoch=2)
    assert rtp(timeline, "video", 180000, 2) == 1080000


def test_metadata_is_fixed_bounded_and_excludes_raw_clock_origins_or_sources():
    timeline = Clock().timeline(None)
    observe(timeline, "video", clocks=(sr("video"),))
    presentation(timeline, "video", 0)
    snapshot = timeline.snapshot()
    assert set(snapshot) == {"mode", "epoch_changes", "clock_sample_span_us", "tracks"}
    assert set(snapshot["tracks"]) == {"video"}
    assert "123" not in repr(snapshot) and "2300000000" not in repr(snapshot)
    assert not hasattr(timeline, "_packets")


def test_both_sr_can_arrive_before_first_mapping_but_stale_initial_sr_is_not_qualified():
    clock = Clock()
    timeline = clock.timeline()
    observe(timeline, "video", clocks=(sr("video"),))
    clock.now = 101
    observe(timeline, "audio", 48000, 101, clocks=(sr("audio", 48000, 1, 101),))
    assert presentation(timeline, "audio", 48000) == 101
    assert timeline.snapshot()["mode"] == "source_sender_report"
    stale = clock.timeline(None)
    observe(stale, "video", 90000, 101, clocks=(sr("video", 90000, 1, 101),))
    clock.now = 132
    assert presentation(stale, "video", 90000) == 101
    assert stale.snapshot()["mode"] == "receiver_estimated"


def test_frozen_clock_confidence_detects_accumulated_residual_not_only_each_small_step():
    clock = Clock()
    timeline = clock.timeline(None)
    observe(timeline, "video", clocks=(sr("video"),))
    expected = presentation(timeline, "video", 90000)
    # Each individual step is less than 50ms, but the frozen reference must
    # not be reported coherent after cumulative source-clock drift.
    for index in range(1, 30):
        clock.now = 100 + index
        pts = index * 90000 + index * 1800
        observe(timeline, "video", pts, clock.now, clocks=(sr("video", pts, index, clock.now),))
    assert timeline.snapshot()["tracks"]["video"]["confidence"] == "source_held_over"
    assert presentation(timeline, "video", 90000) == expected


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), 1e300])
def test_invalid_receipts_are_rejected_without_initializing_mapping(value):
    timeline = Clock().timeline(None)
    with pytest.raises(TimelineError, match="invalid_time"):
        observe(timeline, "video", receipt=value)
    assert timeline.snapshot()["mode"] == "pending"


def test_output_report_does_not_increment_media_or_emit_before_actual_sent_hook():
    clock = Clock()
    timeline = clock.timeline(None)
    observe(timeline, "video")
    rtp(timeline, "video", 0)
    assert timeline.report("video", capture_epoch=1, instant=timeline.instant()) is None
    timeline.sent("video", capture_epoch=1, source_pts=0)
    clock.now += 2
    first = timeline.report("video", capture_epoch=1, instant=timeline.instant())
    second = timeline.report("video", capture_epoch=1, instant=timeline.instant())
    assert first == second and first.rtp_timestamp == 1080000


@pytest.mark.parametrize("kind", ["audio", "video"])
@pytest.mark.parametrize("seconds", [-30, 20])
def test_recent_plausible_sr_pair_cannot_map_fresh_original_input_tens_of_seconds_away(
    kind, seconds
):
    timeline = Clock().timeline()
    rate = 48000 if kind == "audio" else 90000
    clocks = (
        sr("video", seconds * rate if kind == "video" else 0),
        sr("audio", seconds * rate if kind == "audio" else 0),
    )
    observe(timeline, "video", clocks=clocks)
    observe(timeline, "audio", clocks=clocks)
    assert presentation(timeline, kind, 0) == 100
    assert timeline.snapshot()["mode"] == "receiver_estimated"


@pytest.mark.parametrize("later", ["audio", "video"])
def test_unobserved_enabled_track_cannot_qualify_unknown_initial_source_projection(later):
    timeline = Clock().timeline()
    first = "video" if later == "audio" else "audio"
    clocks = (sr("video"), sr("audio"))
    observe(timeline, first, clocks=clocks)
    assert presentation(timeline, first, 0) == 100
    assert timeline.snapshot()["mode"] == "receiver_estimated"
    rate = 48000 if later == "audio" else 90000
    observe(timeline, later, rate * 20, clocks=clocks)
    assert presentation(timeline, later, rate * 20) == 100
    assert timeline.snapshot()["mode"] == "receiver_estimated"


@pytest.mark.parametrize("seconds", [-5, 5])
def test_initial_projection_preserves_previous_five_second_sanity_boundary(seconds):
    timeline = Clock().timeline(None)
    observe(timeline, "video", clocks=(sr("video", seconds * 90000),))
    assert presentation(timeline, "video", 0) == 100 - seconds
    assert timeline.snapshot()["mode"] == "source_sender_report"
