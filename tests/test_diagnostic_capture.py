"""Real artifacts retain encoded bytes and disclose every recorder limit."""

import hashlib
import json
import os
import stat
import threading
import time

import pytest

from halo_server.media.diagnostic_capture import (
    MAX_EVENT_BYTES,
    MAX_EVENTS,
    MAX_MEDIA_BYTES,
    MAX_QUEUE_BYTES,
    DiagnosticCapture,
)


class PausedCapture(DiagnosticCapture):
    """Hold only the writer so producer behavior can be tested deterministically."""

    def __init__(self, *args, **kwargs):
        self.entered = threading.Event()
        self.release = threading.Event()
        super().__init__(*args, **kwargs)
        assert self.entered.wait(1)

    def _open(self, name):
        if name == "events.jsonl":
            self.entered.set()
            assert self.release.wait(3)
        return super()._open(name)


def finished(capture):
    if isinstance(capture, PausedCapture):
        capture.release.set()
    result = capture.close()
    assert result["finished"]
    assert not result["cleanup_pending"]
    return result


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def manifest(capture):
    return json.loads((capture.directory / "manifest.json").read_text())


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.001)
    assert predicate()


def test_real_artifact_roundtrip_timing_fields_hashes_and_private_modes(tmp_path):
    capture = PausedCapture(tmp_path / "capture")
    source = bytes(range(256))
    output = b"\x00\x00\x00\x01\x65encoded-h264"
    before = time.monotonic_ns()
    assert capture.event("source_rtp", "audio", seq=7, rtp_timestamp=480, marker=True)
    assert capture.media("source_rtp", "audio", source, seq=7, rtp_timestamp=480)
    assert capture.trigger("audio_assembly_hold")
    assert capture.media("encoded_output", "video", output, pts=9, stream_id=1)
    after = time.monotonic_ns()
    result = finished(capture)
    assert result["trace_complete"]
    assert not result["active"]
    assert not result["accepting"]
    assert capture.closed
    assert result["counters"]["written_events"] == 1
    assert result["counters"]["written_media"] == 2
    media = (capture.directory / "media.bin").read_bytes()
    index = rows(capture.directory / "media.jsonl")
    assert [media[item["offset"] : item["offset"] + item["length"]] for item in index] == [
        source,
        output,
    ]
    assert all(before <= item["monotonic_ns"] <= after for item in index)
    assert index[0]["fields"] == {"seq": 7, "rtp_timestamp": 480}
    assert rows(capture.directory / "events.jsonl")[0]["fields"]["marker"] is True
    final = manifest(capture)
    assert final["trace_complete"]
    assert final["trigger"]["reason"] == "audio_assembly_hold"
    assert final["trigger"]["kind"] == "trigger"
    assert final["media_window"]["independently_decodable"] == "not_guaranteed"
    assert stat.S_IMODE(capture.directory.stat().st_mode) == 0o700
    for name, spec in final["files"].items():
        path = capture.directory / name
        data = path.read_bytes()
        assert len(data) == spec["bytes"]
        assert hashlib.sha256(data).hexdigest() == spec["sha256"]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((capture.directory / "manifest.json").stat().st_mode) == 0o600


def test_idle_deadline_stops_and_finalizes_without_callbacks(tmp_path):
    capture = DiagnosticCapture(tmp_path / "deadline", duration_seconds=0.03)
    wait_for(lambda: capture.status()["finished"])
    result = capture.status()
    assert result["state"] == "closed"
    assert result["stop_reason"] == "deadline"
    assert result["stopped_monotonic_ns"] == result["deadline_monotonic_ns"]
    assert result["trace_complete"]
    assert not capture.event("lifecycle", "session", reason="start")
    assert not capture.media("source_rtp", "audio", b"late")
    assert not capture.trigger()
    assert capture.allocate_stream() == 0
    assert finished(capture) == result


def test_close_without_trigger_preserves_explicit_final_window(tmp_path):
    capture = PausedCapture(tmp_path / "end")
    assert capture.media("source_rtp", "audio", b"final", seq=1)
    result = finished(capture)
    assert result["trigger"]["kind"] == "end_window"
    assert result["trigger"]["reason"] == "stop"
    assert (capture.directory / "media.bin").read_bytes() == b"final"


