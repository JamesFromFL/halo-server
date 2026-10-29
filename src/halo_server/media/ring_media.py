"""Headless Ring media core — no GTK.

The aiortc compatibility patches, the silent ``MicrophoneTrack``, the frame/audio
helpers, and the WebRTC receive loop used by Halo Server's media session layer.
Importing this module applies the aiortc patches before any ``RTCPeerConnection``
is created.

This is the single source of truth for the Ring-compatibility patches; getting
them wrong makes some cameras decode as garbage (see ``patch_aiortc_h264``).
"""

from __future__ import annotations

import asyncio
import contextlib
import fractions
import functools
import logging
import math
import queue
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib import metadata as importlib_metadata

from aiortc.mediastreams import AudioStreamTrack, MediaStreamError
from av import AudioFrame

from halo_server.media.buffer import h264_nal_types
from halo_server.media.ring_speaker import (
    RingSpeakerControlError,
    activate_ring_camera_speaker,
)

_log = logging.getLogger(__name__)

_ENCODED_FRAME_AIORTC_VERSIONS = frozenset({"1.15.0"})
_RING_DOORBELL_WEBRTC_CLOSE_VERSIONS = frozenset({"0.9.14"})
_DECODER_QUEUE_ATTRIBUTE = "_RTCRtpReceiver__decoder_queue"
_DECODER_THREAD_ATTRIBUTE = "_RTCRtpReceiver__decoder_thread"
_ACTIVE_SSRC_ATTRIBUTE = "_RTCRtpReceiver__active_ssrc"
_RTX_SSRC_ATTRIBUTE = "_RTCRtpReceiver__rtx_ssrc"
_TIMESTAMP_MAPPER_ATTRIBUTE = "_RTCRtpReceiver__timestamp_mapper"
_REMOTE_STREAMS_ATTRIBUTE = "_RTCRtpReceiver__remote_streams"


def _diagnostic_notify(callback, *args):
    """Optional observations cannot change receiver delivery or cleanup."""
    if callback is not None:
        try:
            return callback(*args)
        except BaseException:
            pass
    return None


def _encoded_source_callback(receiver, kind, callback, on_source_clock, on_ingress):
    if on_source_clock is None and on_ingress is None:
        return callback
    observe = (
        install_source_clock_observer(receiver, kind, on_source_clock)
        if on_source_clock is not None
        else None
    )

    def deliver(payload, timestamp):
        cleanup = None
        try:
            try:
                entered = time.monotonic()
            except BaseException:
                entered = None
            if type(entered) in {int, float} and math.isfinite(entered) and entered >= 0:
                cleanup = _diagnostic_notify(on_ingress, kind, timestamp, entered)
            if observe is not None:
                observe(timestamp)
            return callback(payload, timestamp)
        finally:
            if callable(cleanup):
                _diagnostic_notify(cleanup)

    return deliver


def _inbound_statistics(report, kind):
    """Public receiver stats only; source/transport IDs never leave this seam."""
    try:
        if len(report) > 32:
            return None
        rows = [row for row in report.values() if row.type == "inbound-rtp" and row.kind == kind]
        if len(rows) != 1:
            return None
        row = rows[0]
        result = {}
        for output, field, signed in (
            ("packets_received", "packetsReceived", False),
            ("packets_lost", "packetsLost", True),
            ("jitter_ticks", "jitter", False),
        ):
            value = getattr(row, field, None)
            low = -(2**53 - 1) if signed else 0
            result[output] = value if type(value) is int and low <= value <= 2**53 - 1 else None
        result["clock_rate"] = 48000 if kind == "audio" else 90000
        return result
    except Exception:
        return None


async def observe_receiver_statistics(
    audio_receiver, video_receiver, stop, terminal, callback, *, interval=1.0, timeout=0.25
):
    """One retained receiver task: finite sampling, no raw media or log I/O."""
    loop = asyncio.get_running_loop()
    while not stop.is_set() and not terminal.is_set():
        deadline = loop.time() + interval
        await asyncio.sleep(interval)
        woke = loop.time()
        if stop.is_set() or terminal.is_set():
            return
        snapshot = None
        try:
            async with asyncio.timeout(timeout):
                # Stay within this retained owner rather than creating hidden
                # child tasks whose cancellation could outlive its retirement.
                audio = await audio_receiver.getStats()
                video = await video_receiver.getStats()
            snapshot = {
                "audio": _inbound_statistics(audio, "audio"),
                "video": _inbound_statistics(video, "video"),
            }
        except asyncio.CancelledError:
            raise
        except BaseException:
            pass
        if not stop.is_set() and not terminal.is_set():
            _diagnostic_notify(callback, snapshot, max(0, woke - deadline), loop.time() - woke)


_ENCODED_MIME_TYPES = {
    "video": "video/h264",
    "audio": "audio/opus",
}
STARTUP_KEYFRAME_REQUESTS = 3
STARTUP_KEYFRAME_POLL_SECONDS = 0.1
STARTUP_KEYFRAME_INTERVAL_SECONDS = 0.75
STARTUP_KEYFRAME_DEADLINE_SECONDS = 5.0
INTERCOM_SPEAKER_POLL_SECONDS = 0.02
INTERCOM_PEER_READY_TIMEOUT = 5.0
INTERCOM_SPEAKER_ACTIVATION_TIMEOUT = 12.0
INTERCOM_SPEAKER_RETRY_SECONDS = 0.25
INTERCOM_STATISTICS_TIMEOUT = 1.0
WEBRTC_OFFER_TIMEOUT = 10.0
WEBRTC_SIGNALING_TIMEOUT = 20.0
WEBRTC_ANSWER_TIMEOUT = 15.0
WEBRTC_REMOTE_DESCRIPTION_TIMEOUT = 5.0
RECEIVER_TASK_CLEANUP_TIMEOUT = 0.5
RECEIVER_CLOSE_TIMEOUT = 2.0
FIRST_VIDEO_TIMEOUT = 20.0
VIDEO_STALL_TIMEOUT = 10.0
_DIAGNOSTIC_VALUE_LIMIT = 128


class EncodedFrameAdapterError(RuntimeError):
    """Raised when aiortc's validated encoded-frame boundary is unavailable."""


class MediaCompatibilityError(RuntimeError):
    """Raised when the installed media dependencies no longer match Halo's patches."""


class RingMediaNegotiationError(RuntimeError):
    """Raised when Ring's WebRTC answer cannot be applied to the local peer."""


class RingMediaProgressError(RuntimeError):
    """A negotiated attempt did not deliver timely accepted video."""


class RingMediaUnsupportedCodecError(MediaCompatibilityError):
    """Negotiated codecs cannot use the validated encoded-media boundary."""


@dataclass(frozen=True)
class MicrophonePcmSnapshot:
    """Content-free counters for one intercom PCM queue."""

    queued_frames: int
    accepted_frames: int
    consumed_frames: int
    dropped_frames: int
    intercom_active: bool = False
    intercom_generation: int = 0
    speaker_ready: bool = False
    speaker_failure: str | None = None


@dataclass(frozen=True)
class AudioSenderSnapshot:
    """Content-free aiortc audio sender counters."""

    packets_sent: int = 0
    bytes_sent: int = 0
    remote_packets_received: int | None = None
    remote_packets_lost: int | None = None
    remote_fraction_lost: int | None = None
    remote_jitter: int | None = None
    round_trip_time: float | None = None


@dataclass(frozen=True)
class SourceClockObservation:
    """An actual source SR mapped into the encoded callback timestamp domain."""

    kind: str
    ssrc: int
    rtp_timestamp: int
    sender_ntp: int
    received_at: float
    capture_epoch: int = 0


