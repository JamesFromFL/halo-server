"""Fixed, content-free codec contract observations for one receive attempt."""

from __future__ import annotations

import re
import weakref

from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from aiortc.sdp import SessionDescription

MAX_SDP = 64 * 1024
MAX_CODECS = 16
MAX_COUNTER = 2**53 - 1
_KINDS = ("audio", "video")
_NAMES = {"h264", "vp8", "rtx", "opus", "pcmu", "pcma", "g722", "other"}
_DIRECTIONS = {"sendrecv", "sendonly", "recvonly", "inactive", "unknown"}
_FEEDBACK = {
    ("nack", None): "nack",
    ("nack", "pli"): "pli",
    ("ccm", "fir"): "fir",
    ("goog-remb", None): "remb",
    ("transport-cc", None): "transport_cc",
}
_EXTENSIONS = {
    "urn:ietf:params:rtp-hdrext:sdes:mid": "mid",
    "http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time": "abs_send_time",
    "urn:ietf:params:rtp-hdrext:ssrc-audio-level": "audio_level",
    "http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01": "transport_cc",
    "urn:ietf:params:rtp-hdrext:transport-wide-cc-02": "transport_cc",
}


def _number(value, maximum, *, minimum=0):
    return value if type(value) is int and minimum <= value <= maximum else None


def _parameter(value, maximum):
    if type(value) is str and 0 < len(value) <= 9 and value.isascii() and value.isdecimal():
        value = int(value)
    return _number(value, maximum)


def describe_codec(codec):
    if type(codec) is not RTCRtpCodecParameters:
        return None
    mime = codec.mimeType
    name = (
        mime.split("/")[-1].lower()
        if type(mime) is str
        and len(mime) <= 32
        and mime.lower()
        in {
            "video/h264",
            "video/vp8",
            "video/rtx",
            "audio/opus",
            "audio/pcmu",
            "audio/pcma",
            "audio/g722",
        }
        else "other"
    )
    parameters = codec.parameters
    if type(parameters) is not dict or len(parameters) > 64:
        return None
    h264 = name == "h264"
    profile = parameters.get("profile-level-id")
    profile = (
        profile.lower()
        if h264 and type(profile) is str and re.fullmatch("[0-9a-fA-F]{6}", profile)
        else None
    )
    feedback = {key: False for key in _FEEDBACK.values()}
    feedback["other"] = False
    if type(codec.rtcpFeedback) is not list or len(codec.rtcpFeedback) > 16:
        return None
    for item in codec.rtcpFeedback:
        kind, parameter = getattr(item, "type", None), getattr(item, "parameter", None)
        if (
            type(kind) is not str
            or len(kind) > 32
            or (parameter is not None and (type(parameter) is not str or len(parameter) > 32))
        ):
            feedback["other"] = True
            continue
        key = _FEEDBACK.get((kind, parameter))
        feedback[key or "other"] = True
    opus = name == "opus"
    return {
        "name": name,
        "clock_rate": _number(codec.clockRate, 1_000_000, minimum=1),
        "channels": _number(codec.channels, 8, minimum=1),
        "profile_level_id": profile,
        "packetization_mode": _parameter(parameters.get("packetization-mode", "0"), 1)
        if h264
        else None,
        "packetization_mode_explicit": h264 and "packetization-mode" in parameters,
        "level_asymmetry_allowed": _parameter(parameters.get("level-asymmetry-allowed"), 1)
        if h264
        else None,
        "feedback": feedback,
        "opus": {
            key: _parameter(parameters.get(key.replace("_", "-")), maximum) if opus else None
            for key, maximum in (
                ("stereo", 1),
                ("sprop_stereo", 1),
                ("useinbandfec", 1),
                ("usedtx", 1),
                ("maxplaybackrate", 48000),
                ("maxaveragebitrate", 510000),
            )
        },
    }


def _extensions(items):
    if type(items) is not list or len(items) > 16:
        raise ValueError
    result = {key: False for key in _EXTENSIONS.values()}
    result["other"] = False
    for item in items:
        uri = getattr(item, "uri", None)
        key = _EXTENSIONS.get(uri) if type(uri) is str else None
        result[key or "other"] = True
    return result


def _safe_codec(value):
    if type(value) is not dict or value.get("name") not in _NAMES:
        raise ValueError
    result = {"name": value["name"]}
    for key, maximum in (
        ("clock_rate", 1_000_000),
        ("channels", 8),
        ("packetization_mode", 1),
        ("level_asymmetry_allowed", 1),
    ):
        item = value.get(key)
        if (
            item is not None
            and _number(item, maximum, minimum=1 if key in {"clock_rate", "channels"} else 0)
            is None
        ):
            raise ValueError
        result[key] = item
    profile = value.get("profile_level_id")
    if profile is not None and (
        type(profile) is not str or not re.fullmatch("[0-9a-f]{6}", profile)
    ):
        raise ValueError
    result["profile_level_id"] = profile
    explicit = value.get("packetization_mode_explicit")
    if type(explicit) is not bool:
        raise ValueError
    result["packetization_mode_explicit"] = explicit
    result["feedback"] = _safe_flags(value.get("feedback"), (*_FEEDBACK.values(), "other"))
    opus = value.get("opus")
    if type(opus) is not dict:
        raise ValueError
    result["opus"] = {}
    for key, maximum in (
        ("stereo", 1),
        ("sprop_stereo", 1),
        ("useinbandfec", 1),
        ("usedtx", 1),
        ("maxplaybackrate", 48000),
        ("maxaveragebitrate", 510000),
    ):
        item = opus.get(key)
        if item is not None and _number(item, maximum) is None:
            raise ValueError
        result["opus"][key] = item
    return result


