import math
from types import SimpleNamespace

import pytest
from test_service import FakeMediaSession, _service

from halo_server.api.admin import MAX_RESPONSE_BYTES, _encode_response
from halo_server.media.telemetry import DiagnosticBudget


def test_large_camera_status_keeps_all_core_rows_with_bounded_optional_metrics(tmp_path):
    service = _service(tmp_path)
    calls = []
    # Saturated numeric histograms exercise a much larger serialized snapshot
    # than ordinary zeros; the budget must count bytes, not merely owners.
    detail = {"histograms": [[2**53 - 1] * 12 for _ in range(300)]}

    def diagnostics():
        calls.append(True)
        return detail

    for camera in range(256):
        session = FakeMediaSession(None, SimpleNamespace(id=camera))
        session.leases.add("viewer")
        session.diagnostics = diagnostics
        service._media_sessions[str(camera)] = session

    status = service.status()
    rows = status["media"]["sessions"]
    coverage = status["media"]["diagnostic_coverage"]
    assert len(rows) == status["media"]["active_upstreams"] == 256
    assert len(calls) <= DiagnosticBudget.MAX_OWNERS
    assert 0 < coverage["included"] < DiagnosticBudget.MAX_OWNERS
    assert coverage["included"] + coverage["omitted"] == 256
    assert coverage["bytes"] <= DiagnosticBudget.MAX_BYTES
    assert all(row["lease_count"] == 1 for row in rows.values())
    wire = _encode_response({"id": "status", "ok": True, "result": status})
    assert len(wire) <= MAX_RESPONSE_BYTES


def test_diagnostic_budget_limits_getter_work_even_for_small_snapshots():
    calls = []
    owner = SimpleNamespace(diagnostics=lambda: calls.append(True) or {"count": 1})
    budget = DiagnosticBudget()
    for _ in range(256):
        budget.get(owner)
    assert len(calls) == budget.report()["included"] == 16
    assert budget.report()["omitted"] == 240


@pytest.mark.parametrize("value", [{"count": math.nan}, {"count": object()}, None])
def test_invalid_optional_diagnostics_never_break_owner_status(value):
    budget = DiagnosticBudget()
    assert budget.get(SimpleNamespace(diagnostics=lambda: value)) is None
    assert budget.report()["unavailable"] == 1
    assert budget.report()["omitted"] == 0


def test_failed_optional_getter_does_not_consume_serialized_budget():
    def broken():
        raise RuntimeError("private provider details")

    budget = DiagnosticBudget()
    assert budget.get(SimpleNamespace(diagnostics=broken)) is None
    assert budget.get(SimpleNamespace(diagnostics=lambda: {"count": 1})) == {"count": 1}
    assert budget.report()["included"] == budget.report()["unavailable"] == 1