def install_source_clock_observer(receiver, kind, callback, *, clock=time.monotonic):
    """Wrap one pinned receiver, preserving its original RTCP processing.

    Encoded frames use aiortc's TimestampMapper domain, which has a separate
    origin for each receiver. Raw SR values must never be relabelled as that
    domain. The returned hook maps only an actual, recent, known-source report.
    """
    import aiortc
    from aiortc.rtcrtpreceiver import TimestampMapper
    from aiortc.rtp import RtcpSrPacket

    original = getattr(receiver, "_handle_rtcp_packet", None)
    code = getattr(getattr(type(receiver), "_handle_rtcp_packet", None), "__code__", None)
    mapper = getattr(receiver, _TIMESTAMP_MAPPER_ATTRIBUTE, None)
    streams = getattr(receiver, _REMOTE_STREAMS_ATTRIBUTE, None)
    active = getattr(receiver, _ACTIVE_SSRC_ATTRIBUTE, None)
    rtx = getattr(receiver, _RTX_SSRC_ATTRIBUTE, None)
    if (
        str(getattr(aiortc, "__version__", "")) not in _ENCODED_FRAME_AIORTC_VERSIONS
        or kind not in _ENCODED_MIME_TYPES
        or not callable(original)
        or not {"RtcpSrPacket", "sender_info", "ntp_timestamp"}.issubset(
            set(getattr(code, "co_names", ()))
        )
        or type(mapper) is not TimestampMapper
        or not hasattr(mapper, "_origin")
        or not hasattr(mapper, "_last")
        or not isinstance(streams, dict)
        or not isinstance(active, dict)
        or not isinstance(rtx, dict)
        or "_handle_rtcp_packet" in receiver.__dict__
    ):
        raise EncodedFrameAdapterError("Source clock observer contract changed")
    latest = None
    published = False

    async def handle(packet):
        nonlocal latest
        await original(packet)
        if not isinstance(packet, RtcpSrPacket) or packet.ssrc not in streams:
            return
        info = packet.sender_info
        if (
            type(packet.ssrc) is int
            and 0 < packet.ssrc < 2**32
            and type(info.ntp_timestamp) is int
            and 0 < info.ntp_timestamp < 2**64
            and type(info.rtp_timestamp) is int
            and 0 <= info.rtp_timestamp < 2**32
        ):
            latest = (packet.ssrc, info.rtp_timestamp, info.ntp_timestamp, clock())

    receiver._handle_rtcp_packet = handle

    def invalidate():
        nonlocal published
        if published:
            published = False
            callback(SourceClockObservation(kind, 0, 0, 0, clock()))

    def observe(timestamp):
        nonlocal published
        if latest is None or type(timestamp) is not int:
            return
        ssrc, raw, ntp, received = latest
        origin = mapper._origin
        if len(active) > 16 or len(streams) > 16:
            invalidate()
            return
        candidates = [
            (observed, rtx.get(source, source))
            for source, observed in active.items()
            if source in streams and rtx.get(source, source) in streams
        ]
        if (
            type(origin) is not int
            or ssrc not in streams
            or not candidates
            or len({source for _observed, source in candidates}) != 1
            or max(candidates)[1] != ssrc
            or not 0 <= clock() - received <= 30
        ):
            invalidate()
            return
        mapped = (raw - origin) % 2**32
        mapped += round((timestamp - mapped) / 2**32) * 2**32
        callback(SourceClockObservation(kind, ssrc, mapped, ntp, received))
        published = True

    return observe


def patch_ring_doorbell_webrtc_close() -> bool:
    """Prevent ring-doorbell's reader task from awaiting itself on a Ring close."""
    try:
        version = importlib_metadata.version("ring-doorbell")
    except importlib_metadata.PackageNotFoundError:
        _log.debug("Could not apply ring-doorbell WebRTC close patch: package not installed")
        return False

    if version not in _RING_DOORBELL_WEBRTC_CLOSE_VERSIONS:
        _log.debug(
            "Skipping ring-doorbell WebRTC close patch for unvalidated version %s",
            version,
        )
        return False

    from ring_doorbell.webrtcstream import RingWebRtcStream

    if getattr(RingWebRtcStream, "_halo_reader_self_await_patch_applied", False):
        return True

    original_close = RingWebRtcStream._close
    code = getattr(original_close, "__code__", None)
    names = set(getattr(code, "co_names", ()))
    varnames = tuple(getattr(code, "co_varnames", ()))
    required_names = {"_on_close_callback", "done", "read_task", "websocket"}
    if (
        code is None
        or not required_names.issubset(names)
        or varnames[:2] != ("self", "closed_by_self")
    ):
        _log.warning(
            "ring-doorbell %s WebRTC close contract changed; "
            "natural stream renewal cleanup is unpatched",
            version,
        )
        return False

    @functools.wraps(original_close)
    async def _close_without_self_await(self, *, closed_by_self: bool) -> None:
        if getattr(self, "read_task", None) is asyncio.current_task():
            self.read_task = None
        await original_close(self, closed_by_self=closed_by_self)

    RingWebRtcStream._close = _close_without_self_await
    RingWebRtcStream._halo_reader_self_await_patch_applied = True
    _log.debug("Applied ring-doorbell WebRTC reader self-await patch")
    return True


class _EncodedFrameQueue(queue.Queue):
    """Observe validated encoded frames before optionally forwarding them to aiortc."""

    def __init__(
        self,
        kind: str,
        callback: Callable[[bytes, int], None],
        *,
        forward_to_decoder: bool,
        on_incompatible: Callable[[str, str], None] | None,
    ) -> None:
        super().__init__()
        self._kind = kind
        self._callback = callback
        self._forward_to_decoder = forward_to_decoder
        self._on_incompatible = on_incompatible
        self._reported_mime_types: set[str] = set()

    def put(self, item, block: bool = True, timeout: float | None = None) -> None:
        if item is None:
            super().put(item, block=block, timeout=timeout)
            return

        if not isinstance(item, tuple) or len(item) != 2:
            if self._forward_to_decoder:
                super().put(item, block=block, timeout=timeout)
            return

        codec, frame = item
        mime_type = str(getattr(codec, "mimeType", "")).lower()
        if mime_type == _ENCODED_MIME_TYPES[self._kind]:
            self._callback(bytes(frame.data), int(frame.timestamp))
            if not self._forward_to_decoder:
                return
        elif mime_type not in self._reported_mime_types:
            self._reported_mime_types.add(mime_type)
            if self._on_incompatible is not None:
                self._on_incompatible(self._kind, mime_type or "unknown")

        if self._forward_to_decoder:
            super().put(item, block=block, timeout=timeout)


def install_encoded_frame_adapter(
    receiver,
    kind: str,
    callback: Callable[[bytes, int], None],
    *,
    forward_to_decoder: bool,
    on_incompatible: Callable[[str, str], None] | None = None,
) -> queue.Queue:
    """Install Halo's validated aiortc 1.15 encoded-frame queue on one receiver."""
    import aiortc

    version = str(getattr(aiortc, "__version__", "unknown"))
    if version not in _ENCODED_FRAME_AIORTC_VERSIONS:
        raise EncodedFrameAdapterError(
            f"aiortc {version} is not validated for encoded H.264/Opus access"
        )
    if kind not in _ENCODED_MIME_TYPES:
        raise EncodedFrameAdapterError(f"unsupported encoded receiver kind: {kind}")
    if not hasattr(receiver, _DECODER_QUEUE_ATTRIBUTE) or not hasattr(
        receiver, _DECODER_THREAD_ATTRIBUTE
    ):
        raise EncodedFrameAdapterError("aiortc receiver no longer exposes the validated queue")
    packet_handler = getattr(type(receiver), "_handle_rtp_packet", None)
    code = getattr(packet_handler, "__code__", None)
    names = set(getattr(code, "co_names", ()))
    if _DECODER_QUEUE_ATTRIBUTE not in names or "put" not in names:
        raise EncodedFrameAdapterError("aiortc encoded-frame delivery contract changed")
    if getattr(receiver, _DECODER_THREAD_ATTRIBUTE) is not None:
        raise EncodedFrameAdapterError("encoded adapter must be installed before decoding starts")

    current_queue = getattr(receiver, _DECODER_QUEUE_ATTRIBUTE)
    if not isinstance(current_queue, queue.Queue) or isinstance(current_queue, _EncodedFrameQueue):
        raise EncodedFrameAdapterError("aiortc receiver decoder queue contract changed")

    adapter = _EncodedFrameQueue(
        kind,
        callback,
        forward_to_decoder=forward_to_decoder,
        on_incompatible=on_incompatible,
    )
    setattr(receiver, _DECODER_QUEUE_ATTRIBUTE, adapter)
    return adapter


