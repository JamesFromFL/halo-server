"""Harness controls, independent of external reference installations."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1] / "tools/receiver_comparison/run.py"
spec = importlib.util.spec_from_file_location("receiver_comparison", HARNESS)
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


@pytest.fixture(scope="module")
def frames():
    return comparison.source_frames()


def test_synthetic_source_is_independently_decodable_and_large_idr_exceeds_old_capacity():
    large = comparison.source_frames(True)
    trace = comparison.make_trace("large_idr", large)
    assert trace["source_decoded_pictures"] == 12
    assert 128 < trace["aus"][0]["packets"] < 512


def test_loss_reorder_and_wrap_change_only_requested_trace_properties(frames):
    normal = comparison.make_trace("regular", frames)
    loss = comparison.make_trace("loss", frames)
    reordered = comparison.make_trace("reorder", frames)
    wrapped = comparison.make_trace("wrap", frames)
    assert len(loss["packets"]) == len(normal["packets"]) - 1
    assert len(loss["omitted_packet_ids"]) == 1
    assert {p["wire"] for p in reordered["packets"]} == {p["wire"] for p in normal["packets"]}
    assert reordered["packets"][3]["id"] == normal["packets"][4]["id"]
    assert any(a["timestamp"] < wrapped["aus"][0]["timestamp"] for a in wrapped["aus"])
    assert any(p["sequence"] < 65500 for p in wrapped["packets"])


def test_changed_source_hash_fails_closed(tmp_path):
    source = tmp_path / "source.ts"
    source.write_text("modified source")
    with pytest.raises(ValueError, match="hash mismatch"):
        comparison.checked_hash(source, "0" * 64)


def test_missing_references_are_reported_and_never_replaced_by_a_fixture(tmp_path):
    output = tmp_path / "report.json"
    child = subprocess.run(
        [sys.executable, str(HARNESS), "--output", str(output)], timeout=5, check=False
    )
    assert child.returncode == 2
    report = json.loads(output.read_text())
    assert report["cases"] == {}
    assert all(r["status"] == "unavailable" for r in report["references"].values())


def test_bounded_child_timeout_terminates_instead_of_returning_a_result():
    with pytest.raises(RuntimeError, match="was killed"):
        comparison.run_child([sys.executable, "-c", "import time; time.sleep(5)"], {}, 0.1)


@pytest.mark.asyncio
async def test_explicit_legacy_control_waits_for_next_frame_boundary_and_closes(frames):
    regular = comparison.make_trace("regular", frames)
    delayed = comparison.make_trace("delayed_boundary", frames)
    baseline = await comparison.halo_child({"trace": regular, "stock_buffer": True})
    waiting = await comparison.halo_child({"trace": delayed, "stock_buffer": True})
    assert len(baseline["assembled"]) == len(frames) - 1
    assert baseline["output_digest"] == waiting["output_digest"]
    assert waiting["assembled"][0]["ms"] - baseline["assembled"][0]["ms"] > 120
    assert baseline["cleanup"]["peer_closed"]
    assert waiting["cleanup"]["sockets_closed"]


@pytest.mark.asyncio
async def test_timestamp_instrumentation_preserves_actual_halo_output(frames):
    trace = comparison.make_trace("regular", frames)
    observed = await comparison.halo_child({"trace": trace, "instrument": True})
    control = await comparison.halo_child({"trace": trace, "instrument": False})
    assert observed["output_digest"] == control["output_digest"]
    assert len(observed["forwarded"]) == len(control["forwarded"])
    assert all(row["ms"] == 0 for row in control["arrivals"])


@pytest.mark.asyncio
async def test_early_close_preserves_only_complete_access_units(frames):
    trace = comparison.make_trace("cancel", frames)
    result = await comparison.halo_child({"trace": trace})
    complete = {row["au"] for row in result["assembled"]}
    assert {0, 1}.issubset(complete)
    assert complete.issubset({0, 1, 2})
    assert len(result["assembled"]) == len(result["forwarded"])
    assert result["cleanup"]["peer_closed"]
    assert result["cleanup"]["sockets_closed"]


@pytest.mark.asyncio
async def test_loss_recovery_hash_identity_anchors_clock_at_first_released_au(frames):
    trace = comparison.make_trace("loss", frames)
    result = await comparison.halo_child({"trace": trace})
    assert [row["au"] for row in result["assembled"]] == list(range(6, 12))
    assert comparison.summarize(result, trace)["complete_aus"] == 6
    assert result["timestamp_origin_au"] == 6
    assert result["timestamp_origin_source_rtp"] == trace["aus"][6]["timestamp"]
    assert result["timestamp_mapping_valid"] is True
    assert [row["mapped_timestamp"] for row in result["forwarded"]] == list(range(0, 18000, 3000))
    assert [row["au"] for row in result["sink"]] == list(range(6, 12))


def test_valid_content_does_not_hide_incorrect_timestamp_mapping(frames):
    trace = comparison.make_trace("regular", frames)
    result = {
        "forwarded": [
            {"delivery_id": n, "sha256": trace["aus"][n]["annexb_sha256"], "mapped_timestamp": t}
            for n, t in enumerate((0, 3001, 6000))
        ],
        "egress": [],
        "sink": [],
    }
    comparison.match_halo_access_units(result, trace)
    assert result["timestamp_origin_au"] == 0
    assert result["timestamp_mapping_valid"] is False
    assert [row["au"] for row in result["assembled"]] == [0, 2]
    assert result["forwarded"][1]["payload_valid"] is True
    assert result["forwarded"][1]["timestamp_valid"] is False


@pytest.mark.asyncio
async def test_damaged_initial_stock_buffer_au_remains_negative_control():
    trace = comparison.make_trace("large_idr", comparison.source_frames(True))
    result = await comparison.halo_child({"trace": trace, "stock_buffer": True})
    assert len(result["forwarded"]) == 11
    assert [row["au"] for row in result["assembled"]] == [1, 2, 3, 4, 5, 7, 8, 9, 10]
    assert result["timestamp_origin_au"] == 0
    assert result["timestamp_origin_evidence_delivery"] == 1
    assert result["timestamp_mapping_valid"] is True
    assert result["forwarded"][0]["au"] == 0
    assert result["forwarded"][0]["payload_valid"] is False
    assert result["forwarded"][6]["payload_valid"] is False