def test_queue_is_bounded_and_dropped_trace_is_never_claimed_complete(tmp_path):
    capture = PausedCapture(tmp_path / "queue", max_queue_bytes=800)
    assert capture.media("source_rtp", "audio", b"a" * 400)
    assert not capture.media("source_rtp", "audio", b"b" * 400)
    assert not capture.event("source_rtp", "audio", seq=1, rtp_timestamp=2)
    result = finished(capture)
    assert result["counters"]["queue_drops"] == 2
    assert result["queue_high_water_bytes"] <= 800
    assert not result["trace_complete"]
    assert not manifest(capture)["trace_complete"]
    assert (capture.directory / "media.bin").read_bytes() == b"a" * 400


def test_contention_drops_immediately_without_waiting_or_disk_io(tmp_path):
    capture = PausedCapture(tmp_path / "contention")
    with capture._lock:
        begin = time.monotonic()
        assert not capture.event("source_rtp", "audio", seq=1)
        assert not capture.media("source_rtp", "audio", b"bytes")
        assert not capture.trigger()
        assert time.monotonic() - begin < 0.1
    result = finished(capture)
    assert result["counters"]["contention_drops"] == 3
    assert not result["trace_complete"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_events": 1},
        {"max_event_bytes": 1},
    ],
)
def test_event_limits_are_hard_and_audited(tmp_path, kwargs):
    capture = PausedCapture(tmp_path / "events", **kwargs)
    assert capture.event("source_rtp", "audio", seq=1)
    assert capture.event("source_rtp", "audio", seq=2)
    result = finished(capture)
    assert result["counters"]["event_limit_drops"] >= 1
    assert result["bytes_written"]["events.jsonl"] <= kwargs.get("max_event_bytes", MAX_EVENT_BYTES)
    assert result["counters"]["written_events"] <= kwargs.get("max_events", MAX_EVENTS)
    assert not result["trace_complete"]


def test_media_limit_bounds_payload_and_audits_drop(tmp_path):
    capture = PausedCapture(tmp_path / "media", max_media_bytes=8)
    assert capture.trigger()
    assert capture.media("encoded_output", "audio", b"12345")
    assert capture.media("encoded_output", "audio", b"67890")
    result = finished(capture)
    assert result["counters"]["media_limit_drops"] == 1
    assert (capture.directory / "media.bin").read_bytes() == b"12345"
    assert not result["trace_complete"]


def test_first_trigger_wins_and_post_window_is_finite(tmp_path):
    capture = PausedCapture(tmp_path / "window", post_trigger_seconds=0.01)
    assert capture.media("source_rtp", "audio", b"before")
    assert capture.trigger("manual")
    assert not capture.trigger("audio_assembly_hold")
    assert capture.media("source_rtp", "audio", b"inside")
    time.sleep(0.02)
    assert capture.media("source_rtp", "audio", b"after")
    result = finished(capture)
    assert (capture.directory / "media.bin").read_bytes() == b"beforeinside"
    assert result["counters"]["media_outside_window"] == 1
    assert result["trace_complete"]
    assert result["trigger"]["reason"] == "manual"


def test_pre_trigger_ring_has_age_and_memory_bounds(tmp_path):
    capture = DiagnosticCapture(tmp_path / "ring", pre_trigger_seconds=0.03, max_media_bytes=300)
    assert capture.media("source_rtp", "audio", b"a" * 100)
    wait_for(lambda: capture.status()["counters"]["media_window_byte_evictions"] == 1)
    assert capture.media("source_rtp", "audio", b"b")
    wait_for(lambda: capture.status()["counters"]["media_window_time_evictions"] == 1)
    assert capture.media("source_rtp", "audio", b"c")
    result = finished(capture)
    assert (capture.directory / "media.bin").read_bytes() == b"c"
    assert not result["trace_complete"]