async def request_startup_keyframes(
    receiver,
    stop: threading.Event,
    terminal: asyncio.Event,
) -> None:
    """Request a bounded set of startup IDRs for encoded pass-through."""
    import aiortc

    version = str(getattr(aiortc, "__version__", "unknown"))
    sender = getattr(receiver, "_send_rtcp_pli", None)
    if (
        version not in _ENCODED_FRAME_AIORTC_VERSIONS
        or not callable(sender)
        or not hasattr(receiver, _ACTIVE_SSRC_ATTRIBUTE)
        or not hasattr(receiver, _RTX_SSRC_ATTRIBUTE)
    ):
        _log.warning(
            "aiortc %s does not expose Halo's validated startup keyframe boundary",
            version,
        )
        return

    loop = asyncio.get_running_loop()
    deadline = loop.time() + STARTUP_KEYFRAME_DEADLINE_SECONDS
    sent = 0
    while (
        sent < STARTUP_KEYFRAME_REQUESTS
        and loop.time() < deadline
        and not stop.is_set()
        and not terminal.is_set()
    ):
        active_ssrcs = set(getattr(receiver, _ACTIVE_SSRC_ATTRIBUTE))
        rtx_ssrcs = set(getattr(receiver, _RTX_SSRC_ATTRIBUTE))
        media_ssrcs = sorted(active_ssrcs - rtx_ssrcs)
        if not media_ssrcs:
            await asyncio.sleep(STARTUP_KEYFRAME_POLL_SECONDS)
            continue
        for media_ssrc in media_ssrcs:
            await sender(media_ssrc)
        sent += 1
        if sent < STARTUP_KEYFRAME_REQUESTS:
            await asyncio.sleep(STARTUP_KEYFRAME_INTERVAL_SECONDS)


def patch_aiortc_h264() -> bool:
    """Monkey-patch aiortc for H264 and RTP receiver compatibility with Ring cameras.

    Three patches are applied at module import time so they take effect before
    any RTCPeerConnection or H264Decoder is created.

    Patch 1 — JitterBuffer capacity (128 → 512)
        The stickup_cam_mini_v3 sends each IDR keyframe as 159 RTP packets:
        1 SPS + 1 PPS + 157 FU-A fragments at 1172 bytes each (~184 KB total).
        aiortc's video JitterBuffer has capacity=128.  When packet #128 arrives
        (still part of the IDR), delta >= capacity triggers smart_remove(1),
        which removes packets until the timestamp changes — but SPS, PPS, and
        all 157 IDR fragments share the same RTP timestamp, so smart_remove
        wipes the ENTIRE keyframe.  The decoder never receives SPS+PPS+IDR and
        every subsequent P-frame fails with AVERROR_INVALIDDATA.  Capacity 512
        (power-of-2 requirement) comfortably fits the largest Ring IDR.

    Patch 2 — SDP offer H264 codec variants
        Two additions to the CODECS["video"] list:
        a. packetization-mode=0 Baseline variants: aiortc only offers mode=1;
           cameras that answer with mode=0 cause is_codec_compatible() to return
           False, raising OperationError in setRemoteDescription().
        b. High Profile Level 5.0 (profile-level-id=640032): the stickup_cam_mini_v3
           streams High Profile H264 (profile_idc=100, confirmed from SPS bytes
           0x67 0x64 0x00 0x32 in the RTP stream) but Ring's SDP answers with
           Baseline (42001f) because our offer only lists Baseline.  Adding
           640032 lets Ring negotiate the correct profile so the SPS in the
           bitstream matches what was negotiated, eliminating the profile
           mismatch that causes decode errors after the buffer fix.

    Patch 3 — H264Decoder AV_CODEC_FLAG_OUTPUT_CORRUPT (0x8)
        Instructs FFmpeg to output error-concealed frames rather than silently
        dropping them, helping the stream stay visible through transient errors.

    Patch 4 — H264Decoder warning throttling
        aiortc logs every FFmpeg decode rejection at WARNING. With Ring streams,
        one lost/corrupt keyframe can make FFmpeg reject many dependent frames
        until the next clean keyframe. The stream recovers, so repeated warnings
        are not actionable in the terminal. We keep the failure count in debug
        logging instead of flooding stderr.
    """
    try:
        import av
        from aiortc.codecs import CODECS
        from aiortc.codecs import h264 as aiortc_h264
        from aiortc.codecs.h264 import H264Decoder
        from aiortc.jitterbuffer import JitterBuffer
        from aiortc.rtcrtpparameters import RTCRtcpFeedback, RTCRtpCodecParameters
        from aiortc.rtcrtpreceiver import RTCRtpReceiver

        # --- Patch 1: increase video JitterBuffer capacity 128 → 512 ---
        if not getattr(RTCRtpReceiver, "_halo_jitter_buffer_patch_applied", False):
            _orig_receiver_init = RTCRtpReceiver.__init__

            def _patched_receiver_init(self, kind: str, transport) -> None:
                _orig_receiver_init(self, kind, transport)
                if kind == "video":
                    # Replace the 128-slot buffer created by __init__ with a 512-slot
                    # one.  Name mangling: __jitter_buffer → _RTCRtpReceiver__jitter_buffer.
                    self._RTCRtpReceiver__jitter_buffer = JitterBuffer(
                        capacity=512,
                        is_video=True,
                    )

            RTCRtpReceiver.__init__ = _patched_receiver_init
            RTCRtpReceiver._halo_jitter_buffer_patch_applied = True
            _log.debug("Patched RTCRtpReceiver video JitterBuffer capacity: 128 → 512")

        # --- Patch 2: add H264 codec variants to the SDP offer ---
        existing_h264 = {
            (c.parameters.get("packetization-mode"), c.parameters.get("profile-level-id"))
            for c in CODECS["video"]
            if c.mimeType.lower() == "video/h264"
        }
        base_pt = max((c.payloadType for c in CODECS["video"]), default=102) + 1
        additions = [
            # packetization-mode=0 Baseline (cameras that negotiate mode=0)
            ("0", "42001f"),
            ("0", "42e01f"),
            # High Profile Level 5.0, both modes (stickup_cam_mini_v3 bitstream)
            ("1", "640032"),
            ("0", "640032"),
        ]
        for mode, profile in additions:
            if (mode, profile) not in existing_h264:
                CODECS["video"].append(
                    RTCRtpCodecParameters(
                        mimeType="video/H264",
                        clockRate=90000,
                        payloadType=base_pt,
                        rtcpFeedback=[
                            RTCRtcpFeedback(type="nack"),
                            RTCRtcpFeedback(type="nack", parameter="pli"),
                            RTCRtcpFeedback(type="goog-remb"),
                        ],
                        parameters={
                            "level-asymmetry-allowed": "1",
                            "packetization-mode": mode,
                            "profile-level-id": profile,
                        },
                    )
                )
                base_pt += 1
        _log.debug("Added H264 SDP variants (mode=0 Baseline, High Profile 640032)")

        # --- Patch 3: set AV_CODEC_FLAG_OUTPUT_CORRUPT on the H264Decoder ---
        if not getattr(H264Decoder, "_halo_output_corrupt_patch_applied", False):
            _orig_h264_init = H264Decoder.__init__

            def _permissive_init(self) -> None:
                _orig_h264_init(self)
                # AV_CODEC_FLAG_OUTPUT_CORRUPT (1 << 3): output frames even when
                # avcodec_send_packet() reports AVERROR_INVALIDDATA, using FFmpeg's
                # error concealment to keep the stream visible through transient errors.
                self.codec.flags |= 0x8  # AV_CODEC_FLAG_OUTPUT_CORRUPT

            H264Decoder.__init__ = _permissive_init
            H264Decoder._halo_output_corrupt_patch_applied = True
            _log.debug("Applied H264 decoder patch (AV_CODEC_FLAG_OUTPUT_CORRUPT)")

        if not getattr(H264Decoder, "_halo_quiet_decode_patch_applied", False):

            def _quiet_decode(self, encoded_frame):
                try:
                    packet = av.Packet(encoded_frame.data)
                    packet.pts = encoded_frame.timestamp
                    packet.time_base = aiortc_h264.VIDEO_TIME_BASE
                    return self.codec.decode(packet)
                except av.FFmpegError as exc:
                    now = time.monotonic()
                    count = getattr(self, "_halo_decode_error_count", 0) + 1
                    last_log = getattr(self, "_halo_decode_last_log", 0.0)
                    self._halo_decode_error_count = count
                    if now - last_log >= 30:
                        _log.debug(
                            "H264Decoder skipped corrupt frames; latest error after %d skips: %s",
                            count,
                            exc,
                        )
                        self._halo_decode_last_log = now
                        self._halo_decode_error_count = 0
                    return []

            H264Decoder.decode = _quiet_decode
            H264Decoder._halo_quiet_decode_patch_applied = True
            _log.debug("Applied H264 decoder warning throttle")

        _log.debug("aiortc H264/RTP patches applied")
        return True
    except Exception as exc:
        _log.warning("Could not apply aiortc compatibility patches: %s", exc)
        return False