def _safe_flags(value, keys):
    if type(value) is not dict or any(type(value.get(key)) is not bool for key in keys):
        raise ValueError
    return {key: value[key] for key in keys}


def sanitize_codec_diagnostics(value):
    """Parent status whitelist: no SDP, arbitrary fmtp, IDs, payload types or URI."""
    try:
        if type(value) is not dict or value.get("answer_state") not in {
            "not_seen",
            "observed",
            "unavailable",
        }:
            return None
        result = {"answer_state": value["answer_state"], "answer": {}, "encoded_frames": {}}
        answer, frames = value.get("answer"), value.get("encoded_frames")
        if type(answer) is not dict or type(frames) is not dict:
            return None
        for kind in _KINDS:
            row = answer.get(kind)
            result["answer"][kind] = None
            if row is not None:
                codecs = row.get("codecs")
                if (
                    type(row) is not dict
                    or row.get("direction") not in _DIRECTIONS
                    or type(codecs) is not list
                    or len(codecs) > MAX_CODECS
                ):
                    return None
                result["answer"][kind] = {
                    "direction": row["direction"],
                    "codecs": [_safe_codec(item) for item in codecs],
                    "extensions": _safe_flags(
                        row.get("extensions"), (*_EXTENSIONS.values(), "other")
                    ),
                }
            frame = frames.get(kind)
            if type(frame) is not dict or _number(frame.get("observations"), MAX_COUNTER) is None:
                return None
            codec = frame.get("receiver_codec")
            result["encoded_frames"][kind] = {
                "observations": frame["observations"],
                "receiver_codec": None if codec is None else _safe_codec(codec),
            }
        return result
    except BaseException:
        return None


class NegotiatedCodecs:
    def __init__(self, *, current):
        self._current, self._closed = current, False
        self._answer_state = "not_seen"
        self._answer = {kind: None for kind in _KINDS}
        self._frames = {kind: {"observations": 0, "receiver_codec": None} for kind in _KINDS}
        self._objects = {}

    def _admitted(self):
        try:
            return not self._closed and self._current() is True
        except BaseException:
            return False

    def answer(self, sdp):
        if not self._admitted():
            return
        try:
            if (
                type(sdp) is not str
                or len(sdp) > MAX_SDP
                or len(sdp.encode("utf-8", "surrogatepass")) > MAX_SDP
            ):
                raise ValueError
            media_count = codec_count = 0
            for line in sdp.splitlines():
                if line.startswith("m="):
                    media_count += 1
                    codec_count = 0
                    if media_count > 4:
                        raise ValueError
                elif line.startswith("a=rtpmap:"):
                    codec_count += 1
                    if codec_count > MAX_CODECS:
                        raise ValueError
            parsed = SessionDescription.parse(sdp)
            if len(parsed.media) > 4:
                raise ValueError
            result = {kind: None for kind in _KINDS}
            for media in parsed.media:
                if media.kind not in _KINDS:
                    continue
                if result[media.kind] is not None or len(media.rtp.codecs) > MAX_CODECS:
                    raise ValueError
                codecs = [describe_codec(codec) for codec in media.rtp.codecs]
                if any(codec is None for codec in codecs):
                    raise ValueError
                result[media.kind] = {
                    "direction": media.direction if media.direction in _DIRECTIONS else "unknown",
                    "codecs": codecs,
                    "extensions": _extensions(media.rtp.headerExtensions),
                }
            if self._admitted():
                self._answer, self._answer_state = result, "observed"
        except BaseException:
            self._answer_state = "unavailable"
            self._answer = {kind: None for kind in _KINDS}

    def frame(self, kind, codec):
        if not self._admitted() or kind not in _KINDS:
            return
        try:
            row = self._frames[kind]
            row["observations"] = min(MAX_COUNTER, row["observations"] + 1)
            previous = self._objects.get(kind)
            if previous is not None and previous() is codec:
                return
            safe = describe_codec(codec)
            if self._admitted():
                row["receiver_codec"] = safe
                if safe is not None:
                    self._objects[kind] = weakref.ref(codec)
                else:
                    self._objects.pop(kind, None)
        except BaseException:
            pass

    def snapshot(self):
        if not self._admitted():
            self.fence()
            return None
        return sanitize_codec_diagnostics(
            {
                "answer_state": self._answer_state,
                "answer": self._answer,
                "encoded_frames": self._frames,
            }
        )

    def fence(self):
        self._closed = True
        self._objects.clear()
        self._answer = {kind: None for kind in _KINDS}
        self._frames = {kind: {"observations": 0, "receiver_codec": None} for kind in _KINDS}
