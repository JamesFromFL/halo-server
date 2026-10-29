"""Telemetry must remain bounded and cannot acquire signal or identity fields."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from halo_server.media.telemetry import StreamMetrics


def test_schema_is_fixed_and_snapshots_are_independent():
    metrics = StreamMetrics(counters=("frames",), maxima=("batch",), timings=("feed",))
    metrics.count("frames", 2)
    metrics.maximum("batch", 3)
    metrics.observe("feed", 0.02)
    metrics.count("private_key", 1)
    first = metrics.snapshot()
    first["counters"]["frames"] = 999
    first["timings"]["feed"]["buckets"][0] = 999
    result = metrics.snapshot()
    assert result["counters"] == {"frames": 2}
    assert result["maxima"] == {"batch": 3}
    assert result["timings"]["feed"]["count"] == 1
    assert result["timings"]["feed"]["sum_us"] == result["timings"]["feed"]["max_us"] == 20000
    assert sum(result["timings"]["feed"]["buckets"]) == 1
    assert "private_key" not in json.dumps(result)
    assert not result["overflow"]


@pytest.mark.parametrize("bad", [True, -1, 1.2, "payload", b"secret", None])
def test_invalid_counts_are_non_authoritative(bad):
    metrics = StreamMetrics(counters=("frames",), maxima=("batch",))
    metrics.count("frames", bad)
    metrics.maximum("batch", bad)
    assert metrics.snapshot()["counters"]["frames"] == 0
    assert metrics.snapshot()["maxima"]["batch"] == 0


def test_bad_timing_and_huge_values_are_bounded():
    metrics = StreamMetrics(counters=("bytes",), timings=("feed",))
    for bad in (True, -1, "private", float("nan"), float("inf")):
        metrics.observe("feed", bad)
    metrics.count("bytes", 10**100)
    metrics.observe("feed", 1e300)
    result = metrics.snapshot()
    assert result["overflow"]
    assert result["counters"]["bytes"] == 2**53 - 1
    assert result["timings"]["feed"]["max_us"] == 2**53 - 1
    assert result["timings"]["feed"]["count"] == 1
    assert result["timings"]["feed"]["buckets"][-1] == 1


def test_threaded_source_updates_and_status_reads_reconcile():
    metrics = StreamMetrics(counters=("frames",), timings=("read",))

    def update():
        for _ in range(200):
            metrics.count("frames")
            metrics.observe("read", 0.001)
            metrics.snapshot()

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _index: update(), range(4)))
    result = metrics.snapshot()
    assert result["counters"]["frames"] == result["timings"]["read"]["count"] == 800
    assert sum(result["timings"]["read"]["buckets"]) == 800


@pytest.mark.parametrize(
    "schema",
    [
        dict(counters=("secret-url",)),
        dict(counters=("x",), timings=("x",)),
        dict(counters=tuple(f"m{i}" for i in range(161))),
    ],
)
def test_bad_schema_rejected_once_at_construction(schema):
    with pytest.raises(ValueError):
        StreamMetrics(**schema)


def test_optional_diagnostic_failure_cannot_break_owner_status():
    from halo_server.media.telemetry import safe_diagnostics

    class Broken:
        def diagnostics(self):
            raise RuntimeError("private exception must stay private")

    class Wrong:
        def diagnostics(self):
            return object()

    assert safe_diagnostics(Broken()) is None
    assert safe_diagnostics(Wrong()) is None
    assert safe_diagnostics(object()) is None
