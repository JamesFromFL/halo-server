"""Private artifact integrity and actual offline Opus assembly/decoding."""

import hashlib
import json
import os
import wave

import pytest

from halo_server.media.diagnostic_capture import LOSS_COUNTERS, DiagnosticCapture
from halo_server.media.diagnostic_replay import (
    CaptureArtifact,
    CaptureReadError,
    export_audio,
    replay_audio,
)

PAYLOAD = b"\xf8\xff\xfe"


def write_artifact(path, rows, *, counters=None):
    path.mkdir(mode=0o700)
    data, indexed, events = b"", [], []
    for record, payload in rows:
        indexed.append({**record, "offset": len(data), "length": len(payload)})
        events.append(record)
        data += payload
    files = {
        "events.jsonl": b"".join((json.dumps(row) + "\n").encode() for row in events),
        "media.jsonl": b"".join((json.dumps(row) + "\n").encode() for row in indexed),
        "media.bin": data,
    }
    manifest = {
        "schema_version": 1,
        "finalized": True,
        "cleanup_pending": False,
        "trace_complete": True,
        "counters": dict.fromkeys(LOSS_COUNTERS, 0),
        "files": {},
    }
    manifest["counters"].update(counters or {})
    for name, payload in files.items():
        (path / name).write_bytes(payload)
        (path / name).chmod(0o600)
        manifest["files"][name] = {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    (path / "manifest.json").write_text(json.dumps(manifest))
    (path / "manifest.json").chmod(0o600)
    return path


def rtp_rows():
    rows = []
    for sequence in (0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 3):
        ns = (10_000 + (700 if sequence == 3 else sequence * 60)) * 1_000_000
        rows.append(
            (
                {
                    "monotonic_ns": ns,
                    "stage": "source_rtp",
                    "track": "audio",
                    "fields": {
                        "seq": sequence,
                        "rtp_timestamp": sequence * 2880,
                        "payload_type": 111,
                        "marker": True,
                        "codec": "opus",
                        "source_generation": 1,
                        "attempt": 1,
                    },
                },
                PAYLOAD,
            )
        )
    return rows


@pytest.mark.asyncio
async def test_offline_replay_preserves_payload_hashes_and_reproduces_real_missing_sequence_hold(
    tmp_path,
):
    artifact = CaptureArtifact(write_artifact(tmp_path / "capture", rtp_rows()))
    report = await replay_audio(artifact)
    group = report["groups"][0]
    assert group["packets"] == 11 and group["frame_count"] == 1
    assert group["frames"] == [
        {
            "monotonic_ns": 10_700_000_000,
            "timestamp": 0,
            "bytes": 3,
            "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
        }
    ]
    held = group["jitter_decisions"][-2]
    assert held["hold_ms"] == 600 and held["first_gap_offset"] == 3
    assert held["prefetch"] == 4 and held["frame_returned"] is False
    assert group["max_arrival_gap_ms"] == 120
    assert not report["network_used"] and not report["pre_window_state_restored"]
    assert artifact.summary()["media_is_bounded_excerpt"]
    assert artifact.bytes_for(artifact.media[0]) == PAYLOAD


def test_independent_audio_export_has_exact_timing_sidecar_and_private_wav(tmp_path):
    rows = [
        (
            {
                "monotonic_ns": 10_000_000_000,
                "stage": stage,
                "track": "audio",
                "fields": {"timestamp": 960, "codec": "opus"},
            },
            PAYLOAD,
        )
        for stage in ("source_frame", "encoded_output")
    ]
    artifact = CaptureArtifact(write_artifact(tmp_path / "capture", rows))
    report = export_audio(artifact, tmp_path / "export")
    assert len(report["files"]) == 2
    for item in report["files"]:
        assert item["decode_errors"] == 0 and item["samples"] == 960
        path = tmp_path / "export" / item["wav"]
        assert path.stat().st_mode & 0o777 == 0o600
        with wave.open(str(path), "rb") as wav:
            assert wav.getnframes() == 960 and wav.getnchannels() == 2
            assert len(wav.readframes(960)) == 3840
        sidecar = json.loads((tmp_path / "export" / item["sidecar"]).read_bytes())
        assert sidecar["packets"][0]["monotonic_ns"] == 10_000_000_000
        assert sidecar["packets"][0]["fields"]["timestamp"] == 960
        assert sidecar["timing"] == "concatenated_samples_use_sidecar_for_delivery_gaps"


@pytest.mark.parametrize("kind", ["hash", "symlink", "permissions", "directory_link", "hardlink"])
def test_reader_rejects_corrupt_or_nonprivate_artifacts(tmp_path, kind):
    path = write_artifact(tmp_path / "capture", rtp_rows())
    if kind == "hash":
        (path / "media.bin").write_bytes(b"corrupt")
    elif kind == "symlink":
        (path / "media.bin").rename(tmp_path / "outside")
        (path / "media.bin").symlink_to(tmp_path / "outside")
    elif kind == "permissions":
        (path / "media.bin").chmod(0o644)
    elif kind == "hardlink":
        os.link(path / "media.bin", tmp_path / "outside")
    else:
        (tmp_path / "link").symlink_to(path)
        path = tmp_path / "link"
    with pytest.raises(CaptureReadError):
        CaptureArtifact(path)


def test_incomplete_timing_is_never_promoted_to_complete_by_manifest_flag(tmp_path):
    artifact = CaptureArtifact(
        write_artifact(tmp_path / "capture", rtp_rows(), counters={"queue_drops": 1})
    )
    assert not artifact.trace_complete
    assert artifact.summary()["diagnostic_losses"] == {"queue_drops": 1}


def test_index_bounds_verified_even_when_hash_matches(tmp_path):
    path = write_artifact(tmp_path / "capture", rtp_rows())
    rows = [json.loads(line) for line in (path / "media.jsonl").read_text().splitlines()]
    rows[0]["length"] = 1000000
    data = b"".join((json.dumps(row) + "\n").encode() for row in rows)
    (path / "media.jsonl").write_bytes(data)
    manifest = json.loads((path / "manifest.json").read_bytes())
    manifest["files"]["media.jsonl"] = {
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(CaptureReadError, match="invalid_media_index"):
        CaptureArtifact(path)


def test_real_capture_is_readable_and_preserves_exact_payload(tmp_path):
    capture = DiagnosticCapture(tmp_path / "capture", duration_seconds=5)
    assert capture.event("source_rtp", "audio", seq=1, codec="opus")
    assert capture.media("source_rtp", "audio", PAYLOAD, seq=1, codec="opus")
    assert capture.close()["finalized"]
    artifact = CaptureArtifact(tmp_path / "capture")
    assert artifact.summary()["media_records"] == 1
    assert artifact.bytes_for(artifact.media[0]) == PAYLOAD