def validate_media_compatibility() -> None:
    """Fail startup when Halo's exact private media contracts are unavailable."""
    import aiortc
    from aiortc.codecs import CODECS
    from aiortc.codecs.h264 import H264Decoder
    from aiortc.rtcrtpreceiver import RTCRtpReceiver
    from ring_doorbell.webrtcstream import RingWebRtcStream

    errors: list[str] = []
    aiortc_version = str(getattr(aiortc, "__version__", "unknown"))
    if aiortc_version not in _ENCODED_FRAME_AIORTC_VERSIONS:
        errors.append(f"aiortc {aiortc_version} is not validated")
    if not getattr(RTCRtpReceiver, "_halo_jitter_buffer_patch_applied", False):
        errors.append("aiortc video jitter-buffer patch is missing")
    if not getattr(H264Decoder, "_halo_output_corrupt_patch_applied", False):
        errors.append("aiortc H.264 decoder recovery patch is missing")
    if not getattr(H264Decoder, "_halo_quiet_decode_patch_applied", False):
        errors.append("aiortc H.264 logging patch is missing")

    required_h264 = {
        ("0", "42001f"),
        ("0", "42e01f"),
        ("1", "640032"),
        ("0", "640032"),
    }
    available_h264 = {
        (
            str(codec.parameters.get("packetization-mode")),
            str(codec.parameters.get("profile-level-id")),
        )
        for codec in CODECS["video"]
        if codec.mimeType.lower() == "video/h264"
    }
    if not required_h264.issubset(available_h264):
        errors.append("required Ring H.264 offer profiles are missing")

    try:
        ring_version = importlib_metadata.version("ring-doorbell")
    except importlib_metadata.PackageNotFoundError:
        ring_version = "missing"
    if ring_version not in _RING_DOORBELL_WEBRTC_CLOSE_VERSIONS:
        errors.append(f"ring-doorbell {ring_version} is not validated")
    if not getattr(RingWebRtcStream, "_halo_reader_self_await_patch_applied", False):
        errors.append("ring-doorbell WebRTC close patch is missing")

    if errors:
        raise MediaCompatibilityError("; ".join(errors))


patch_aiortc_h264()
patch_ring_doorbell_webrtc_close()


MIC_SAMPLE_RATE = 48000
MIC_SAMPLES = 960  # 20 ms at 48 kHz — matches aiortc's AUDIO_PTIME
MIC_FRAME_BYTES = MIC_SAMPLES * 2
MIC_QUEUE_FRAMES = 10
MIC_FRAME_MAX_AGE = 0.25


@dataclass(frozen=True)
class _QueuedMicrophoneFrame:
    payload: bytes
    queued_at: float
    generation: int | None
    admission: Callable[[], bool] | None


def retain_aiortc_connect_tasks(peer_connection, retain) -> None:
    """Retain aiortc's untracked private connection task for one peer."""
    for task in asyncio.all_tasks():
        if task.done():
            continue
        coro = task.get_coro()
        frame = getattr(coro, "cr_frame", None)
        code = getattr(coro, "cr_code", None)
        if (
            frame is not None
            and code is not None
            and code.co_name == "__connect"
            and frame.f_locals.get("self") is peer_connection
        ):
            retain(task, "aiortc connection")


def audio_frame_to_s16le_interleaved(frame) -> tuple[bytes, int, int, int]:
    """Return (raw bytes, channels, rate, samples) for a PyAV audio frame."""
    import numpy as np

    arr = frame.to_ndarray()
    channels = audio_frame_channels(frame, arr)
    samples = int(getattr(frame, "samples", 0) or 0)
    rate = int(getattr(frame, "sample_rate", 0) or MIC_SAMPLE_RATE)

    # PyAV planar audio is channels x samples.  GStreamer appsrc is simpler
    # and more reliable when we feed it interleaved S16LE.
    interleaved = arr.T.reshape(-1) if frame.format.is_planar else arr.reshape(-1)

    if interleaved.dtype.kind == "f":
        interleaved = np.clip(interleaved, -1.0, 1.0)
        interleaved = (interleaved * 32767).astype(np.int16)
    elif interleaved.dtype != np.int16:
        info = np.iinfo(interleaved.dtype) if interleaved.dtype.kind in {"i", "u"} else None
        if info is not None and interleaved.dtype.itemsize > 2:
            shift = (interleaved.dtype.itemsize - 2) * 8
            interleaved = (interleaved >> shift).astype(np.int16)
        else:
            interleaved = interleaved.astype(np.int16)

    if samples <= 0 and channels > 0:
        samples = max(1, interleaved.size // channels)
    return interleaved.tobytes(), channels, rate, samples


def audio_frame_channels(frame, arr) -> int:
    layout = getattr(frame, "layout", None)
    channels = getattr(layout, "channels", None)
    if channels is not None:
        try:
            return max(1, len(channels))
        except TypeError:
            pass
    if frame.format.is_planar and getattr(arr, "ndim", 0) >= 1:
        return max(1, int(arr.shape[0]))
    if getattr(arr, "ndim", 0) >= 2:
        return max(1, int(arr.shape[1]))
    return 1


class MicrophonePcmBuffer:
    """Thread-safe low-latency queue of fixed 20 ms microphone frames."""

    def __init__(
        self,
        *,
        max_frames: int = MIC_QUEUE_FRAMES,
        clock=time.monotonic,
        max_age: float = MIC_FRAME_MAX_AGE,
    ) -> None:
        if isinstance(max_frames, bool) or not isinstance(max_frames, int) or max_frames <= 0:
            raise ValueError("microphone queue size must be a positive integer")
        if isinstance(max_age, bool) or not math.isfinite(max_age) or max_age <= 0:
            raise ValueError("microphone frame age must be finite and positive")
        self._clock, self._max_age = clock, max_age
        self._frames: deque[_QueuedMicrophoneFrame] = deque(maxlen=max_frames)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._accepted_frames = 0
        self._consumed_frames = 0
        self._dropped_frames = 0
        self._intercom_active = False
        self._intercom_generation = 0
        self._speaker_ready = False
        self._speaker_failure: str | None = None

    def begin_intercom(self) -> int:
        """Start a new gated intercom generation and clear stale PCM."""
        with self._condition:
            self._frames.clear()
            self._accepted_frames = 0
            self._consumed_frames = 0
            self._dropped_frames = 0
            self._intercom_generation += 1
            self._intercom_active = True
            self._speaker_ready = False
            self._speaker_failure = None
            self._condition.notify_all()
            return self._intercom_generation

    def end_intercom(self) -> None:
        """Stop the active intercom generation and discard queued PCM."""
        with self._condition:
            self._intercom_active = False
            self._speaker_ready = False
            self._speaker_failure = None
            self._frames.clear()
            self._accepted_frames = 0
            self._consumed_frames = 0
            self._dropped_frames = 0
            self._condition.notify_all()

    def set_speaker_ready(self, generation: int, ready: bool) -> bool:
        """Gate PCM consumption until Ring speaker activation is sent."""
        with self._condition:
            if not self._intercom_active or generation != self._intercom_generation:
                return False
            self._speaker_ready = ready
            if ready:
                self._speaker_failure = None
            self._condition.notify_all()
            return True

    def set_speaker_failure(self, generation: int, failure: str) -> bool:
        """Publish one stable activation failure to readiness waiters."""
        if not isinstance(failure, str) or not failure:
            raise ValueError("speaker failure must be a non-empty string")
        with self._condition:
            if not self._intercom_active or generation != self._intercom_generation:
                return False
            self._speaker_ready = False
            self._speaker_failure = failure
            self._condition.notify_all()
            return True

    def wait_speaker_ready(self, generation: int, timeout: float) -> bool:
        """Wait for speaker activation without exposing microphone content."""
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("speaker readiness timeout must be positive")
        deadline = time.monotonic() + float(timeout)
        with self._condition:
            while (
                self._intercom_active
                and generation == self._intercom_generation
                and not self._speaker_ready
                and self._speaker_failure is None
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return bool(
                self._intercom_active
                and generation == self._intercom_generation
                and self._speaker_ready
            )

    def push(self, payload: bytes) -> None:
        frame = _microphone_frame(payload)
        with self._lock:
            self._append_locked(frame)

    def push_intercom(self, generation: int, payload: bytes, *, admission=None) -> bool:
        """Push PCM only while the matching intercom generation is active."""
        frame = _microphone_frame(payload)
        with self._lock:
            if not self._intercom_active or generation != self._intercom_generation:
                return False
            self._append_locked(frame, admission=admission)
            return True

    def pull(self) -> bytes | None:
        for _ in range(self._frames.maxlen):
            with self._lock:
                if self._intercom_active and not self._speaker_ready:
                    return None
                if not self._frames:
                    return None
                frame = self._frames.popleft()
            # Admission may take the session/controller locks. Never call it
            # under the microphone lock (release takes them in the other order).
            age = self._clock() - frame.queued_at
            try:
                admitted = 0 <= age <= self._max_age and (
                    frame.admission is None or bool(frame.admission())
                )
            except Exception:
                admitted = False
            admitted = admitted and 0 <= self._clock() - frame.queued_at <= self._max_age
            with self._lock:
                if frame.generation is not None:
                    admitted = admitted and (
                        self._intercom_active
                        and self._speaker_ready
                        and frame.generation == self._intercom_generation
                    )
                if not admitted:
                    self._dropped_frames += 1
                    continue
                self._consumed_frames += 1
                return frame.payload
        return None

    def clear(self, *, reset_statistics: bool = False) -> None:
        """Discard queued PCM and require speaker activation for the next transport."""
        with self._condition:
            self._frames.clear()
            self._speaker_ready = False
            self._speaker_failure = None
            if reset_statistics:
                self._accepted_frames = 0
                self._consumed_frames = 0
                self._dropped_frames = 0

    def snapshot(self) -> MicrophonePcmSnapshot:
        """Return queue counters without exposing captured audio."""
        with self._lock:
            return MicrophonePcmSnapshot(
                queued_frames=len(self._frames),
                accepted_frames=self._accepted_frames,
                consumed_frames=self._consumed_frames,
                dropped_frames=self._dropped_frames,
                intercom_active=self._intercom_active,
                intercom_generation=self._intercom_generation,
                speaker_ready=self._speaker_ready,
                speaker_failure=self._speaker_failure,
            )

    def _append_locked(self, frame: bytes, *, admission=None) -> None:
        if len(self._frames) == self._frames.maxlen:
            self._dropped_frames += 1
        self._frames.append(
            _QueuedMicrophoneFrame(
                frame,
                self._clock(),
                self._intercom_generation if self._intercom_active else None,
                admission,
            )
        )
        self._accepted_frames += 1


def _microphone_frame(payload: bytes) -> bytes:
    frame = bytes(payload)
    if len(frame) != MIC_FRAME_BYTES:
        raise ValueError(f"microphone frame must contain exactly {MIC_FRAME_BYTES} bytes")
    return frame


class MicrophoneTrack(AudioStreamTrack):
    """AudioStreamTrack fed only by authenticated plugin PCM, or silence."""

    kind = "audio"

    def __init__(self, pcm_buffer: MicrophonePcmBuffer | None = None) -> None:
        super().__init__()
        self._pcm_buffer = pcm_buffer or MicrophonePcmBuffer()
        self._start: float | None = None
        self._timestamp: int = 0

    async def recv(self) -> AudioFrame:  # type: ignore[override]
        if self.readyState != "live":
            raise MediaStreamError

        # Pace output to a fixed 20 ms interval independent of mic availability.
        if self._start is None:
            self._start = time.monotonic()
            self._timestamp = 0
        else:
            self._timestamp += MIC_SAMPLES
            wait = self._start + (self._timestamp / MIC_SAMPLE_RATE) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)

        frame = AudioFrame(format="s16", layout="mono", samples=MIC_SAMPLES)
        frame.sample_rate = MIC_SAMPLE_RATE
        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, MIC_SAMPLE_RATE)

        raw = self._pcm_buffer.pull()
        if raw is not None:
            frame.planes[0].update(raw)
            return frame

        # Silence fallback — zeroed plane.
        for p in frame.planes:
            p.update(bytes(p.buffer_size))
        return frame


