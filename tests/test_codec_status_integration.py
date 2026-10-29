"""Independent codec evidence survives stats gaps but never source retirement."""

import json

from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from test_media_ingress_diagnostics import session_fixture

from halo_server.media import session as module
from halo_server.media.negotiated_codecs import NegotiatedCodecs


def snapshot():
    owner = NegotiatedCodecs(current=lambda: True)
    owner.frame(
        "video",
        RTCRtpCodecParameters(
            mimeType="video/H264",
            clockRate=90000,
            payloadType=101,
            parameters={"profile-level-id": "640032", "packetization-mode": "1"},
        ),
    )
    return owner.snapshot()


def test_independent_codec_metadata_survives_missing_public_stats_but_not_staleness_or_stop():
    session, _, _, callbacks = session_fixture()
    now = [10.0]
    session._diagnostic_clock = lambda: now[0]
    value = snapshot()
    value["secret"] = "private-value"
    value["encoded_frames"]["video"]["receiver_codec"]["secret"] = "private-value"
    try:
        callbacks["on_receiver_stats"]({"codec_negotiation": value}, 0, 0)
        diagnostic = session.diagnostics()
        assert diagnostic["receiver_stats"] == {"audio": None, "video": None}
        assert diagnostic["codec_negotiation"]["encoded_frames"]["video"]["observations"] == 1
        assert "private-value" not in json.dumps(diagnostic)
        diagnostic["codec_negotiation"]["encoded_frames"]["video"]["observations"] = 999
        assert (
            session.diagnostics()["codec_negotiation"]["encoded_frames"]["video"]["observations"]
            == 1
        )
        now[0] = 13.1
        assert session.diagnostics()["codec_negotiation"] is None
        callbacks["on_receiver_stats"]({"codec_negotiation": snapshot()}, 0, 0)
        assert session.diagnostics()["codec_negotiation"] is not None
        session.stop_all()
        callbacks["on_receiver_stats"]({"codec_negotiation": snapshot()}, 0, 0)
        assert session.diagnostics()["codec_negotiation"] is None
    finally:
        session.stop_all()


def test_revision_change_during_codec_sanitization_rejects_late_snapshot(monkeypatch):
    session, _, _, callbacks = session_fixture()
    original = module.sanitize_codec_diagnostics

    def retired(value):
        safe = original(value)
        session._clear_media_locked()
        return safe

    monkeypatch.setattr(module, "sanitize_codec_diagnostics", retired)
    try:
        callbacks["on_receiver_stats"]({"codec_negotiation": snapshot()}, 0, 0)
        assert session.diagnostics()["codec_negotiation"] is None
        assert session._receiver_statistics_at is None
    finally:
        session.stop_all()