@pytest.mark.parametrize(
    "stage,track,fields",
    [
        ("https://secret", "audio", {}),
        ("source_rtp", "address", {}),
        ("source_rtp", "audio", {"token": 123}),
        ("source_rtp", "audio", {"seq": "credential"}),
        ("source_rtp", "audio", {"seq": float("nan")}),
        ("source_rtp", "audio", {"seq": float("inf")}),
        ("source_rtp", "audio", {"seq": 10**1000}),
        ("source_rtp", "audio", {"seq": [1, 2]}),
        ("source_rtp", "audio", {"reason": "secret"}),
        ([], "audio", {}),
    ],
)
def test_untrusted_metadata_is_rejected_without_hot_path_exceptions(tmp_path, stage, track, fields):
    capture = PausedCapture(tmp_path / "invalid")
    assert not capture.event(stage, track, **fields)
    result = finished(capture)
    assert result["counters"]["invalid_records"] == 1
    assert (capture.directory / "events.jsonl").read_bytes() == b""
    assert not result["trace_complete"]


def test_mutable_oversized_or_empty_payloads_are_rejected(tmp_path):
    capture = PausedCapture(tmp_path / "payload")
    for payload in (b"", bytearray(b"mutable"), memoryview(b"view"), b"x" * (1024**2 + 1)):
        assert not capture.media("source_rtp", "audio", payload)
    assert not capture.trigger("unbounded text")
    assert finished(capture)["counters"]["invalid_records"] == 5


@pytest.mark.parametrize(
    "kwargs",
    [
        {"duration_seconds": 301},
        {"duration_seconds": float("nan")},
        {"duration_seconds": 10**1000},
        {"duration_seconds": True},
        {"pre_trigger_seconds": 31},
        {"post_trigger_seconds": -1},
        {"max_media_bytes": MAX_MEDIA_BYTES + 1},
        {"max_event_bytes": MAX_EVENT_BYTES + 1},
        {"max_events": MAX_EVENTS + 1},
        {"max_queue_bytes": MAX_QUEUE_BYTES + 1},
        {"max_events": True},
    ],
)
def test_invalid_configuration_creates_no_directory(tmp_path, kwargs):
    destination = tmp_path / "invalid"
    with pytest.raises(ValueError, match="invalid_capture_limits"):
        DiagnosticCapture(destination, **kwargs)
    assert not destination.exists()


def test_reusing_or_following_symlinked_capture_directory_is_forbidden(tmp_path):
    capture = DiagnosticCapture(tmp_path / "existing")
    finished(capture)
    with pytest.raises(FileExistsError):
        DiagnosticCapture(capture.directory)
    (tmp_path / "linked").symlink_to(capture.directory)
    with pytest.raises(FileExistsError):
        DiagnosticCapture(tmp_path / "linked")
    (tmp_path / "parent").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="invalid_capture_directory"):
        DiagnosticCapture(tmp_path / "parent" / "child")