async def audio_sender_snapshot(sender) -> AudioSenderSnapshot:
    """Collect content-free outbound and remote-inbound audio RTP counters."""
    report = await sender.getStats()
    outbound = None
    remote = None
    for stat in report.values():
        kind = getattr(stat, "kind", None)
        if kind != "audio":
            continue
        if getattr(stat, "type", None) == "outbound-rtp":
            outbound = stat
        elif getattr(stat, "type", None) == "remote-inbound-rtp":
            remote = stat
    return AudioSenderSnapshot(
        packets_sent=int(getattr(outbound, "packetsSent", 0)),
        bytes_sent=int(getattr(outbound, "bytesSent", 0)),
        remote_packets_received=_optional_int(remote, "packetsReceived"),
        remote_packets_lost=_optional_int(remote, "packetsLost"),
        remote_fraction_lost=_optional_int(remote, "fractionLost"),
        remote_jitter=_optional_int(remote, "jitter"),
        round_trip_time=_optional_float(remote, "roundTripTime"),
    )


async def manage_intercom_speaker(
    device,
    halo_session_id: str,
    camera_id: str,
    session_reference: str,
    source: MicrophonePcmBuffer,
    sender,
    transceiver,
    peer_connection,
    stop: threading.Event,
    terminal: asyncio.Event,
    *,
    activation_timeout: float = INTERCOM_SPEAKER_ACTIVATION_TIMEOUT,
    retry_interval: float = INTERCOM_SPEAKER_RETRY_SECONDS,
    peer_ready_timeout: float = INTERCOM_PEER_READY_TIMEOUT,
) -> None:
    """Latch one Ring camera speaker on and gate each intercom generation."""
    pending_generation: int | None = None
    active_generation: int | None = None
    baseline: AudioSenderSnapshot | None = None
    baseline_attempted = False
    speaker_latched = False
    activation_deadline = 0.0
    next_attempt = 0.0
    last_failure = "activation_timeout"
    try:
        while not stop.is_set() and not terminal.is_set():
            snapshot = source.snapshot()
            if snapshot.intercom_active and snapshot.intercom_generation != pending_generation:
                generation = snapshot.intercom_generation
                pending_generation = generation
                active_generation = None
                baseline = None
                baseline_attempted = False
                speaker_latched = False
                activation_deadline = 0.0
                next_attempt = 0.0
                last_failure = "activation_timeout"

            should_activate = bool(
                snapshot.intercom_active
                and snapshot.accepted_frames > 0
                and not snapshot.speaker_ready
                and snapshot.speaker_failure is None
                and pending_generation == snapshot.intercom_generation
            )
            now = asyncio.get_running_loop().time()
            if should_activate and activation_deadline == 0:
                activation_deadline = now + activation_timeout
            if should_activate and now >= activation_deadline:
                generation = snapshot.intercom_generation
                if source.set_speaker_failure(generation, last_failure):
                    _log.warning(
                        "Ring intercom speaker activation failed camera_id=%s "
                        "session=%s generation=%d reason=%s terminal=true",
                        camera_id,
                        session_reference,
                        generation,
                        last_failure,
                    )
            elif should_activate and now >= next_attempt:
                generation = snapshot.intercom_generation
                peer_ready = await _wait_for_peer_connection(
                    peer_connection,
                    stop,
                    terminal,
                    timeout=min(
                        peer_ready_timeout,
                        max(INTERCOM_SPEAKER_POLL_SECONDS, activation_deadline - now),
                    ),
                )
                if not peer_ready:
                    last_failure = "peer_not_connected"
                else:
                    if not baseline_attempted:
                        baseline_attempted = True
                        baseline = await _safe_audio_sender_snapshot(
                            sender,
                            camera_id,
                            session_reference,
                        )
                    try:
                        remaining = activation_deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise TimeoutError
                        async with asyncio.timeout(remaining):
                            command_sent = await activate_ring_camera_speaker(
                                device,
                                halo_session_id,
                            )
                    except TimeoutError:
                        last_failure = "activation_timeout"
                    except RingSpeakerControlError as exc:
                        last_failure = _diagnostic_value(exc)
                    else:
                        speaker_latched = True
                        current = source.snapshot()
                        if (
                            current.intercom_active
                            and current.intercom_generation == generation
                            and source.set_speaker_ready(generation, True)
                        ):
                            active_generation = generation
                            _log.info(
                                "Ring intercom speaker active camera_id=%s session=%s "
                                "generation=%d command_sent=%s direction=%s",
                                camera_id,
                                session_reference,
                                generation,
                                command_sent,
                                _safe_direction(getattr(transceiver, "currentDirection", None)),
                            )
                next_attempt = asyncio.get_running_loop().time() + retry_interval

            snapshot = source.snapshot()
            if active_generation is not None and (
                not snapshot.intercom_active or snapshot.intercom_generation != active_generation
            ):
                await _log_intercom_rtp_summary(
                    camera_id,
                    session_reference,
                    active_generation,
                    sender,
                    baseline,
                    speaker_latched,
                )
                active_generation = None
                baseline = None
                baseline_attempted = False
            if not snapshot.intercom_active:
                pending_generation = None
            await asyncio.sleep(INTERCOM_SPEAKER_POLL_SECONDS)
    finally:
        if active_generation is not None:
            await _log_intercom_rtp_summary(
                camera_id,
                session_reference,
                active_generation,
                sender,
                baseline,
                speaker_latched,
            )


