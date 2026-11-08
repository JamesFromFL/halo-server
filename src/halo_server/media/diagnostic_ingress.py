"""Optional finite source traces; observation never changes receiver policy."""

from __future__ import annotations

import contextlib
import math
import time

from aiortc.jitterbuffer import JitterBuffer


class ScopedDiagnosticTrace:
    """Fence retained callbacks without closing the shared capture owner."""

    def __init__(self, trace, current, **scope):
        self._trace = trace
        self._current = current
        self._scope = scope
        self._fenced = False

    @property
    def closed(self):
        try:
            return (
                self._fenced or not self._current() or bool(getattr(self._trace, "closed", False))
            )
        except BaseException:
            return True

    def event(self, stage, track, **fields):
        with contextlib.suppress(BaseException):
            if not self._fenced and self._current():
                return self._trace.event(stage, track, **(fields | self._scope))
        return False

    def media(self, stage, track, payload, **fields):
        with contextlib.suppress(BaseException):
            if not self._fenced and self._current():
                return self._trace.media(stage, track, payload, **(fields | self._scope))
        return False

    def trigger(self, reason):
        with contextlib.suppress(BaseException):
            if not self._fenced and self._current():
                return self._trace.trigger(reason)
        return False

    def fence(self):
        self._fenced = True


def trace_frame(trace, kind, frame):
    """Read optional frame metadata inside the fail-open observation boundary."""
    if trace is not None:
        with contextlib.suppress(BaseException):
            fields = {
                "timestamp": frame.timestamp,
                "payload_bytes": len(frame.data),
                "codec": "opus" if kind == "audio" else "h264",
            }
            trace.event("source_frame", kind, **fields)
            trace.media(
                "source_frame",
                kind,
                frame.data,
                **fields,
            )


def trace_rtp(trace, kind, packet, receiver=None):
    if trace is None:
        return
    with contextlib.suppress(BaseException):
        fields = {
            "seq": packet.sequence_number,
            "rtp_timestamp": packet.timestamp,
            "payload_type": packet.payload_type,
            "marker": bool(packet.marker),
            "payload_bytes": len(packet.payload),
        }
        if receiver is not None:
            codec = receiver._RTCRtpReceiver__codecs.get(packet.payload_type)
            if codec is not None and codec.mimeType.lower() in {"audio/opus", "video/h264"}:
                fields["codec"] = codec.mimeType.lower().split("/")[1]
        trace.event("source_rtp", kind, **fields)
        trace.media(
            "source_rtp",
            kind,
            packet.payload,
            **fields,
        )


def _audio_state(buffer, packet):
    """Inspect at most the pinned sixteen audio slots, without touching media."""
    capacity = buffer._capacity
    packets = buffer._packets
    if capacity != 16 or type(packets) is not list or len(packets) != capacity:
        return None
    origin = buffer._origin
    result = {
        "origin": origin,
        "capacity": capacity,
        "prefetch": buffer._prefetch,
        "occupancy_mask": 0,
        "contiguous_packets": 0,
        "contiguous_frames": 0,
        "first_gap_offset": 0,
        "incoming_offset": None if origin is None else (packet.sequence_number - origin) % 65536,
    }
    if origin is None:
        return {key: value for key, value in result.items() if value is not None}
    previous = None
    contiguous = True
    for offset in range(capacity):
        item = packets[(origin + offset) % capacity]
        if item is not None:
            result["occupancy_mask"] |= 1 << offset
            if contiguous:
                result["contiguous_packets"] += 1
                if previous is not None and item.timestamp != previous:
                    result["contiguous_frames"] += 1
                previous = item.timestamp
        elif contiguous:
            contiguous = False
            result["first_gap_offset"] = offset
    if contiguous:
        result["first_gap_offset"] = capacity
    return result


def install_audio_jitter_trace(receiver, trace, *, clock=time.monotonic):
    """Wrap the exact add call, retaining all original results and exceptions."""
    if trace is None:
        return False
    from halo_server.media.opus_release import CompleteOpusBuffer

    buffer = getattr(receiver, "_RTCRtpReceiver__jitter_buffer", None)
    if (
        type(buffer) not in {JitterBuffer, CompleteOpusBuffer}
        or buffer.capacity != 16
        or "add" in buffer.__dict__
    ):
        return False
    original = buffer.add
    first = last_frame = None
    waiting_packets = 0
    triggered = False

    def observed(packet):
        nonlocal first, last_frame, waiting_packets, triggered
        if isinstance(trace, ScopedDiagnosticTrace) and trace.closed:
            return original(packet)
        now = None
        with contextlib.suppress(BaseException):
            value = clock()
            if type(value) in (int, float) and math.isfinite(value):
                now = value
            before = _audio_state(buffer, packet)
            if before is not None:
                trace.event("audio_jitter_before", "audio", seq=packet.sequence_number, **before)
        result = original(packet)
        with contextlib.suppress(BaseException):
            pli, frame = result
            if first is None and now is not None:
                first = now
            waiting_packets += 1
            anchor = last_frame if last_frame is not None else first
            hold_ms = (
                None if now is None or anchor is None else max(0, round((now - anchor) * 1000))
            )
            after = _audio_state(buffer, packet)
            if after is not None:
                trace.event(
                    "audio_jitter_after",
                    "audio",
                    seq=packet.sequence_number,
                    frame_returned=frame is not None,
                    pli=bool(pli),
                    waiting_packets=waiting_packets,
                    **({"frame_timestamp": frame.timestamp} if frame is not None else {}),
                    **({"hold_ms": hold_ms} if hold_ms is not None else {}),
                    **after,
                )
            if frame is not None:
                last_frame = now
                waiting_packets = 0
            elif not triggered and waiting_packets >= 2 and hold_ms is not None and hold_ms >= 350:
                triggered = bool(trace.trigger("audio_assembly_hold"))
        return result

    buffer.add = observed
    return True
