"""Finite Ring path observations do not acquire media authority."""

import json
import threading

import pytest
from test_media_ingress_diagnostics import session_fixture

from halo_server.media import session as module


def observed():
    return dict(
        state="observed",
        installed_before_reader=True,
        transcoding=True,
        reason="codec_mismatch",
        observations=1,
        invalid_messages=0,
        oversized_messages=0,
        age_ms=20,
    )


def test_source_status_preserves_safe_metadata_age_and_clears_at_stop():
    session, _client, _lease, callbacks = session_fixture()
    now = [10.0]
    session._diagnostic_clock = lambda: now[0]
    value = observed() | {"private": "secret-signaling-body"}
    try:
        callbacks["on_receiver_stats"]({"stream_info": value}, 0, 0)
        now[0] += 0.75
        snapshot = session.diagnostics()
        assert snapshot["ring_stream_info"] == observed() | {"age_ms": 770}
        assert "secret-signaling-body" not in json.dumps(snapshot)
        snapshot["ring_stream_info"]["transcoding"] = False
        assert session.diagnostics()["ring_stream_info"]["transcoding"] is True
        now[0] += 3
        assert session.diagnostics()["ring_stream_info"] is None
    finally:
        session.stop_all()
    callbacks["on_receiver_stats"]({"stream_info": observed()}, 0, 0)
    assert session.diagnostics()["ring_stream_info"] is None


def test_late_sanitizer_result_cannot_restore_stopped_source(monkeypatch):
    session, _client, _lease, callbacks = session_fixture()
    entered, release = threading.Event(), threading.Event()
    original = module._safe_stream_info

    def held(value):
        entered.set()
        assert release.wait(2)
        return original(value)

    monkeypatch.setattr(module, "_safe_stream_info", held)
    thread = threading.Thread(
        target=callbacks["on_receiver_stats"], args=({"stream_info": observed()}, 0, 0)
    )
    thread.start()
    try:
        assert entered.wait(2)
        session.stop_all()
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and session.diagnostics()["ring_stream_info"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        {"state": []},
        {"state": "unknown"},
        {"reason": []},
        {"reason": "private-value"},
        {"transcoding": 1},
        {"installed_before_reader": "yes"},
        {"observations": True},
        {"age_ms": -1},
        {"state": "not_seen"},
        {"reason": None},
        {"observations": 0},
        {"oversized_messages": 2**53},
    ],
)
def test_malformed_stream_info_is_unknown_not_a_report(mutation):
    assert module._safe_stream_info(observed() | mutation) is None


def test_absence_is_not_false_transcoding_and_extra_fields_do_not_escape():
    value = observed() | dict(
        state="not_seen", transcoding=None, reason=None, age_ms=None, observations=0
    )
    assert module._safe_stream_info(value)["transcoding"] is None
    assert module._safe_stream_info(None) is None


def test_observed_unknown_reason_is_finite_and_preserved():
    assert module._safe_stream_info(observed() | {"reason": "unknown"})["reason"] == "unknown"