async def _log_intercom_rtp_summary(
    camera_id: str,
    session_reference: str,
    generation: int,
    sender,
    baseline: AudioSenderSnapshot | None,
    speaker_latched: bool,
) -> None:
    final = await _safe_audio_sender_snapshot(sender, camera_id, session_reference)
    if final is None:
        _log.info(
            "Ring intercom RTP summary camera_id=%s session=%s generation=%d "
            "statistics=unavailable speaker_latched=%s",
            camera_id,
            session_reference,
            generation,
            speaker_latched,
        )
        return
    start = baseline or AudioSenderSnapshot()
    _log.info(
        "Ring intercom RTP summary camera_id=%s session=%s generation=%d "
        "packets_sent=%d bytes_sent=%d remote_received=%s remote_lost=%s "
        "fraction_lost=%s jitter=%s rtt=%s speaker_latched=%s",
        camera_id,
        session_reference,
        generation,
        max(0, final.packets_sent - start.packets_sent),
        max(0, final.bytes_sent - start.bytes_sent),
        _counter_delta(start.remote_packets_received, final.remote_packets_received),
        _diagnostic_counter(final.remote_packets_lost),
        _diagnostic_counter(final.remote_fraction_lost),
        _diagnostic_counter(final.remote_jitter),
        _diagnostic_float(final.round_trip_time),
        speaker_latched,
    )


async def _wait_for_peer_connection(
    peer_connection,
    stop: threading.Event,
    terminal: asyncio.Event,
    *,
    timeout: float = INTERCOM_PEER_READY_TIMEOUT,
) -> bool:
    try:
        async with asyncio.timeout(timeout):
            while not stop.is_set() and not terminal.is_set():
                state = getattr(peer_connection, "connectionState", None)
                if state == "connected":
                    return True
                if state in {"failed", "closed"}:
                    return False
                await asyncio.sleep(INTERCOM_SPEAKER_POLL_SECONDS)
    except TimeoutError:
        return False
    return False


async def _safe_audio_sender_snapshot(
    sender,
    camera_id: str,
    session_reference: str,
) -> AudioSenderSnapshot | None:
    try:
        async with asyncio.timeout(INTERCOM_STATISTICS_TIMEOUT):
            return await audio_sender_snapshot(sender)
    except Exception as exc:
        _log.debug(
            "Ring intercom RTP statistics unavailable camera_id=%s session=%s reason=%s",
            camera_id,
            session_reference,
            _diagnostic_value(exc),
        )
        return None


def _optional_int(value, attribute: str) -> int | None:
    raw = getattr(value, attribute, None)
    return int(raw) if isinstance(raw, int | float) else None


def _optional_float(value, attribute: str) -> float | None:
    raw = getattr(value, attribute, None)
    return float(raw) if isinstance(raw, int | float) else None


def _counter_delta(start: int | None, end: int | None) -> str:
    if start is None or end is None:
        return "unknown"
    return str(max(0, end - start))


def _diagnostic_counter(value: int | None) -> str:
    return "unknown" if value is None else str(value)


def _diagnostic_float(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.3f}"


def _safe_direction(value: object) -> str:
    direction = str(value)
    if direction in {"sendrecv", "sendonly", "recvonly", "inactive"}:
        return direction
    return "unknown"


async def _settle_cleanup(tasks) -> None:
    """Keep ownership through cancellation until real cleanup has completed."""
    if not tasks:
        return
    settled = asyncio.gather(*tasks, return_exceptions=True)
    cancelled = False
    while not settled.done():
        try:
            await asyncio.shield(settled)
        except asyncio.CancelledError:
            cancelled = True
    settled.result()
    if cancelled:
        raise asyncio.CancelledError


async def _run_bounded_cleanup(
    awaitable: Awaitable[object],
    *,
    timeout: float,
    label: str,
    camera_id: str,
    session_reference: str,
) -> bool:
    task = asyncio.create_task(awaitable)
    try:
        done, _ = await asyncio.wait((task,), timeout=timeout)
    except asyncio.CancelledError:
        task.cancel()
        await _settle_cleanup((task,))
        raise
    if not done:
        task.cancel()
        _log.warning(
            "%s timed out camera_id=%s session=%s timeout=%.1f",
            label,
            camera_id,
            session_reference,
            timeout,
        )
        await _settle_cleanup((task,))
        return False
    try:
        task.result()
    except asyncio.CancelledError:
        _log.warning(
            "%s was cancelled camera_id=%s session=%s",
            label,
            camera_id,
            session_reference,
        )
        return False
    except Exception as exc:
        _log.warning(
            "%s failed camera_id=%s session=%s reason=%s",
            label,
            camera_id,
            session_reference,
            _diagnostic_value(exc),
        )
        return False
    return True


