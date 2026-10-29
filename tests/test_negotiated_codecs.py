"""Declared answer versus actual receiver interpretation, never raw SDP output."""

import asyncio
import gc
import json
import weakref
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiortc import RTCRtpReceiver
from aiortc.rtcpeerconnection import find_common_codecs
from aiortc.rtcrtpparameters import RTCRtcpFeedback, RTCRtpCodecParameters

from halo_server.media.negotiated_codecs import NegotiatedCodecs, sanitize_codec_diagnostics
from halo_server.media.ring_media import (
    install_encoded_frame_adapter,
    observe_receiver_statistics,
)


def answer(*, mode="1", profile="640029", private=False):
    fmtp = f"profile-level-id={profile};level-asymmetry-allowed=1"
    if mode is not None:
        fmtp += f";packetization-mode={mode}"
    if private:
        fmtp += ";private-token=private-secret"
    return (
        "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\n"
        "m=video 9 UDP/TLS/RTP/SAVPF 100\r\nc=IN IP4 0.0.0.0\r\n"
        "a=mid:0\r\na=sendonly\r\na=rtpmap:100 H264/90000\r\n"
        f"a=fmtp:100 {fmtp}\r\n"
        "a=rtcp-fb:100 nack\r\na=rtcp-fb:100 nack pli\r\n"
        "a=extmap:3 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time\r\n"
        "a=extmap:4 urn:private-secret\r\n"
        "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\nc=IN IP4 0.0.0.0\r\n"
        "a=mid:1\r\na=sendrecv\r\na=rtpmap:111 opus/48000/2\r\n"
        "a=fmtp:111 stereo=0;sprop-stereo=0;useinbandfec=1;usedtx=0;"
        "maxplaybackrate=16000;private-token=private-secret\r\n"
    )


def high(profile="640032"):
    return RTCRtpCodecParameters(
        mimeType="video/H264",
        clockRate=90000,
        payloadType=100,
        parameters={
            "profile-level-id": profile,
            "packetization-mode": "1",
            "level-asymmetry-allowed": "1",
        },
        rtcpFeedback=[RTCRtcpFeedback(type="nack", parameter="pli")],
    )


def test_answer_preserves_exact_remote_level_separate_from_receiver_interpretation():
    owner = NegotiatedCodecs(current=lambda: True)
    owner.answer(answer(private=True))
    common = find_common_codecs([high("640032")], [high("640029")])
    assert common[0].parameters["profile-level-id"] == "640032"
    owner.frame("video", common[0])
    snapshot = owner.snapshot()
    assert snapshot["answer_state"] == "observed"
    video = snapshot["answer"]["video"]
    assert video["codecs"][0]["profile_level_id"] == "640029"
    assert video["codecs"][0]["packetization_mode"] == 1
    assert video["codecs"][0]["packetization_mode_explicit"] is True
    assert video["extensions"]["abs_send_time"] is True
    assert video["extensions"]["other"] is True
    assert snapshot["encoded_frames"]["video"]["receiver_codec"]["profile_level_id"] == "640032"
    assert snapshot["answer"]["audio"]["codecs"][0]["opus"]["maxplaybackrate"] == 16000
    assert "private" not in json.dumps(snapshot)
    assert "0.0.0.0" not in json.dumps(snapshot)
    assert not hasattr(owner, "sdp")


def test_implicit_mode_zero_is_distinct_from_explicit_and_invalid_values():
    for mode, expected, explicit in [
        (None, 0, False),
        ("0", 0, True),
        ("1", 1, True),
        ("9", None, True),
    ]:
        owner = NegotiatedCodecs(current=lambda: True)
        owner.answer(answer(mode=mode))
        codec = owner.snapshot()["answer"]["video"]["codecs"][0]
        assert (codec["packetization_mode"], codec["packetization_mode_explicit"]) == (
            expected,
            explicit,
        )


