"""Offline inspection, deterministic audio assembly replay and PCM export.

No Ring credentials, network sockets or live receiver are used. The payload
excerpt starts with an empty decoder/jitter buffer; missing pre-window state is
never inferred. WAV exports concatenate decoded samples, while timing sidecars
retain arrival/source timing so smooth file playback cannot hide transport gaps.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import struct
import wave
from collections import Counter
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

from halo_server.media.diagnostic_capture import (
    LOSS_COUNTERS,
    MAX_EVENT_BYTES,
    MAX_EVENTS,
    MAX_INDEX_BYTES,
    MAX_MEDIA_BYTES,
    MAX_PAYLOAD_BYTES,
    _private_directory,
    _validated_fields,
)

_FILES = {
    "events.jsonl": MAX_EVENT_BYTES,
    "media.jsonl": MAX_INDEX_BYTES,
    "media.bin": MAX_MEDIA_BYTES,
}
_MAX_PACKETS = 20_000


class CaptureReadError(ValueError):
    """Artifacts are unsafe, unfinished, corrupt or outside bounded schema 1."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CaptureReadError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise CaptureReadError("nonfinite_json_number")


def _json(data):
    try:
        return json.loads(
            data,
            object_pairs_hook=_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CaptureReadError("invalid_json") from exc


def _scope(fields, name):
    value = fields.get(name, 0)
    if type(value) is not int or not 0 <= value < 2**53:
        raise CaptureReadError("invalid_replay_scope")
    return value


def _read(directory_fd, name, limit):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        with os.fdopen(fd, "rb") as file:
            before = os.fstat(file.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_mode & 0o077
                or before.st_nlink != 1
                or before.st_size > limit
            ):
                raise CaptureReadError("unsafe_capture_file")
            value = file.read(limit + 1)
            after = os.fstat(file.fileno())
            if (
                len(value) != before.st_size
                or len(value) > limit
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            ):
                raise CaptureReadError("capture_changed_during_read")
            return value
    except OSError as exc:
        raise CaptureReadError("capture_file_unavailable") from exc


def _rows(raw, *, media=False):
    result = []
    for line in raw.splitlines():
        if not 0 < len(line) <= 16384 or len(result) >= MAX_EVENTS:
            raise CaptureReadError("capture_row_limit")
        row = _json(line)
        expected = {"monotonic_ns", "stage", "track", "fields"}
        if media:
            expected |= {"offset", "length"}
        if (
            type(row) is not dict
            or set(row) != expected
            or type(row["monotonic_ns"]) is not int
            or not 0 <= row["monotonic_ns"] < 2**64
            or type(row["fields"]) is not dict
            or type(row["stage"]) is not str
            or type(row["track"]) is not str
            or _validated_fields(row["stage"], row["track"], row["fields"]) is None
        ):
            raise CaptureReadError("invalid_capture_row")
        result.append(row)
    return result


class CaptureArtifact:
    def __init__(self, directory):
        path = Path(directory).absolute()
        if path.resolve(strict=True) != path:
            raise CaptureReadError("symlink_capture_directory")
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise CaptureReadError("unsafe_capture_directory")
            self.manifest = _json(_read(fd, "manifest.json", 65536))
            manifest = self.manifest
            if (
                type(manifest) is not dict
                or type(manifest.get("schema_version")) is not int
                or manifest["schema_version"] != 1
                or manifest.get("finalized") is not True
                or manifest.get("cleanup_pending") is not False
                or type(manifest.get("trace_complete")) is not bool
                or type(manifest.get("files")) is not dict
                or set(manifest["files"]) != set(_FILES)
            ):
                raise CaptureReadError("unfinished_or_unsupported_capture")
            data = {}
            for name, limit in _FILES.items():
                data[name] = _read(fd, name, limit)
                recorded = manifest["files"][name]
                if (
                    type(recorded) is not dict
                    or type(recorded.get("bytes")) is not int
                    or recorded.get("bytes") != len(data[name])
                    or recorded.get("sha256") != hashlib.sha256(data[name]).hexdigest()
                ):
                    raise CaptureReadError("capture_hash_mismatch")
        finally:
            os.close(fd)
        self.events = _rows(data["events.jsonl"])
        self.media = _rows(data["media.jsonl"], media=True)
        self.payload = data["media.bin"]
        end = 0
        for row in self.media:
            offset, length = row["offset"], row["length"]
            if (
                type(offset) is not int
                or type(length) is not int
                or offset != end
                or not 0 < length <= MAX_PAYLOAD_BYTES
                or offset + length > len(self.payload)
            ):
                raise CaptureReadError("invalid_media_index")
            end += length
        if end != len(self.payload):
            raise CaptureReadError("unindexed_media_bytes")
        counters = manifest.get("counters")
        if type(counters) is not dict or any(
            type(counters.get(key)) is not int or counters[key] < 0 for key in LOSS_COUNTERS
        ):
            raise CaptureReadError("invalid_loss_counters")
        self.trace_complete = manifest["trace_complete"] and not any(
            counters[key] for key in LOSS_COUNTERS
        )

    def bytes_for(self, row):
        return self.payload[row["offset"] : row["offset"] + row["length"]]

    def summary(self):
        return {
            "schema_version": 1,
            "verified_file_hashes": True,
            "trace_complete": self.trace_complete,
            "timing_events": len(self.events),
            "media_records": len(self.media),
            "media_bytes": len(self.payload),
            "timing_stages": dict(Counter(row["stage"] for row in self.events)),
            "media_stages": dict(Counter(row["stage"] for row in self.media)),
            "diagnostic_losses": {
                key: self.manifest["counters"][key]
                for key in LOSS_COUNTERS
                if self.manifest["counters"][key]
            },
            "media_is_bounded_excerpt": True,
            "pre_window_decoder_state_available": False,
        }


async def replay_audio(artifact):
    """Feed captured Opus RTP to real aiortc without sleeping or networking."""
    from aiortc.rtcrtpparameters import RTCRtpCodecParameters
    from aiortc.rtcrtpreceiver import RTCRtpReceiver
    from aiortc.rtp import RtpPacket

    from halo_server.media.diagnostic_ingress import install_audio_jitter_trace

    rows = [
        row
        for row in artifact.media
        if row["stage"] == "source_rtp"
        and row["track"] == "audio"
        and row["fields"].get("codec") == "opus"
    ]
    if len(rows) > _MAX_PACKETS:
        raise CaptureReadError("replay_packet_limit")
    rows.sort(key=lambda row: row["monotonic_ns"])
    groups = {}
    for row in rows:
        fields = row["fields"]
        key = (_scope(fields, "source_generation"), _scope(fields, "attempt"))
        groups.setdefault(key, []).append(row)
    reports = []
    for (generation, attempt), records in groups.items():
        receiver = RTCRtpReceiver("audio", SimpleNamespace(state="new"))
        current = [records[0]["monotonic_ns"]]
        frames, jitter = [], []

        class Observer:
            def __init__(self, current, jitter):
                self.current, self.jitter = current, jitter

            def event(self, stage, _track, **fields):
                if stage == "audio_jitter_after":
                    self.jitter.append({"monotonic_ns": self.current[0], **fields})

            def trigger(self, _reason):
                return True

        class Queue:
            def __init__(self, current, frames):
                self.current, self.frames = current, frames

            def put(self, item):
                frame = item[1]
                self.frames.append(
                    {
                        "monotonic_ns": self.current[0],
                        "timestamp": frame.timestamp,
                        "bytes": len(frame.data),
                        "sha256": hashlib.sha256(frame.data).hexdigest(),
                    }
                )

        receiver._RTCRtpReceiver__decoder_queue = Queue(current, frames)
        receiver._RTCRtpReceiver__decoder_thread = True
        install_audio_jitter_trace(
            receiver, Observer(current, jitter), clock=lambda current=current: current[0] / 1e9
        )
        arrivals = []
        for row in records:
            fields = row["fields"]
            values = (fields.get("seq"), fields.get("rtp_timestamp"), fields.get("payload_type"))
            if any(type(value) is not int for value in values):
                raise CaptureReadError("missing_rtp_replay_metadata")
            sequence, timestamp, payload_type = values
            if not (0 <= sequence < 65536 and 0 <= timestamp < 2**32 and 0 <= payload_type < 128):
                raise CaptureReadError("invalid_rtp_replay_metadata")
            receiver._RTCRtpReceiver__codecs[payload_type] = RTCRtpCodecParameters(
                mimeType="audio/opus", clockRate=48000, channels=2, payloadType=payload_type
            )
            packet = RtpPacket(
                payload_type=payload_type,
                sequence_number=sequence,
                timestamp=timestamp,
                marker=int(bool(fields.get("marker", False))),
            )
            packet.ssrc = 1  # Synthetic routing identity; original identifiers are never stored.
            packet.payload = artifact.bytes_for(row)
            current[0] = row["monotonic_ns"]
            arrivals.append(current[0])
            await receiver._handle_rtp_packet(packet, current[0] // 1_000_000)
        reports.append(
            {
                "source_generation": generation,
                "attempt": attempt,
                "packets": len(records),
                "frame_count": len(frames),
                "frames": frames,
                "jitter_decisions": jitter,
                "max_arrival_gap_ms": max(
                    ((b - a) / 1e6 for a, b in pairwise(arrivals)), default=0
                ),
                "max_release_gap_ms": max(
                    ((b["monotonic_ns"] - a["monotonic_ns"]) / 1e6 for a, b in pairwise(frames)),
                    default=0,
                ),
                "starts_with_empty_jitter_buffer": True,
            }
        )
    return {
        "schema_version": 1,
        "kind": "offline_aiortc_audio_replay",
        "network_used": False,
        "trace_complete": artifact.trace_complete,
        "pre_window_state_restored": False,
        "groups": reports,
    }


def export_audio(artifact, directory):
    """Independently decode source/output Opus; keep arrival timing beside WAV."""
    import av

    groups = {}
    for row in artifact.media:
        if row["track"] != "audio" or row["stage"] not in {"source_frame", "encoded_output"}:
            continue
        fields = row["fields"]
        key = (
            row["stage"],
            _scope(fields, "source_generation"),
            _scope(fields, "attempt"),
            _scope(fields, "capture_epoch"),
            _scope(fields, "stream_id"),
        )
        groups.setdefault(key, []).append(row)
    if sum(map(len, groups.values())) > _MAX_PACKETS or len(groups) > 64:
        raise CaptureReadError("audio_export_limit")
    path, fd = _private_directory(directory)
    results = []
    total_samples = 0
    try:
        for index, (scope, rows) in enumerate(sorted(groups.items())):
            decoder = av.CodecContext.create("libopus", "r")
            decoder.thread_count = 1
            decoder.extradata = b"OpusHead\x01\x02\0\0" + struct.pack("<I", 48000) + b"\0\0\0"
            resampler = av.AudioResampler(format="s16", layout="stereo", rate=48000)
            name = f"audio-{index:02d}-{scope[0]}"
            output_fd = os.open(
                name + ".wav",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=fd,
            )
            timing, samples, errors = [], 0, 0
            with os.fdopen(output_fd, "wb") as file, wave.open(file, "wb") as wav:
                wav.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                for row in sorted(rows, key=lambda row: row["monotonic_ns"]):
                    start = samples
                    failed = False
                    try:
                        for frame in decoder.decode(av.Packet(artifact.bytes_for(row))):
                            for pcm in resampler.resample(frame):
                                if (
                                    samples + pcm.samples > 48000 * 90
                                    or (total_samples + pcm.samples) * 4 > MAX_MEDIA_BYTES
                                ):
                                    raise CaptureReadError("decoded_audio_limit")
                                wav.writeframesraw(
                                    pcm.to_ndarray().astype("<i2", copy=False).tobytes()
                                )
                                samples += pcm.samples
                                total_samples += pcm.samples
                    except av.FFmpegError:
                        errors += 1
                        failed = True
                    timing.append(
                        {
                            "monotonic_ns": row["monotonic_ns"],
                            "fields": row["fields"],
                            "wav_start_sample": start,
                            "wav_samples": samples - start,
                            "decode_failed": failed,
                        }
                    )
            sidecar = {
                "schema_version": 1,
                "sample_rate": 48000,
                "channels": 2,
                "decoder": "libopus",
                "trace_complete": artifact.trace_complete,
                "timing": "concatenated_samples_use_sidecar_for_delivery_gaps",
                "pre_window_decoder_state_restored": False,
                "packets": timing,
            }
            _write_json(fd, name + ".json", sidecar)
            results.append(
                {
                    "wav": name + ".wav",
                    "sidecar": name + ".json",
                    "samples": samples,
                    "decode_errors": errors,
                }
            )
        report = {"schema_version": 1, "trace_complete": artifact.trace_complete, "files": results}
        _write_json(fd, "export.json", report)
    finally:
        os.close(fd)
    return {"directory": str(path), **report}


def _write_json(directory_fd, name, value):
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd
    )
    with os.fdopen(fd, "w") as file:
        json.dump(value, file, separators=(",", ":"), allow_nan=False)
        file.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("inspect", "replay-audio", "export-audio"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output-directory", type=Path)
    args = parser.parse_args(argv)
    try:
        artifact = CaptureArtifact(args.directory)
        if args.operation == "inspect":
            result = artifact.summary()
        elif args.operation == "replay-audio":
            result = asyncio.run(replay_audio(artifact))
        elif args.output_directory is None:
            parser.error("export-audio requires --output-directory")
        else:
            result = export_audio(artifact, args.output_directory)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (CaptureReadError, OSError) as exc:
        # File names and media are never interpolated into failure messages.
        print(json.dumps({"error": "capture_read_or_export_failed", "kind": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
