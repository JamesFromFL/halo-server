"""Explicit, bounded timing and encoded-media diagnostics for one camera.

Callbacks only offer scalar metadata and immutable payloads to a bounded queue.
The writer owns serialization, window selection, hashing and disk I/O. Payloads
in media.bin plus their media.jsonl index are exact encoded data, not a movie;
arrival timing must be replayed from the shared monotonic clock. No arbitrary
text, credentials, SDP, addresses or source identifiers are accepted.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import threading
import time
from collections import deque
from pathlib import Path

SCHEMA_VERSION = 1
MAX_DURATION_SECONDS = 300
MAX_MEDIA_BYTES = 64 * 1024 * 1024
MAX_EVENT_BYTES = 96 * 1024 * 1024
MAX_EVENTS = 500_000
MAX_QUEUE_BYTES = 8 * 1024 * 1024
MAX_QUEUE_ITEMS = 8192
MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_WINDOW_SECONDS = 30
MAX_FIELDS = 40

STAGES = frozenset(
    {
        "ingress_rtp",
        "source_rtp",
        "audio_jitter_before",
        "audio_jitter_after",
        "audio_assembly",
        "video_assembly",
        "source_frame",
        "output_frame",
        "source_read",
        "conversion_start",
        "conversion_end",
        "encoded_output",
        "timeline",
        "scheduled_send",
        "actual_send",
        "rtp_send",
        "rtp_sent",
        "receiver_report",
        "sender_report",
        "source_sender_report",
        "worker",
        "lifecycle",
        "stream_start",
        "stream_end",
        "packet_schedule",
        "packet_sent",
        "reconfigure_request",
        "reconfigure_applied",
        "reconfigure_first_idr",
        "reconfigure_superseded",
    }
)
TRACKS = frozenset({"audio", "video", "session"})
NUMERIC_FIELDS = frozenset(
    {
        "seq",
        "sequence",
        "rtp_timestamp",
        "clock_rate",
        "payload_type",
        "marker",
        "size",
        "payload_bytes",
        "missing",
        "buffered",
        "released",
        "skipped",
        "epoch",
        "config_epoch",
        "capture_epoch",
        "source_generation",
        "attempt",
        "stream_id",
        "received_ns",
        "source_ns",
        "scheduled_ns",
        "actual_ns",
        "duration_ns",
        "queue_depth",
        "frames",
        "samples",
        "sample_rate",
        "channels",
        "width",
        "height",
        "bitrate",
        "jitter",
        "packets_lost",
        "highest_sequence",
        "round_trip_ms",
        "origin",
        "capacity",
        "prefetch",
        "occupancy_mask",
        "contiguous_packets",
        "contiguous_frames",
        "waiting_packets",
        "first_gap_offset",
        "incoming_offset",
        "frame_returned",
        "frame_timestamp",
        "hold_ms",
        "pli",
        "timestamp",
        "pts",
        "duration_samples",
        "received_us",
        "scheduled_us",
        "actual_us",
        "elapsed_us",
        "lost",
        "fraction_lost",
        "lsr",
        "dlsr",
        "ntp_seconds",
        "ntp_fraction",
        "repair",
        "keyframe",
        "adapted",
        "fps",
        "bit_rate",
        "packet_time",
        "output_count",
        "presentation_us",
        "source_pts",
        "wall_us",
        "request_revision",
        "decoder_epoch",
        "decoder_preserved",
        "changed_fields",
        "mtu",
        "rtcp_interval_us",
        "profile_code",
        "level_code",
        "nal_mask",
    }
)
REASONS = frozenset(
    {
        "manual",
        "audio_assembly_hold",
        "audio_hold",
        "video_hold",
        "packet_gap",
        "recovery",
        "loss",
        "timeout",
        "start",
        "stop",
        "deadline",
    }
)
ENUM_FIELDS = {
    "codec": frozenset({"h264", "opus", "aac_eld", "aac_lc", "pcm"}),
    "reason": REASONS,
    "outcome": frozenset(
        {"queued", "sent", "released", "held", "dropped", "reordered", "complete"}
    ),
}
COUNTERS = (
    "accepted_events",
    "accepted_media",
    "written_events",
    "written_media",
    "queue_drops",
    "contention_drops",
    "authority_contention_drops",
    "invalid_records",
    "event_limit_drops",
    "media_limit_drops",
    "media_window_byte_evictions",
    "media_window_time_evictions",
    "media_outside_window",
    "media_index_limit_drops",
    "writer_failures",
    "discarded_on_failure",
)
LOSS_COUNTERS = (
    "queue_drops",
    "contention_drops",
    "authority_contention_drops",
    "invalid_records",
    "event_limit_drops",
    "media_limit_drops",
    "media_window_byte_evictions",
    "media_index_limit_drops",
    "writer_failures",
    "discarded_on_failure",
)


def _bounded_number(value, minimum, maximum):
    return type(value) in {int, float} and minimum <= value <= maximum and math.isfinite(value)


def _validated_fields(stage, track, fields):
    if stage not in STAGES or track not in TRACKS or len(fields) > MAX_FIELDS:
        return None
    for key, value in fields.items():
        if key in NUMERIC_FIELDS:
            if value is None or type(value) is bool:
                continue
            if not _bounded_number(value, -(2**63), 2**64 - 1):
                return None
        elif key in ENUM_FIELDS:
            if type(value) is not str or value not in ENUM_FIELDS[key]:
                return None
        else:
            return None
    return fields


def _private_directory(path):
    """Create an exclusive private leaf through a pinned parent descriptor."""
    path = Path(path).absolute()
    parent = path.parent
    if path.name in {"", ".", ".."} or parent.resolve(strict=True) != parent:
        raise ValueError("invalid_capture_directory")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(parent_fd)
        # A root-owned sticky /tmp permits a fresh exclusive private leaf.
        if info.st_uid not in {0, os.getuid()} or (
            info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
        ):
            raise ValueError("unsafe_capture_parent")
        os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
        os.chmod(path.name, 0o700, dir_fd=parent_fd, follow_symlinks=False)
        directory_fd = os.open(
            path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
        os.fchmod(directory_fd, 0o700)
        return path, directory_fd
    finally:
        os.close(parent_fd)


class DiagnosticCapture:
    """One explicitly authorized camera; camera binding belongs to the owner.

    False from event/media/trigger is a lost diagnostic observation, never a
    media failure. Close waits at most its timeout. A stuck filesystem leaves
    cleanup_pending true; unfinished artifacts must not be exported as complete.
    The writer is a daemon so diagnostics cannot prevent process shutdown.
    """

    def __init__(
        self,
        directory,
        *,
        duration_seconds=240,
        pre_trigger_seconds=10,
        post_trigger_seconds=10,
        max_media_bytes=MAX_MEDIA_BYTES,
        max_event_bytes=MAX_EVENT_BYTES,
        max_events=MAX_EVENTS,
        max_queue_bytes=MAX_QUEUE_BYTES,
    ):
        for value, minimum, maximum in (
            (duration_seconds, 0.01, MAX_DURATION_SECONDS),
            (pre_trigger_seconds, 0, MAX_WINDOW_SECONDS),
            (post_trigger_seconds, 0, MAX_WINDOW_SECONDS),
        ):
            if not _bounded_number(value, minimum, maximum):
                raise ValueError("invalid_capture_limits")
        for value, maximum in (
            (max_media_bytes, MAX_MEDIA_BYTES),
            (max_event_bytes, MAX_EVENT_BYTES),
            (max_events, MAX_EVENTS),
            (max_queue_bytes, MAX_QUEUE_BYTES),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("invalid_capture_limits")
        self.directory, self._directory_fd = _private_directory(directory)
        self._start_ns = time.monotonic_ns()
        self._deadline_ns = self._start_ns + int(duration_seconds * 1_000_000_000)
        self._pre_ns = int(pre_trigger_seconds * 1_000_000_000)
        self._post_ns = int(post_trigger_seconds * 1_000_000_000)
        self._limits = {
            "duration_seconds": duration_seconds,
            "pre_trigger_seconds": pre_trigger_seconds,
            "post_trigger_seconds": post_trigger_seconds,
            "media_bytes": max_media_bytes,
            "event_bytes": max_event_bytes,
            "events": max_events,
            "queue_bytes": max_queue_bytes,
            "queue_items": MAX_QUEUE_ITEMS,
            "payload_bytes": MAX_PAYLOAD_BYTES,
            "media_index_bytes": MAX_INDEX_BYTES,
        }
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._done = threading.Event()
        self._queue = deque()
        self._queue_bytes = 0
        self._queue_high_water_bytes = self._queue_high_water_items = 0
        self._counters = dict.fromkeys(COUNTERS, 0)
        self._closing = self._failed = False
        self._trigger = self._stop_reason = self._stop_ns = None
        self._stream_ordinal = 0
        self._writer_bytes = {"events.jsonl": 0, "media.jsonl": 0, "media.bin": 0}
        self._thread = threading.Thread(
            target=self._run, name="halo-diagnostic-capture", daemon=True
        )
        try:
            self._thread.start()
        except BaseException:
            os.close(self._directory_fd)
            raise

    @property
    def closed(self):
        """Cheap admission fence for already-installed optional observers."""
        return self._closing or self._failed or time.monotonic_ns() >= self._deadline_ns

    def allocate_stream(self):
        """Capture-local ordinal; view setup only, not a per-packet callback."""
        with self._lock:
            if self._closing or self._failed or time.monotonic_ns() >= self._deadline_ns:
                return 0
            self._stream_ordinal += 1
            return self._stream_ordinal

    def event(self, stage, track, **fields):
        return self._offer("event", stage, track, None, fields)

    def media(self, stage, track, payload, **fields):
        return self._offer("media", stage, track, payload, fields)

    def _offer(self, kind, stage, track, payload, fields):
        try:
            now = time.monotonic_ns()
            if self._closing or self._failed or now >= self._deadline_ns:
                self._wake.set()
                return False
            if _validated_fields(stage, track, fields) is None or (
                kind == "media"
                and (type(payload) is not bytes or not 0 < len(payload) <= MAX_PAYLOAD_BYTES)
            ):
                self._counters["invalid_records"] += 1
                return False
            cost = 256 + 128 * len(fields) + (len(payload) if payload is not None else 0)
            if not self._lock.acquire(blocking=False):
                self._counters["contention_drops"] += 1
                return False
            try:
                if self._closing or self._failed:
                    return False
                if (
                    len(self._queue) >= MAX_QUEUE_ITEMS
                    or self._queue_bytes + cost > self._limits["queue_bytes"]
                ):
                    self._counters["queue_drops"] += 1
                    return False
                self._queue.append((kind, now, stage, track, fields, payload, cost))
                self._queue_bytes += cost
                self._queue_high_water_bytes = max(self._queue_high_water_bytes, self._queue_bytes)
                self._queue_high_water_items = max(self._queue_high_water_items, len(self._queue))
                self._counters["accepted_events" if kind == "event" else "accepted_media"] += 1
            finally:
                self._lock.release()
            self._wake.set()
            return True
        except Exception:
            self._counters["invalid_records"] += 1
            return False

    def note_authority_contention(self):
        """Account for a skipped authority check without waiting on its lock."""
        if not self.closed:
            self._counters["authority_contention_drops"] += 1

    def trigger(self, reason="manual"):
        try:
            if self.closed:
                return False
            if type(reason) is not str or reason not in REASONS:
                self._counters["invalid_records"] += 1
                return False
            if not self._lock.acquire(blocking=False):
                self._counters["contention_drops"] += 1
                return False
            try:
                now = time.monotonic_ns()
                if (
                    self._trigger is not None
                    or self._closing
                    or self._failed
                    or now >= self._deadline_ns
                ):
                    return False
                self._trigger = {"monotonic_ns": now, "reason": reason, "kind": "trigger"}
            finally:
                self._lock.release()
            self._wake.set()
            return True
        except Exception:
            self._counters["invalid_records"] += 1
            return False

    def status(self):
        with self._lock:
            finalized = self._done.is_set()
            accepting = (
                not (self._closing or self._failed) and time.monotonic_ns() < self._deadline_ns
            )
            return {
                "schema_version": SCHEMA_VERSION,
                "state": (
                    "failed"
                    if self._failed
                    else "closed"
                    if finalized
                    else "finalizing"
                    if self._closing
                    else "capturing"
                ),
                "active": accepting,
                "accepting": accepting,
                "finished": finalized,
                "finalized": finalized,
                "cleanup_pending": not finalized,
                "started_monotonic_ns": self._start_ns,
                "deadline_monotonic_ns": self._deadline_ns,
                "stopped_monotonic_ns": self._stop_ns,
                "stop_reason": self._stop_reason,
                "trigger": dict(self._trigger) if self._trigger else None,
                "limits": dict(self._limits),
                "counters": dict(self._counters),
                "queue_bytes": self._queue_bytes,
                "queue_items": len(self._queue),
                "queue_high_water_bytes": self._queue_high_water_bytes,
                "queue_high_water_items": self._queue_high_water_items,
                "bytes_written": dict(self._writer_bytes),
                "trace_complete": finalized and not any(self._counters[k] for k in LOSS_COUNTERS),
            }

    def fence(self):
        """Revoke admission immediately without waiting on filesystem cleanup."""
        with self._lock:
            if not self._closing:
                self._closing = True
                self._stop_ns = time.monotonic_ns()
                self._stop_reason = "stop"
        self._wake.set()

    def close(self, timeout=2):
        if not _bounded_number(timeout, 0, 5):
            raise ValueError("invalid_close_timeout")
        self.fence()
        self._thread.join(timeout=timeout)
        return self.status()

    def _open(self, name):
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self._directory_fd,
        )
        try:
            os.fchmod(fd, 0o600)
            return os.fdopen(fd, "wb", buffering=64 * 1024)
        except BaseException:
            os.close(fd)
            raise

    def _json(self, value):
        return (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode()

    def _run(self):
        files = {}
        hashes = {name: hashlib.sha256() for name in self._writer_bytes}
        ring = deque()
        ring_bytes = 0
        active_trigger = None
        processed_ns = self._start_ns
        try:
            for name in self._writer_bytes:
                files[name] = self._open(name)

            def write(name, value):
                if files[name].write(value) != len(value):
                    raise OSError("short_diagnostic_write")
                hashes[name].update(value)
                self._writer_bytes[name] += len(value)

            def write_media(record):
                _, timestamp, stage, track, fields, payload, _ = record
                if self._writer_bytes["media.bin"] + len(payload) > self._limits["media_bytes"]:
                    self._counters["media_limit_drops"] += 1
                    return
                metadata = self._json(
                    {
                        "monotonic_ns": timestamp,
                        "stage": stage,
                        "track": track,
                        "fields": fields,
                        "offset": self._writer_bytes["media.bin"],
                        "length": len(payload),
                    }
                )
                if self._writer_bytes["media.jsonl"] + len(metadata) > MAX_INDEX_BYTES:
                    self._counters["media_index_limit_drops"] += 1
                    return
                write("media.bin", payload)
                write("media.jsonl", metadata)
                self._counters["written_media"] += 1

            while True:
                with self._lock:
                    now = time.monotonic_ns()
                    if not self._closing and now >= self._deadline_ns:
                        self._closing = True
                        self._stop_ns = self._deadline_ns
                        self._stop_reason = "deadline"
                    if self._closing and self._trigger is None:
                        self._trigger = {
                            "monotonic_ns": self._stop_ns,
                            "reason": self._stop_reason,
                            "kind": "end_window",
                        }
                    requested_trigger = self._trigger
                    record = self._queue.popleft() if self._queue else None
                    if record is not None:
                        self._queue_bytes -= record[-1]
                    finished = self._closing and record is None
                    self._wake.clear()
                if requested_trigger is not None and active_trigger is None:
                    active_trigger = requested_trigger
                    cutoff = active_trigger["monotonic_ns"] - self._pre_ns
                    while ring:
                        previous = ring.popleft()
                        if previous[1] >= cutoff:
                            write_media(previous)
                        else:
                            self._counters["media_window_time_evictions"] += 1
                    ring_bytes = 0
                if record is not None:
                    kind, timestamp, stage, track, fields, payload, cost = record
                    if kind == "event":
                        line = self._json(
                            {
                                "monotonic_ns": timestamp,
                                "stage": stage,
                                "track": track,
                                "fields": fields,
                            }
                        )
                        if (
                            self._counters["written_events"] >= self._limits["events"]
                            or self._writer_bytes["events.jsonl"] + len(line)
                            > self._limits["event_bytes"]
                        ):
                            self._counters["event_limit_drops"] += 1
                        else:
                            write("events.jsonl", line)
                            self._counters["written_events"] += 1
                    elif active_trigger is not None:
                        trigger_ns = active_trigger["monotonic_ns"]
                        if trigger_ns - self._pre_ns <= timestamp <= trigger_ns + self._post_ns:
                            write_media(record)
                        else:
                            self._counters["media_outside_window"] += 1
                    else:
                        ring.append(record)
                        ring_bytes += cost
                    # Follow processed observation time while draining a backlog.
                    # Writer scheduling delays must not define the media window.
                    processed_ns = max(processed_ns, timestamp)
                    cutoff = processed_ns - self._pre_ns
                    while ring and ring[0][1] < cutoff:
                        ring_bytes -= ring.popleft()[-1]
                        self._counters["media_window_time_evictions"] += 1
                    while ring and (
                        ring_bytes > self._limits["media_bytes"] or len(ring) > MAX_QUEUE_ITEMS
                    ):
                        ring_bytes -= ring.popleft()[-1]
                        self._counters["media_window_byte_evictions"] += 1
                    continue
                cutoff = now - self._pre_ns
                while ring and ring[0][1] < cutoff:
                    ring_bytes -= ring.popleft()[-1]
                    self._counters["media_window_time_evictions"] += 1
                if finished:
                    break
                self._wake.wait(timeout=min(0.1, max(0, (self._deadline_ns - now) / 1e9)))
        except Exception:
            self._failed = True
            self._counters["writer_failures"] += 1
        finally:
            with self._lock:
                self._closing = True
                if self._stop_ns is None:
                    self._stop_ns = time.monotonic_ns()
                    self._stop_reason = "writer_failure"
                self._counters["discarded_on_failure"] += len(self._queue)
                self._queue.clear()
                self._queue_bytes = 0
            for file in files.values():
                try:
                    file.flush()
                    os.fsync(file.fileno())
                    file.close()
                except Exception:
                    self._failed = True
                    self._counters["writer_failures"] += 1
                finally:
                    try:
                        file.close()
                    except Exception:
                        self._failed = True
            try:
                result = self.status()
                result.update(
                    state="failed" if self._failed else "closed",
                    finalized=True,
                    finished=True,
                    cleanup_pending=False,
                    active=False,
                    accepting=False,
                    trace_complete=not any(self._counters[k] for k in LOSS_COUNTERS),
                )
                result["files"] = {
                    name: {"bytes": self._writer_bytes[name], "sha256": hashes[name].hexdigest()}
                    for name in files
                }
                result["media_window"] = {
                    "selection": "first_trigger_or_final_window",
                    "independently_decodable": "not_guaranteed",
                    "note": "encoded_payloads_require_codec_metadata_and_reference_frames",
                }
                with self._open("manifest.json") as output:
                    output.write(self._json(result))
                    output.flush()
                    os.fsync(output.fileno())
                os.fsync(self._directory_fd)
            except Exception:
                self._failed = True
                self._counters["writer_failures"] += 1
            finally:
                os.close(self._directory_fd)
                self._done.set()