def test_malformed_oversized_private_fields_and_status_schema_are_bounded():
    owner = NegotiatedCodecs(current=lambda: True)
    for value in [None, "private-secret" * 10000, answer(profile="private-secret")]:
        owner.answer(value)
        snapshot = owner.snapshot()
        assert "private" not in json.dumps(snapshot)
    owner.answer(answer())
    value = owner.snapshot()
    value["secret"] = "private-secret"
    value["answer"]["audio"]["secret"] = "private-secret"
    safe = sanitize_codec_diagnostics(value)
    assert "private" not in json.dumps(safe)
    for item in [float("nan"), True, -1, 2**53]:
        value = owner.snapshot()
        value["encoded_frames"]["audio"]["observations"] = item
        assert sanitize_codec_diagnostics(value) is None
    assert len(json.dumps(owner.snapshot())) < 4096


def test_media_and_codec_allocation_limits_are_checked_before_diagnostic_parse():
    values = [
        answer() + "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n" * 3,
        answer() + "a=rtpmap:112 opus/48000/2\r\n" * 16,
    ]
    for value in values:
        owner = NegotiatedCodecs(current=lambda: True)
        with patch("halo_server.media.negotiated_codecs.SessionDescription.parse") as parse:
            owner.answer(value)
        parse.assert_not_called()
        assert owner.snapshot()["answer_state"] == "unavailable"


def test_current_source_fence_and_weak_codec_references_do_not_retain_unknown_parameters():
    current = [True]
    owner = NegotiatedCodecs(current=lambda: current[0])
    codec = high()
    codec.parameters["private"] = "private-secret"
    ref = weakref.ref(codec)
    for _ in range(10000):
        owner.frame("video", codec)
    assert owner.snapshot()["encoded_frames"]["video"]["observations"] == 10000
    assert len(owner._objects) == 1
    del codec
    gc.collect()
    assert ref() is None
    current[0] = False
    owner.answer(answer())
    owner.frame("video", high())
    assert owner.snapshot() is None and not owner._objects


def test_real_receiver_queue_observation_preserves_original_frame_and_decoder_forwarding():
    receiver = RTCRtpReceiver("video", SimpleNamespace(state="new", _stats_id="synthetic"))
    owner = NegotiatedCodecs(current=lambda: True)
    media = []
    adapter = install_encoded_frame_adapter(
        receiver,
        "video",
        lambda data, pts: media.append((data, pts)),
        forward_to_decoder=True,
        on_codec=owner.frame,
    )
    codec, frame = high(), SimpleNamespace(data=b"synthetic-h264", timestamp=123)
    item = (codec, frame)
    adapter.put(item)
    assert media == [(frame.data, frame.timestamp)] and adapter.get_nowait() is item
    assert owner.snapshot()["encoded_frames"]["video"]["observations"] == 1

    def broken(*_args):
        raise KeyboardInterrupt

    adapter._on_codec = broken
    adapter.put(item)
    assert adapter.get_nowait() is item and len(media) == 2
    adapter.put(None)
    assert adapter.get_nowait() is None


@pytest.mark.asyncio
async def test_answer_metadata_survives_public_stats_failure_without_an_extra_sampler():
    stop, terminal = asyncio.Event(), asyncio.Event()
    owner = NegotiatedCodecs(current=lambda: not stop.is_set())
    owner.answer(answer())
    snapshots = []

    async def failed_stats():
        raise RuntimeError("synthetic-private-error")

    def observed(snapshot, *_args):
        snapshots.append(snapshot)
        stop.set()

    receiver = SimpleNamespace(getStats=failed_stats)
    async with asyncio.timeout(1):
        await observe_receiver_statistics(
            receiver,
            receiver,
            stop,
            terminal,
            observed,
            codecs=owner,
            interval=0.001,
            wake_interval=0.001,
        )
    assert len(snapshots) == 1
    assert snapshots[0]["audio"] is snapshots[0]["video"] is None
    assert snapshots[0]["codec_negotiation"]["answer_state"] == "observed"
    assert "private" not in json.dumps(snapshots)
    assert owner.snapshot() is None
