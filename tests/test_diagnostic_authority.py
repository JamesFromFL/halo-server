"""Diagnostic ownership checks must not wait behind inventory/account changes."""

import statistics
import threading
import time
from types import SimpleNamespace

import pytest
from test_diagnostic_capture import PausedCapture
from test_native_service import Camera, service_fixture

from halo_server.camera_privacy import ring_privacy_receipt
from halo_server.media.diagnostic_capture import DiagnosticCapture
from halo_server.owner_capture import ScopedTrace
from halo_server.ring.client import RingClient, RingSessionManager


def backend_fixture(tmp_path):
    service, fixture_client, old_sessions = service_fixture(tmp_path)
    # Use real account properties and locks; only the SDK's cached inventory is
    # supplied locally. No authenticated connection or source capture is opened.
    client = RingClient()
    cached = SimpleNamespace(all_devices=fixture_client.all_devices)
    client._ring = SimpleNamespace(devices=lambda: cached)
    client._authenticated = True
    sessions = RingSessionManager(None)
    sessions._client, sessions._active_scope = client, old_sessions.active_scope
    service._sessions, service._media_client = sessions, client
    service._camera_privacy.observe(
        sessions.active_scope,
        client,
        cached.all_devices,
        ring_privacy_receipt(cached.all_devices, time.monotonic()),
    )
    backend = service._owner_admin.control_backend
    scope = backend.inventory().cameras[0].scope
    return backend, scope, sessions, cached


@pytest.mark.parametrize("which", ["inventory", "account", "privacy"])
def test_real_backend_contention_skips_observation_without_waiting_or_revoking(tmp_path, which):
    backend, scope, sessions, _ = backend_fixture(tmp_path)
    capture = PausedCapture(tmp_path / "locked")
    token = backend._service._camera_privacy_epoch(scope.camera_id)

    def current():
        return backend.diagnostic_scope_current(scope, privacy_token=token)

    trace = ScopedTrace(capture, current)
    lock = {
        "inventory": backend._lock,
        "account": sessions._lock,
        "privacy": backend._service._camera_privacy._lock,
    }[which]
    entered, release = threading.Event(), threading.Event()

    def hold():
        with lock:
            entered.set()
            assert release.wait(2)

    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(1)
    try:
        began = time.monotonic()
        assert current() is None
        assert not trace.event("source_rtp", "audio", seq=1)
        assert time.monotonic() - began < 0.1
        assert not trace.revoked.is_set()
    finally:
        release.set()
        worker.join(1)
        assert not worker.is_alive()
    assert current() is True
    assert trace.event("source_rtp", "audio", seq=2)
    capture.release.set()
    result = capture.close()
    assert result["finished"] and not result["trace_complete"]
    assert result["counters"]["authority_contention_drops"] == 1


def test_real_backend_checks_live_device_account_and_authentication_every_time(tmp_path):
    backend, scope, sessions, cached = backend_fixture(tmp_path)
    assert backend.diagnostic_scope_current(scope) is backend.scope_current(scope) is True
    original = cached.all_devices[0]
    cached.all_devices[0] = Camera()
    assert backend.diagnostic_scope_current(scope) is backend.scope_current(scope) is False
    cached.all_devices[0] = original
    assert backend.diagnostic_scope_current(scope) is True
    sessions._client._authenticated = False
    assert backend.diagnostic_scope_current(scope) is False
    sessions._client._authenticated = True
    sessions._active_scope = None
    assert backend.diagnostic_scope_current(scope) is False


def test_full_scoped_recorder_callback_cost_is_bounded_and_reported(tmp_path):
    backend, scope, _, _ = backend_fixture(tmp_path)
    capture = DiagnosticCapture(tmp_path / "callback-cost")
    trace = ScopedTrace(capture, lambda: backend.diagnostic_scope_current(scope))
    costs = []
    payload = bytes(1200)
    for sequence in range(2000):
        begin = time.perf_counter_ns()
        trace.event("source_rtp", "video", seq=sequence, rtp_timestamp=sequence * 3750)
        trace.media("source_rtp", "video", payload, seq=sequence, rtp_timestamp=sequence * 3750)
        costs.append(time.perf_counter_ns() - begin)
    result = capture.close()
    assert result["finished"]
    assert result["queue_high_water_bytes"] <= result["limits"]["queue_bytes"]
    ordered = sorted(costs)
    print(
        {
            "scoped_event_and_media_pairs": len(costs),
            "median_us": round(statistics.median(costs) / 1000, 2),
            "p99_us": round(ordered[int(len(ordered) * 0.99)] / 1000, 2),
            "max_us": round(max(costs) / 1000, 2),
            "total_ms": round(sum(costs) / 1_000_000, 2),
            "counters": result["counters"],
        }
    )