def test_unsafe_parent_is_rejected(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    with pytest.raises(ValueError, match="unsafe_capture_parent"):
        DiagnosticCapture(parent / "capture")


def test_fence_is_immediate_and_close_join_is_bounded(tmp_path):
    capture = PausedCapture(tmp_path / "blocked")
    assert capture.allocate_stream() == 1
    assert capture.allocate_stream() == 2
    capture.fence()
    start = time.monotonic()
    result = capture.close(timeout=0.01)
    assert time.monotonic() - start < 0.2
    assert result["cleanup_pending"]
    assert not result["finished"]
    assert not result["accepting"]
    assert not capture.event("lifecycle", "session", reason="start")
    assert capture.allocate_stream() == 0
    assert finished(capture)["finished"]


def test_writer_open_failure_is_isolated_and_manifest_reports_failure(tmp_path):
    class FailedCapture(DiagnosticCapture):
        def _open(self, name):
            if name == "media.bin":
                raise OSError("simulated unavailable disk")
            return super()._open(name)

    capture = FailedCapture(tmp_path / "failure")
    wait_for(lambda: capture.status()["finished"])
    result = capture.status()
    assert result["state"] == "failed"
    assert not result["trace_complete"]
    assert result["counters"]["writer_failures"] == 1
    assert not capture.event("source_rtp", "audio", seq=1)
    assert manifest(capture)["state"] == "failed"
    assert "simulated unavailable disk" not in (capture.directory / "manifest.json").read_text()


def test_writer_thread_owns_serialization_hashing_and_file_creation(tmp_path, monkeypatch):
    thread_ids = []
    original_open = DiagnosticCapture._open
    original_json = DiagnosticCapture._json

    def observed_open(self, name):
        thread_ids.append(threading.get_ident())
        return original_open(self, name)

    def observed_json(self, value):
        thread_ids.append(threading.get_ident())
        return original_json(self, value)

    monkeypatch.setattr(DiagnosticCapture, "_open", observed_open)
    monkeypatch.setattr(DiagnosticCapture, "_json", observed_json)
    capture = DiagnosticCapture(tmp_path / "threads")
    assert capture.event("source_rtp", "audio", seq=1)
    finished(capture)
    assert thread_ids
    assert all(identity == capture._thread.ident for identity in thread_ids)
    assert threading.get_ident() not in thread_ids


def test_stricter_umask_cannot_weaken_explicit_private_modes(tmp_path):
    previous = os.umask(0o777)
    try:
        capture = DiagnosticCapture(tmp_path / "umask")
        finished(capture)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(capture.directory.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in capture.directory.iterdir())


def test_declared_optional_numeric_metadata_preserves_unknown(tmp_path):
    capture = PausedCapture(tmp_path / "unknown")
    assert capture.event("audio_jitter_after", "audio", origin=None, frame_timestamp=None)
    finished(capture)
    assert rows(capture.directory / "events.jsonl")[0]["fields"] == {
        "origin": None,
        "frame_timestamp": None,
    }


def test_writer_write_failure_never_escapes_to_media_producer(tmp_path):
    class FailingFile:
        def __init__(self, file):
            self.file = file

        def write(self, value):
            raise OSError("private disk details")

        def __getattr__(self, name):
            return getattr(self.file, name)

    class FailedCapture(PausedCapture):
        def _open(self, name):
            file = super()._open(name)
            return FailingFile(file) if name == "events.jsonl" else file

    capture = FailedCapture(tmp_path / "write_failure")
    assert capture.event("source_rtp", "audio", seq=1)
    result = finished(capture)
    assert result["state"] == "failed"
    assert result["counters"]["writer_failures"] >= 1
    assert not result["trace_complete"]
    assert not capture.media("source_rtp", "audio", b"later")
    assert "private disk details" not in (capture.directory / "manifest.json").read_text()


def test_media_index_limit_never_writes_unindexed_payload(tmp_path, monkeypatch):
    monkeypatch.setattr("halo_server.media.diagnostic_capture.MAX_INDEX_BYTES", 1)
    capture = PausedCapture(tmp_path / "index_limit")
    assert capture.trigger()
    assert capture.media("source_rtp", "audio", b"data")
    result = finished(capture)
    assert result["counters"]["media_index_limit_drops"] == 1
    assert not result["trace_complete"]
    assert (capture.directory / "media.bin").read_bytes() == b""
    assert (capture.directory / "media.jsonl").read_bytes() == b""


def test_backlogged_trigger_window_uses_observation_time_not_writer_time(tmp_path):
    capture = PausedCapture(
        tmp_path / "backlog", pre_trigger_seconds=0.01, post_trigger_seconds=0.01
    )
    assert capture.media("source_rtp", "audio", b"before")
    assert capture.trigger("audio_assembly_hold")
    assert capture.media("source_rtp", "audio", b"after")
    time.sleep(0.03)  # Writer starts after the entire selected window ended.
    result = finished(capture)
    assert (capture.directory / "media.bin").read_bytes() == b"beforeafter"
    assert result["trace_complete"]


def test_authority_contention_is_disclosed_and_final_counters_are_frozen(tmp_path):
    capture = PausedCapture(tmp_path / "authority")
    capture.note_authority_contention()
    result = finished(capture)
    assert result["counters"]["authority_contention_drops"] == 1
    assert not result["trace_complete"]
    capture.note_authority_contention()
    assert not capture.trigger("untrusted reason")
    assert capture.status() == result