class RingStreamReceiver:
    """Negotiate a Ring WebRTC stream and deliver decoded frames via callbacks.

    Headless — no GTK.  Construct with a ring-doorbell camera device and a video
    (and optional audio) callback, then run ``receive()`` as a coroutine on the
    RingClient's asyncio loop until *stop* is set.

        on_video(raw_rgb: bytes, width: int, height: int)   # width padded to a multiple of 4
        on_audio(raw_s16le: bytes, channels: int, rate: int)

    The callbacks fire on the asyncio thread; keep them cheap (an appsrc
    push-buffer is fine and thread-safe).
    """

    def __init__(
        self,
        device,
        on_video,
        on_audio=None,
        *,
        on_encoded_video: Callable[[bytes, int], None] | None = None,
        on_encoded_audio: Callable[[bytes, int], None] | None = None,
        on_source_clock: Callable[[SourceClockObservation], None] | None = None,
        on_encoded_ingress: Callable | None = None,
        on_receiver_stats: Callable | None = None,
        decode_video: bool = True,
        decode_audio: bool = True,
        on_encoded_incompatible: Callable[[str, str], None] | None = None,
        microphone_source: MicrophonePcmBuffer | None = None,
        first_video_timeout: float = FIRST_VIDEO_TIMEOUT,
        video_stall_timeout: float = VIDEO_STALL_TIMEOUT,
        on_terminating: Callable[[BaseException | None], None] | None = None,
    ) -> None:
        self._device = device
        self._on_video = on_video
        self._on_audio = on_audio
        self._on_encoded_video = on_encoded_video
        self._on_encoded_audio = on_encoded_audio
        self._on_source_clock = on_source_clock
        self._on_encoded_ingress = on_encoded_ingress
        self._on_receiver_stats = on_receiver_stats
        self._decode_video = decode_video
        self._decode_audio = decode_audio
        self._on_encoded_incompatible = on_encoded_incompatible
        self._microphone_source = microphone_source
        for value in (first_video_timeout, video_stall_timeout):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("Video progress timeout must be finite and positive")
        self._first_video_timeout = first_video_timeout
        self._video_stall_timeout = video_stall_timeout
        self._on_terminating = on_terminating

    async def receive(self, stop: threading.Event) -> None:
        import numpy as np
        from aiortc import (
            RTCConfiguration,
            RTCIceServer,
            RTCPeerConnection,
            RTCSessionDescription,
        )
        from aiortc.sdp import candidate_from_sdp

        session_id = str(uuid.uuid4())
        session_reference = session_id[:8]
        camera_id = _diagnostic_value(getattr(self._device, "id", "unknown"))
        camera_kind = _diagnostic_value(getattr(self._device, "kind", "unknown"))
        loop = asyncio.get_running_loop()
        pc = None
        mic_track = None
        ring_session_started = False
        terminal = asyncio.Event()
        answer_applied = asyncio.Event()
        stream_errors: list[str] = []
        remote_description_error: Exception | None = None
        tasks: set[asyncio.Task] = set()
        last_video_at: float | None = None
        startup_expires_at = loop.time() + self._first_video_timeout
        startup_deadline = None
        have_sps = have_pps = have_keyframe = False

        def video_progress() -> None:
            nonlocal last_video_at
            last_video_at = loop.time()
            if startup_deadline is not None and not startup_deadline.expired():
                startup_deadline.reschedule(None)

        def encoded_video(payload: bytes, timestamp: int):
            nonlocal have_sps, have_pps, have_keyframe
            if (
                stop.is_set()
                or terminal.is_set()
                or (last_video_at is None and loop.time() >= startup_expires_at)
            ):
                return False
            accepted = self._on_encoded_video(payload, timestamp)
            nal_types = h264_nal_types(payload)
            have_sps |= 7 in nal_types
            have_pps |= 8 in nal_types
            have_keyframe |= have_sps and have_pps and 5 in nal_types
            if accepted is not False and have_keyframe and nal_types & {1, 5}:
                video_progress()
            return accepted

        def retain(task: asyncio.Task, label: str) -> asyncio.Task:
            if task in tasks:
                return task
            tasks.add(task)

            def observe(completed: asyncio.Task) -> None:
                tasks.discard(completed)
                if completed.cancelled():
                    return
                try:
                    error = completed.exception()
                except asyncio.CancelledError:
                    return
                if error is not None:
                    _log.warning("%s failed for %s: %s", label, self._device.name, error)

            task.add_done_callback(observe)
            return task

        def spawn(coro, label: str) -> asyncio.Task:
            return retain(loop.create_task(coro), label)

        async def recv_video(track) -> None:
            try:
                while not stop.is_set():
                    try:
                        frame = await track.recv()
                    except MediaStreamError:
                        break
                    rgb = frame.to_ndarray(format="rgb24")
                    h, w = rgb.shape[:2]
                    w4 = (w + 3) & ~3
                    raw = (
                        np.pad(rgb, ((0, 0), (0, w4 - w), (0, 0))).tobytes()
                        if w4 != w
                        else rgb.tobytes()
                    )
                    if last_video_at is None and loop.time() >= startup_expires_at:
                        continue
                    if self._on_video is not None:
                        try:
                            if self._on_video(raw, w4, h) is not False:
                                video_progress()
                        except Exception as exc:  # noqa: BLE001
                            _log.debug("on_video error: %s", exc)
            finally:
                terminal.set()

        async def recv_audio(track) -> None:
            while not stop.is_set() and not terminal.is_set():
                try:
                    frame = await track.recv()
                except MediaStreamError:
                    break
                raw, channels, rate, _samples = audio_frame_to_s16le_interleaved(frame)
                if self._on_audio is not None:
                    try:
                        self._on_audio(raw, channels, rate)
                    except Exception as exc:  # noqa: BLE001
                        _log.debug("on_audio error: %s", exc)

        async def on_answer(sdp) -> None:
            nonlocal remote_description_error
            try:
                _log.info(
                    "Ring WebRTC answer camera_id=%s kind=%s session=%s codecs=%s",
                    camera_id,
                    camera_kind,
                    session_reference,
                    summarize_sdp_codecs(sdp),
                )
                async with asyncio.timeout(WEBRTC_REMOTE_DESCRIPTION_TIMEOUT):
                    await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))
                answer_applied.set()
                _log.info(
                    "Ring WebRTC answer applied camera_id=%s session=%s "
                    "remote_audio_direction=%s current_audio_direction=%s",
                    camera_id,
                    session_reference,
                    summarize_sdp_audio_direction(sdp),
                    _safe_direction(getattr(audio_transceiver, "currentDirection", None)),
                )
            except Exception as exc:  # noqa: BLE001
                remote_description_error = exc
                _log.warning(
                    "Ring WebRTC answer rejected camera_id=%s session=%s reason=%s",
                    camera_id,
                    session_reference,
                    _diagnostic_value(exc),
                )
                terminal.set()
            finally:
                retain_aiortc_connect_tasks(pc, retain)

        async def on_cand(cand) -> None:
            try:
                await pc.addIceCandidate(cand)
            except Exception as exc:  # noqa: BLE001
                _log.debug("cand err: %s", exc)

        def on_rtc_message(msg) -> None:
            if stop.is_set() or terminal.is_set():
                return
            if msg.answer:
                spawn(on_answer(msg.answer), "Ring answer")
            elif msg.candidate is not None and msg.sdp_m_line_index is not None:
                try:
                    cand = candidate_from_sdp(msg.candidate)
                    cand.sdpMLineIndex = msg.sdp_m_line_index
                    cand.sdpMid = str(msg.sdp_m_line_index)
                    spawn(on_cand(cand), "Ring ICE candidate")
                except Exception as exc:  # noqa: BLE001
                    _log.debug("cand parse: %s", exc)
            elif getattr(msg, "error_code", None):
                message = f"Ring stream error {msg.error_code}: {msg.error_message}"
                stream_errors.append(message)
                _log.error(message)
                terminal.set()

        def on_encoded_incompatible(kind: str, mime_type: str) -> None:
            message = f"Ring negotiated unsupported {kind} codec for encoded transport: {mime_type}"
            stream_errors.append(message)
            if self._on_encoded_incompatible is not None:
                self._on_encoded_incompatible(kind, mime_type)
            terminal.set()

        try:
            async with asyncio.timeout(self._first_video_timeout) as startup_deadline:
                pc = RTCPeerConnection(
                    RTCConfiguration(iceServers=[RTCIceServer(urls=self._device.get_ice_servers())])
                )
                video_transceiver = pc.addTransceiver("video", direction="recvonly")
                mic_track = MicrophoneTrack(self._microphone_source)
                audio_transceiver = pc.addTransceiver(mic_track, direction="sendrecv")

                def source_callback(receiver, kind, callback):
                    return _encoded_source_callback(
                        receiver,
                        kind,
                        callback,
                        self._on_source_clock,
                        self._on_encoded_ingress,
                    )

                if self._on_encoded_video is not None:
                    install_encoded_frame_adapter(
                        video_transceiver.receiver,
                        "video",
                        source_callback(video_transceiver.receiver, "video", encoded_video),
                        forward_to_decoder=self._decode_video,
                        on_incompatible=on_encoded_incompatible,
                    )
                if self._on_encoded_audio is not None:
                    install_encoded_frame_adapter(
                        audio_transceiver.receiver,
                        "audio",
                        source_callback(
                            audio_transceiver.receiver, "audio", self._on_encoded_audio
                        ),
                        forward_to_decoder=self._decode_audio,
                        on_incompatible=on_encoded_incompatible,
                    )

                @pc.on("icecandidate")
                def _on_ice(candidate) -> None:
                    if candidate is not None and not terminal.is_set():
                        spawn(
                            self._device.on_webrtc_candidate(
                                session_id,
                                candidate.candidate,
                                candidate.sdpMLineIndex or 0,
                            ),
                            "local ICE candidate",
                        )

                @pc.on("track")
                def _on_track(track) -> None:
                    _log.info(
                        "Ring WebRTC track camera_id=%s kind=%s session=%s media=%s",
                        camera_id,
                        camera_kind,
                        session_reference,
                        _diagnostic_value(track.kind),
                    )
                    if track.kind == "video" and self._decode_video:
                        spawn(recv_video(track), "video receiver")
                    elif track.kind == "audio" and self._decode_audio:
                        spawn(recv_audio(track), "audio receiver")

                @pc.on("connectionstatechange")
                def _on_connection_state_change() -> None:
                    state = getattr(pc, "connectionState", None)
                    _log.info(
                        "Ring WebRTC connection camera_id=%s session=%s state=%s",
                        camera_id,
                        session_reference,
                        _diagnostic_value(state),
                    )
                    if state in {"failed", "closed"}:
                        if state == "failed":
                            stream_errors.append("WebRTC peer connection failed")
                        terminal.set()

                @pc.on("iceconnectionstatechange")
                def _on_ice_connection_state_change() -> None:
                    _log.info(
                        "Ring WebRTC ICE camera_id=%s session=%s state=%s",
                        camera_id,
                        session_reference,
                        _diagnostic_value(getattr(pc, "iceConnectionState", None)),
                    )

                try:
                    async with asyncio.timeout(WEBRTC_OFFER_TIMEOUT):
                        offer = await pc.createOffer()
                        await pc.setLocalDescription(offer)
                except TimeoutError as exc:
                    raise RingMediaNegotiationError(
                        "Ring WebRTC local offer preparation timed out"
                    ) from exc
                _log.info(
                    "Ring WebRTC offer camera_id=%s kind=%s session=%s codecs=%s",
                    camera_id,
                    camera_kind,
                    session_reference,
                    summarize_sdp_codecs(pc.localDescription.sdp),
                )
                retain_aiortc_connect_tasks(pc, retain)
                ring_session_started = True
                try:
                    async with asyncio.timeout(WEBRTC_SIGNALING_TIMEOUT):
                        await self._device.generate_async_webrtc_stream(
                            pc.localDescription.sdp,
                            session_id,
                            on_rtc_message,
                            keep_alive_timeout=300,
                        )
                except TimeoutError as exc:
                    raise RingMediaNegotiationError("Ring WebRTC signaling timed out") from exc
                _log.info(
                    "Ring WebRTC initiated camera_id=%s kind=%s session=%s",
                    camera_id,
                    camera_kind,
                    session_reference,
                )
                if not stop.is_set() and not terminal.is_set():
                    try:
                        async with asyncio.timeout(WEBRTC_ANSWER_TIMEOUT):
                            while (
                                not answer_applied.is_set()
                                and not stop.is_set()
                                and not terminal.is_set()
                            ):
                                await asyncio.sleep(INTERCOM_SPEAKER_POLL_SECONDS)
                    except TimeoutError as exc:
                        raise RingMediaNegotiationError("Ring WebRTC answer timed out") from exc
                if remote_description_error is not None:
                    raise RingMediaNegotiationError(
                        "Ring WebRTC remote description could not be applied"
                    ) from remote_description_error
                if stream_errors:
                    if stream_errors[-1].startswith("Ring negotiated unsupported"):
                        raise RingMediaUnsupportedCodecError(stream_errors[-1])
                    raise RuntimeError(stream_errors[-1])
                audio_sender = getattr(audio_transceiver, "sender", None)
                if (
                    answer_applied.is_set()
                    and self._microphone_source is not None
                    and audio_sender is not None
                ):
                    spawn(
                        manage_intercom_speaker(
                            self._device,
                            session_id,
                            camera_id,
                            session_reference,
                            self._microphone_source,
                            audio_sender,
                            audio_transceiver,
                            pc,
                            stop,
                            terminal,
                        ),
                        "intercom speaker controller",
                    )
                if answer_applied.is_set() and self._on_encoded_video is not None:
                    spawn(
                        request_startup_keyframes(
                            video_transceiver.receiver,
                            stop,
                            terminal,
                        ),
                        "startup keyframe requests",
                    )
                if answer_applied.is_set() and self._on_receiver_stats is not None:
                    spawn(
                        observe_receiver_statistics(
                            audio_transceiver.receiver,
                            video_transceiver.receiver,
                            stop,
                            terminal,
                            self._on_receiver_stats,
                        ),
                        "receiver statistics",
                    )
                while not stop.is_set() and not terminal.is_set():
                    if last_video_at is None and loop.time() >= startup_expires_at:
                        raise RingMediaProgressError("video_startup_timeout")
                    if last_video_at is not None and (
                        loop.time() - last_video_at >= self._video_stall_timeout
                    ):
                        raise RingMediaProgressError("video_stalled")
                    await asyncio.sleep(0.1)
                if remote_description_error is not None:
                    raise RingMediaNegotiationError(
                        "Ring WebRTC remote description could not be applied"
                    ) from remote_description_error
                if stream_errors:
                    if stream_errors[-1].startswith("Ring negotiated unsupported"):
                        raise RingMediaUnsupportedCodecError(stream_errors[-1])
                    raise RuntimeError(stream_errors[-1])
        except TimeoutError as exc:
            terminal.set()
            raise RingMediaProgressError("video_startup_timeout") from exc
        finally:
            terminal.set()
            if self._on_terminating is not None:
                try:
                    self._on_terminating(sys.exception())
                except Exception:
                    _log.warning("Media termination callback failed camera_id=%s", camera_id)
            _log.info(
                "Ring WebRTC stopping camera_id=%s session=%s requested=%s terminal=%s "
                "errors=%d connection=%s ice=%s",
                camera_id,
                session_reference,
                stop.is_set(),
                terminal.is_set(),
                len(stream_errors),
                _diagnostic_value(getattr(pc, "connectionState", None)),
                _diagnostic_value(getattr(pc, "iceConnectionState", None)),
            )

            async def cleanup():
                if mic_track is not None:
                    with contextlib.suppress(Exception):
                        mic_track.stop()

                pending = [task for task in tasks if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    _, unfinished = await asyncio.wait(
                        pending,
                        timeout=RECEIVER_TASK_CLEANUP_TIMEOUT,
                    )
                    if unfinished:
                        for task in unfinished:
                            task.cancel()
                        _log.warning(
                            "Ring WebRTC task cleanup timed out camera_id=%s session=%s tasks=%d",
                            camera_id,
                            session_reference,
                            len(unfinished),
                        )

                closers = []
                if ring_session_started:
                    closers.append(
                        asyncio.create_task(
                            _run_bounded_cleanup(
                                self._device.close_webrtc_stream(session_id),
                                timeout=RECEIVER_CLOSE_TIMEOUT,
                                label="Ring WebRTC stream close",
                                camera_id=camera_id,
                                session_reference=session_reference,
                            )
                        )
                    )
                if pc is not None:
                    closers.append(
                        asyncio.create_task(
                            _run_bounded_cleanup(
                                pc.close(),
                                timeout=RECEIVER_CLOSE_TIMEOUT,
                                label="WebRTC peer close",
                                camera_id=camera_id,
                                session_reference=session_reference,
                            )
                        )
                    )
                await _settle_cleanup([*closers, *pending])

            await _settle_cleanup((asyncio.create_task(cleanup()),))


def summarize_sdp_codecs(sdp: object) -> str:
    """Return only codec and H.264 profile metadata from SDP."""
    if not isinstance(sdp, str):
        return "none"
    media = "unknown"
    payload_media: dict[str, str] = {}
    codecs: list[str] = []
    profiles: dict[str, str] = {}
    for raw_line in sdp.splitlines():
        line = raw_line.strip()
        if line.startswith("m="):
            media = line[2:].split(" ", 1)[0].lower() or "unknown"
            continue
        if line.startswith("a=rtpmap:"):
            fields = line[len("a=rtpmap:") :].split(None, 1)
            if len(fields) != 2:
                continue
            payload, codec = fields
            normalized_codec = codec.split("/", 1)[0].lower()
            payload_media[payload] = media
            item = f"{media}:{normalized_codec}"
            if item not in codecs:
                codecs.append(item)
            continue
        if not line.startswith("a=fmtp:"):
            continue
        fields = line[len("a=fmtp:") :].split(None, 1)
        if len(fields) != 2:
            continue
        payload, parameters = fields
        for parameter in parameters.split(";"):
            key, separator, value = parameter.strip().partition("=")
            if separator and key.lower() == "profile-level-id":
                normalized = value.strip().lower()
                if normalized and len(normalized) <= 16:
                    profiles[payload] = normalized
    for payload, profile in profiles.items():
        profile_item = f"{payload_media.get(payload, 'unknown')}:h264-profile-{profile}"
        if profile_item not in codecs:
            codecs.append(profile_item)
    return ",".join(codecs) if codecs else "none"


def summarize_sdp_audio_direction(sdp: object) -> str:
    """Return only the audio media direction from SDP."""
    if not isinstance(sdp, str):
        return "unknown"
    in_audio = False
    for raw_line in sdp.splitlines():
        line = raw_line.strip()
        if line.startswith("m="):
            in_audio = line.startswith("m=audio ")
            continue
        if in_audio and line.startswith("a="):
            direction = line[2:]
            if direction in {"sendrecv", "sendonly", "recvonly", "inactive"}:
                return direction
    return "unspecified"


def _diagnostic_value(value: object) -> str:
    normalized = str(value or "unknown")[:_DIAGNOSTIC_VALUE_LIMIT]
    return "".join(
        character if character.isalnum() or character in "._-" else "_" for character in normalized
    )
