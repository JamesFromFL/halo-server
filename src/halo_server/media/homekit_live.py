"""One scoped native UDP output owner over Halo's existing capture handles."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import ipaddress
import logging
import math
import secrets
import socket
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from uuid import UUID

from halo_server.homekit.models import HomeKitOperationError, MediaScope
from halo_server.media.buffer import MediaStreamKind
from halo_server.media.h264 import H264Error, SpsInfo, parse_sps, pps_sequence_id
from halo_server.media.homekit_audio import AudioEndpointRequest, AudioTrack, PreparedAudio
from halo_server.media.live_timeline import LiveTimeline
from halo_server.media.manager import settle_owned
from halo_server.media.rtp import (
    MAX_ACCESS_UNIT_BYTES,
    H264Packetizer,
    ReceiverReportStats,
    RtpError,
    annexb_nalus,
    parse_feedback,
    sender_report,
)
from halo_server.media.srtp import SecureVideo, SrtpError
from halo_server.media.telemetry import StreamMetrics, safe_diagnostics
from halo_server.media.video_budget import CodecRateMonitor, VideoBudgetError, VideoWirePolicy
from halo_server.media.video_repair import RepairQueue, RepairRate

PREPARE_SECONDS = 15.0
SOURCE_SECONDS = 25.0
RECEIVER_SECONDS = 60.0
CLEANUP_SECONDS = 5.0
SEND_SECONDS = 0.25
MAX_SESSIONS = 8
MAX_CAMERA_SESSIONS = 2
MAX_RETRANSMIT_BYTES = 1024 * 1024
MAX_RETRANSMIT_PACKETS = 512
RETRANSMIT_SECONDS = 1.0
MAX_STOP_TOMBSTONES = 512
STOP_TOMBSTONE_SECONDS = 60.0
MAX_FRAME_AGE = 0.5
MAX_ACCESS_UNIT_SECONDS = 1.0
RECOVERY_SECONDS = 6.0
MAX_RECOVERY_IDRS = 3
_log = logging.getLogger(__name__)
_TRACE_STAGES = frozenset(
    {
        "start",
        "stop",
        "lifecycle",
        "video_send",
        "video_feedback",
        "video_reports",
        "audio_send",
        "audio_feedback",
        "audio_reports",
    }
)
_TRACE_REASONS = frozenset(
    {
        "media_failed",
        "feedback_failed",
        "receiver_closed",
        "receiver_timeout",
        "authority_changed",
        "prepare_expired",
        "audio_output_failed",
        "audio_feedback_failed",
        "audio_report_failed",
        "stop_request",
        "boot_retired",
        "start_failed",
        "retirement",
    }
)
_TRACE_ERRORS = frozenset(
    {
        "HomeKitOperationError",
        "H264Error",
        "VideoTransformError",
        "TimelineError",
        "RtpError",
        "SrtpError",
        "AudioTransformError",
        "OpusError",
        "OSError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "BrokenPipeError",
        "TimeoutError",
        "IncompleteReadError",
        "ValueError",
        "RuntimeError",
        "CancelledError",
    }
)
_TRACE_ERROR_CODES = frozenset(
    {
        "media_saturated",
        "media_failed",
        "source_timeout",
        "receiver_timeout",
        "authority_changed",
        "policy_denied",
        "scope_changed",
        "stale_scope",
        "boot_changed",
        "closed",
        "not_ready",
        "invalid_request",
        "busy",
        "timeout",
        "expired",
        "cancelled",
        "backend_failed",
        "cleanup_pending",
    }
)


def _trace_label(value, allowed):
    return value if type(value) is str and value in allowed else "other"


def _trace_age(now, observed):
    if type(observed) not in (int, float) or not observed or not math.isfinite(observed):
        return None
    return min(3_600_000, round(max(0, now - observed) * 1000))


def _format_diagnostics(adapter, attribute):
    with contextlib.suppress(Exception):
        info = getattr(adapter, attribute, None)
        if isinstance(info, SpsInfo):
            rate = info.frame_rate
            return {
                "width": info.width,
                "height": info.height,
                "profile": _trace_label(info.profile, {"baseline", "main", "high"}),
                "level_idc": info.level_idc,
                "frame_rate": (
                    {"numerator": rate.numerator, "denominator": rate.denominator}
                    if rate is not None
                    else None
                ),
                "max_reorder_frames": info.max_reorder_frames,
            }
    return None


def _video_metrics():
    return StreamMetrics(
        counters=(
            "source_access_units",
            "source_bytes",
            "source_nonincreasing_pts",
            "adapter_feeds",
            "output_access_units",
            "output_keyframes",
            "copy_units",
            "adapted_units",
            "output_nonincreasing_pts",
            "stale_output_units",
            "dependency_skips",
            "epoch_changes",
            "reconfigurations",
            "sent_access_units",
            "sent_keyframes",
            "sent_packets",
            "sent_payload_bytes",
            "sent_protected_bytes",
            "retransmit_packets",
            "retransmit_payload_bytes",
            "retransmit_protected_bytes",
            "feedback_datagrams",
            "feedback_rejected",
            "feedback_rate_limited",
            "receiver_reports",
            "nack_requests",
            "nack_cache_misses",
            "nack_rate_limited",
            "repair_duplicate_requests",
            "repair_queue_full",
            "repair_deadline_rejected",
            "repair_send_rate_limited",
            "pli_requests",
            "pli_applied",
            "sender_reports",
            "source_keyframe_requests",
            "source_keyframe_attempted",
            "source_keyframe_rejected",
            "access_unit_budget_rejected",
            "access_unit_deadline_rejected",
            "partial_access_units",
            "recovery_idr_rejected",
        ),
        maxima=("source_access_unit_bytes", "output_access_unit_bytes", "batch_packets"),
        timings=(
            "source_pts_step",
            "source_receipt_step",
            "output_pts_step",
            "source_read",
            "adapter_feed",
            "source_age",
            "pacer_wait",
            "pacer_late",
            "access_unit_send",
            "sent_keyframe_interval",
            "loop_lag",
        ),
    )


def _measure(session, method, name, value=1):
    # Even an optional diagnostic implementation failure must not alter media.
    with contextlib.suppress(Exception):
        getattr(session.metrics, method)(name, value)


def _diagnostic(session, stage, *, payload=None, **fields):
    """The opt-in recorder must never participate in media success/failure."""
    trace = getattr(session, "diagnostic_trace", None)
    if trace is not None:
        with contextlib.suppress(BaseException):
            fields["stream_id"] = session.diagnostic_stream_id
            if payload is None:
                trace.event(stage, "video", **fields)
            else:
                # Media excerpts have a short rolling window. Keep the frame
                # chronology for the entire capture independently of that window.
                fields["size"] = len(payload)
                trace.event(stage, "video", **fields)
                trace.media(stage, "video", payload, **fields)


class _SourceChanged(RuntimeError):
    """An inner capture attempt retired while encoded output was being prepared."""


class _VideoDeadline(RuntimeError):
    """A datagram cannot be admitted within its original source deadline."""


class _RepairRateLimited(RuntimeError):
    """The independent rolling repair send budget is exhausted."""


@dataclass(frozen=True)
class PreparedVideo:
    port: int
    ssrc: int
    address: str
    key: bytes = field(repr=False)
    salt: bytes = field(repr=False)
    audio: PreparedAudio | None = None


@dataclass(eq=False)
class _Session:
    boot: str
    scope: MediaScope
    identity: str
    prepared: PreparedVideo
    endpoint: tuple
    source: str
    destination: str
    family: str
    socket: socket.socket
    secure: SecureVideo
    packetizer: H264Packetizer
    prepared_at: float
    state: str = "prepared"
    retired: bool = False
    handle: object | None = None
    adapter: object | None = None
    config: object | None = None
    audio: AudioTrack | None = None
    audio_config: object | None = None
    speaker_volume: int = 100
    start_task: asyncio.Task | None = None
    sender: asyncio.Task | None = None
    receiver: asyncio.Task | None = None
    reports: asyncio.Task | None = None
    repair_worker: asyncio.Task | None = None
    cleanup: asyncio.Task | None = None
    failure: str | None = None
    adapter_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_source: float = 0
    last_feedback: float = 0
    last_renewal: float = 0
    output_base: int = field(default_factory=lambda: secrets.randbits(32))
    last_timestamp: int | None = None
    last_packet_at: float = 0
    awaiting_idr: bool = True
    idr_requested: bool = False
    recovery_since: float | None = None
    recovery_attempts: int = 0
    recovery_revision: int = 0
    capture_epoch: int | None = None
    next_packet_at: float = 0
    packets: int = 0
    octets: int = 0
    retransmit: OrderedDict = field(default_factory=OrderedDict)
    retransmit_bytes: int = 0
    last_nack_window: float = 0
    nack_count: int = 0
    repair_queue: RepairQueue = field(default_factory=RepairQueue)
    repair_requests: RepairRate = field(default_factory=RepairRate)
    repair_sends: RepairRate = field(default_factory=RepairRate)
    last_pli: float = 0
    upstream_keyframe_pending: bool = False
    last_upstream_keyframe: float = 0
    timeline: LiveTimeline | None = None
    timing_mode: str = "receiver_estimated"
    trace_first_frame: bool = False
    trace_terminal: bool = False
    metrics: StreamMetrics = field(default_factory=_video_metrics)
    receiver_stats: ReceiverReportStats = field(default_factory=ReceiverReportStats)
    codec_rate: CodecRateMonitor = field(default_factory=CodecRateMonitor)
    diagnostic_source_pts: int | None = None
    diagnostic_source_receipt: float | None = None
    diagnostic_output_pts: int | None = None
    diagnostic_keyframe_pts: int | None = None
    terminal: dict | None = None
    active_started_at: float | None = None
    terminal_at: float | None = None
    diagnostic_trace: object | None = None
    diagnostic_stream_id: int = 0


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise HomeKitOperationError("invalid_request") from exc


def _address(value: str, version: str):
    if not isinstance(value, str) or len(value) > 128 or version not in {"ipv4", "ipv6"}:
        raise HomeKitOperationError("invalid_destination")
    try:
        parsed = ipaddress.ip_address(value)
        if version == "ipv4" and isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
            parsed = parsed.ipv4_mapped
        if parsed.version != (4 if version == "ipv4" else 6):
            raise ValueError
        if (
            parsed.is_unspecified
            or parsed.is_multicast
            or parsed.is_reserved
            and not parsed.is_loopback
        ):
            raise ValueError
        scope = getattr(parsed, "scope_id", None)
        scope_id = 0
        if scope is not None:
            scope_id = int(scope) if scope.isdecimal() else socket.if_nametoindex(scope)
            if scope_id < 1:
                raise ValueError
            socket.if_indextoname(scope_id)
        return parsed, scope_id
    except (ValueError, OSError) as exc:
        raise HomeKitOperationError("invalid_destination") from exc


async def _noop(*_args):
    return None


class HomeKitLiveManager:
    """Prepared sockets, native workers and capture leases retire together.

    PREPARE never acquires a Ring source. Unsuccessful cleanup retains its
    reservation; new boots/cameras cannot overlap that native ownership.
    """

    def __init__(
        self,
        controller,
        *,
        on_ended: Callable[..., Awaitable[None]] = _noop,
        adapter_factory=None,
        audio_adapter_factory=None,
        audio_decoder_factory=None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.controller = controller
        self._on_ended = on_ended
        self._adapter_factory = adapter_factory
        self._audio_adapter_factory = audio_adapter_factory
        self._audio_decoder_factory = audio_decoder_factory
        self._clock = clock
        self._sessions: dict[str, _Session] = {}
        self._stopped: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._reaper: asyncio.Task | None = None
        self._ending: set[asyncio.Task] = set()
        self._closed = False
        self._recent: deque[dict] = deque(maxlen=MAX_SESSIONS)
        self.ready = False
        self.capabilities = None
        self.audio_ready = False
        self.audio_capabilities = None

    @property
    def pending_resources(self):
        return len(self._sessions)

    @property
    def cleanup_failures(self):
        return sum(
            session.failure is not None
            or session.audio is not None
            and session.audio.cleanup_failed
            for session in self._sessions.values()
        )

    def _diagnostics(self, session):
        now = self._clock()
        config = session.config
        video_format = (
            {
                "width": config.width,
                "height": config.height,
                "fps": config.fps,
                "profile": _trace_label(config.profile, {"baseline", "main", "high"}),
                "level": _trace_label(config.level, {"3.1", "3.2", "4.0"}),
                "bitrate_kbps": config.max_bit_rate,
                "mtu": config.mtu,
            }
            if config is not None
            else None
        )
        return {
            "state": _trace_label(
                session.state, {"prepared", "starting", "active", "failed", "stopped"}
            ),
            "retired": session.retired,
            "cleanup_pending": session.failure is not None,
            "elapsed_ms": _trace_age(now, session.prepared_at),
            "active_elapsed_ms": _trace_age(
                session.terminal_at if session.terminal_at is not None else now,
                session.active_started_at,
            ),
            "source_age_ms": _trace_age(now, session.last_source),
            "feedback_age_ms": _trace_age(now, session.last_feedback),
            "video_format": video_format,
            "actual_source_format": _format_diagnostics(session.adapter, "source_format"),
            "actual_output_format": _format_diagnostics(session.adapter, "output_format"),
            "codec_source_time_window": session.codec_rate.snapshot(),
            "local_wire_peak_bps": (
                VideoWirePolicy(config.max_bit_rate).peak_bps if config is not None else None
            ),
            "audio_enabled": session.audio_config is not None,
            "mode": _trace_label(
                getattr(session.adapter, "mode", None), {"waiting", "copy", "adapt"}
            ),
            "timing_mode": _trace_label(
                session.timing_mode, {"receiver_estimated", "source_sender_report"}
            ),
            "timeline": session.timeline.snapshot() if session.timeline is not None else None,
            "terminal": copy.deepcopy(session.terminal),
            "video": session.metrics.snapshot(),
            "receiver_report": session.receiver_stats.snapshot(now),
            "repair_queue": session.repair_queue.snapshot(),
            "audio": safe_diagnostics(session.audio),
        }

    def diagnostics(self):
        # No controller/source reads or ownership locks are needed for status.
        return {
            "active": [
                self._diagnostics(item) for item in tuple(self._sessions.values())[:MAX_SESSIONS]
            ],
            "recent": copy.deepcopy(list(self._recent)),
        }

    async def qualify(self):
        from halo_server.media.transcode import video_capabilities

        self.capabilities = await video_capabilities()
        self.ready = self.capabilities.ready
        try:
            from halo_server.media.homekit_audio_codec import audio_capabilities

            self.audio_capabilities = await audio_capabilities()
            self.audio_ready = self.audio_capabilities.ready
        except Exception:
            self.audio_capabilities = None
            self.audio_ready = False
        return self.capabilities

    def _authorized(self, boot, scope):
        if self._closed:
            raise HomeKitOperationError("closed")
        _uuid(boot)
        if not isinstance(scope, MediaScope):
            raise HomeKitOperationError("invalid_request")
        self.controller.authorize(boot, scope, "streaming")

    def _current(self, session):
        if session.retired or self._sessions.get(session.identity) is not session:
            raise HomeKitOperationError("authority_changed")
        self._authorized(session.boot, session.scope)
        if session.handle is not None and not session.handle.is_current():
            raise HomeKitOperationError("authority_changed")

    def _audio_current(self, session, permission):
        self._current(session)
        if permission is not None:
            self.controller.authorize(session.boot, session.scope, permission)

    def _trace_start(self, session):
        try:
            config, audio = session.config, session.audio_config
            _log.info(
                "HomeKit live start width=%d height=%d fps=%d profile=%s level=%s "
                "bitrate_kbps=%d mtu=%d payload_type=%d audio=%s audio_rate=%s "
                "audio_packet_ms=%s audio_bitrate_kbps=%s",
                config.width,
                config.height,
                config.fps,
                _trace_label(config.profile, {"baseline", "main", "high"}),
                _trace_label(config.level, {"3.1", "3.2", "4.0"}),
                config.max_bit_rate,
                config.mtu,
                config.payload_type,
                audio is not None,
                audio.sample_rate if audio is not None else None,
                audio.packet_time if audio is not None else None,
                audio.max_bit_rate if audio is not None else None,
            )
        except Exception:
            pass  # Diagnostic output must never change admission or retirement.

    def _trace_terminal(self, session, stage, reason, error=None):
        if session.trace_terminal:
            return
        session.trace_terminal = True
        if session.diagnostic_trace is not None:
            _diagnostic(session, "stream_end")
            with contextlib.suppress(BaseException):
                # Preserve the final viewing window even when the owner keeps
                # collecting metadata through a long closed-view cleanup tail.
                # The recorder retains an earlier symptom trigger if one exists.
                session.diagnostic_trace.trigger("stop")
        try:
            now = self._clock()
            session.terminal_at = now
            error_class = (
                _trace_label(type(error).__name__, _TRACE_ERRORS) if error is not None else "none"
            )
            error_code = (
                _trace_label(error.code, _TRACE_ERROR_CODES)
                if isinstance(error, HomeKitOperationError)
                else "none"
            )
            number = getattr(error, "errno", None) if isinstance(error, OSError) else None
            number = number if type(number) is int and 0 <= number <= 4095 else None
            session.terminal = {
                "stage": _trace_label(stage, _TRACE_STAGES),
                "reason": _trace_label(reason, _TRACE_REASONS),
                "error_class": error_class,
                "error_code": error_code,
                "errno": number,
            }
            mode = _trace_label(
                getattr(session.adapter, "mode", None), {"waiting", "copy", "adapt"}
            )
            _log.info(
                "HomeKit live terminal stage=%s reason=%s error_class=%s error_code=%s "
                "errno=%s state=%s mode=%s first_frame=%s video_packets=%d video_octets=%d "
                "audio_packets=%d audio_octets=%d elapsed_ms=%s "
                "source_age_ms=%s feedback_age_ms=%s",
                _trace_label(stage, _TRACE_STAGES),
                _trace_label(reason, _TRACE_REASONS),
                error_class,
                error_code,
                number,
                _trace_label(
                    session.state, {"prepared", "starting", "active", "failed", "stopped"}
                ),
                mode,
                session.trace_first_frame,
                session.packets,
                session.octets,
                session.audio.sent_packets if session.audio is not None else 0,
                session.audio.sent_octets if session.audio is not None else 0,
                _trace_age(now, session.prepared_at),
                _trace_age(now, session.last_source),
                _trace_age(now, session.last_feedback),
            )
        except Exception:
            pass

    def _lookup(self, boot, scope, session_id):
        _uuid(boot)
        _uuid(session_id)
        session = self._sessions.get(session_id)
        if session is None or session.boot != boot or session.scope != scope:
            raise HomeKitOperationError("session_conflict")
        return session

    async def prepare(
        self,
        boot,
        scope,
        session_id,
        *,
        source_address,
        target_address,
        address_version,
        target_port,
        key,
        salt,
        audio: AudioEndpointRequest | None = None,
    ) -> PreparedVideo:
        self._authorized(boot, scope)
        _uuid(session_id)
        if audio is not None and not isinstance(audio, AudioEndpointRequest):
            raise HomeKitOperationError("invalid_request")
        self._prune_stopped()
        if (boot, session_id) in self._stopped:
            raise HomeKitOperationError("session_conflict")
        if session_id not in self._sessions and len(self._stopped) >= MAX_STOP_TOMBSTONES:
            raise HomeKitOperationError("busy")
        if type(target_port) is not int or not 1 <= target_port <= 65535:
            raise HomeKitOperationError("invalid_destination")
        if (
            not isinstance(key, bytes)
            or len(key) != 16
            or not isinstance(salt, bytes)
            or len(salt) != 14
        ):
            raise HomeKitOperationError("invalid_request")
        source, source_zone = _address(source_address, address_version)
        target, target_zone = _address(target_address, address_version)
        if address_version == "ipv6":
            if target.is_link_local and not target_zone:
                target_zone = source_zone
            if source.is_link_local and not source_zone or target.is_link_local and not target_zone:
                raise HomeKitOperationError("invalid_destination")
        endpoint = (
            (str(target).split("%", 1)[0], target_port, 0, target_zone)
            if address_version == "ipv6"
            else (str(target), target_port)
        )
        existing = self._sessions.get(session_id)
        if existing is not None:
            if (
                existing.boot == boot
                and existing.scope == scope
                and not existing.retired
                and existing.source == str(source)
                and existing.endpoint == endpoint
                and existing.prepared.key == key
                and existing.prepared.salt == salt
                and (existing.audio.request if existing.audio is not None else None) == audio
            ):
                return existing.prepared
            raise HomeKitOperationError("session_conflict")
        same_camera = [
            item
            for item in self._sessions.values()
            if (item.scope.profile_id, item.scope.camera_id) == (scope.profile_id, scope.camera_id)
        ]
        if any(item.retired for item in same_camera):
            raise HomeKitOperationError("cleanup_pending")
        if len(self._sessions) >= MAX_SESSIONS or len(same_camera) >= MAX_CAMERA_SESSIONS:
            raise HomeKitOperationError("busy")
        sock = socket.socket(
            socket.AF_INET6 if address_version == "ipv6" else socket.AF_INET, socket.SOCK_DGRAM
        )
        secure = None
        audio_track = None
        try:
            sock.setblocking(False)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 128 * 1024)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
            bind = (
                (str(source).split("%", 1)[0], 0, 0, source_zone)
                if address_version == "ipv6"
                else (str(source), 0)
            )
            sock.bind(bind)
            sock.connect(endpoint)
            self._authorized(boot, scope)
            ssrc = secrets.randbelow(0xFFFFFFFF) + 1
            secure = SecureVideo(key, salt, ssrc)
            prepared = PreparedVideo(sock.getsockname()[1], ssrc, str(source), key, salt)
            session = _Session(
                boot,
                scope,
                session_id,
                prepared,
                endpoint,
                str(source),
                str(target),
                address_version,
                sock,
                secure,
                H264Packetizer(ssrc),
                self._clock(),
            )
            if audio is not None:
                audio_endpoint = list(endpoint)
                audio_endpoint[1] = audio.target_port
                audio_track = AudioTrack(
                    audio,
                    bind=bind,
                    endpoint=tuple(audio_endpoint),
                    family=sock.family,
                    excluded_ssrc=ssrc,
                    check=lambda permission: self._audio_current(session, permission),
                    epoch=lambda: (
                        session.handle.capture_epoch if session.handle is not None else None
                    ),
                    controller=self.controller,
                    boot=boot,
                    scope=scope,
                    failure=lambda code: self._end(session, code),
                    diagnostic_failure=lambda stage, code, error: self._trace_terminal(
                        session, stage, code, error
                    ),
                    touch_feedback=lambda received_at: setattr(
                        session, "last_feedback", received_at
                    ),
                    last_feedback=lambda: session.last_feedback,
                    clock=self._clock,
                    adapter_factory=self._audio_adapter_factory,
                    decoder_factory=self._audio_decoder_factory,
                )
                session.audio = audio_track
                prepared = replace(prepared, audio=audio_track.prepared)
                session.prepared = prepared
            self._authorized(boot, scope)
            self._sessions[session_id] = session
            if self._reaper is None:
                self._reaper = asyncio.create_task(self._expire(), name="homekit-video-expiry")
                self._reaper.add_done_callback(_observed)
            return prepared
        except BaseException:
            sock.close()
            if audio_track is not None:
                audio_track.fence()
                audio_track.secure.close()
            if secure is not None:
                secure.close()
            raise

    async def start(
        self,
        boot,
        scope,
        session_id,
        config,
        *,
        audio_config=None,
        microphone_volume=100,
        speaker_volume=100,
    ):
        session = self._lookup(boot, scope, session_id)
        self._current(session)
        from halo_server.media.h264 import VideoConfiguration

        if not isinstance(config, VideoConfiguration):
            raise HomeKitOperationError("invalid_request")
        if any(
            type(gain) is not int or not 0 <= gain <= 100
            for gain in (microphone_volume, speaker_volume)
        ):
            raise HomeKitOperationError("invalid_request")
        if (session.audio is None) != (audio_config is None):
            raise HomeKitOperationError("invalid_request")
        if audio_config is not None:
            from halo_server.media.opus import AudioConfiguration

            if not isinstance(audio_config, AudioConfiguration):
                raise HomeKitOperationError("invalid_request")
            audio_config = replace(audio_config, gain=microphone_volume)
        if session.state == "active":
            if (session.config, session.audio_config, session.speaker_volume) == (
                config,
                audio_config,
                speaker_volume,
            ):
                return
            raise HomeKitOperationError("session_conflict")
        if session.start_task is None:
            session.config = config
            session.audio_config, session.speaker_volume = audio_config, speaker_volume
            session.state = "starting"
            self._trace_start(session)
            session.start_task = asyncio.create_task(
                self._start(session), name="homekit-video-start"
            )
            session.start_task.add_done_callback(_observed)
        elif (session.config, session.audio_config, session.speaker_volume) != (
            config,
            audio_config,
            speaker_volume,
        ):
            raise HomeKitOperationError("session_conflict")
        try:
            await asyncio.shield(session.start_task)
        except BaseException:
            await self._settle_retirement(session)
            raise

    async def _start(self, session):
        from halo_server.media.transcode import AccessUnitAdapter

        try:
            session.handle = await self.controller.open_live(
                session.boot, session.scope, session_id=session.identity
            )
            self._current(session)
            with contextlib.suppress(BaseException):
                trace = getattr(session.handle, "diagnostic_trace", None)
                if trace is not None:
                    session.diagnostic_stream_id = trace.allocate_stream()
                    session.diagnostic_trace = trace
                    _diagnostic(
                        session,
                        "stream_start",
                        width=session.config.width,
                        height=session.config.height,
                        fps=session.config.fps,
                        bit_rate=session.config.max_bit_rate,
                        clock_rate=90000,
                    )
            factory = self._adapter_factory or AccessUnitAdapter
            session.adapter = factory(session.config)
            session.timeline = LiveTimeline(
                audio_rate=session.audio_config.sample_rate if session.audio_config else None,
                video_origin=session.output_base,
                audio_origin=session.audio.rtp_origin if session.audio is not None else None,
                clock=self._clock,
            )
            if session.audio is not None:
                session.audio.start(
                    session.handle,
                    session.audio_config,
                    speaker_volume=session.speaker_volume,
                    timeline=session.timeline,
                    diagnostic_trace=session.diagnostic_trace,
                    diagnostic_stream_id=session.diagnostic_stream_id,
                )
            session.last_source = session.last_feedback = session.last_renewal = self._clock()
            session.active_started_at = session.last_source
            session.state = "active"
            session.sender = asyncio.create_task(
                self._send_video(session), name="homekit-video-send"
            )
            session.receiver = asyncio.create_task(
                self._receive_feedback(session), name="homekit-video-rtcp"
            )
            session.reports = asyncio.create_task(self._reports(session), name="homekit-video-sr")
            session.repair_worker = asyncio.create_task(
                self._repair_video(session), name="homekit-video-repair"
            )
            for task in (session.sender, session.receiver, session.reports, session.repair_worker):
                task.add_done_callback(_observed)
        except BaseException as exc:
            self._trace_terminal(session, "start", "start_failed", exc)
            session.retired = True
            raise

    async def reconfigure(self, boot, scope, session_id, config):
        from halo_server.media.h264 import VideoConfiguration

        session = self._lookup(boot, scope, session_id)
        self._current(session)
        if session.state != "active" or not isinstance(config, VideoConfiguration):
            raise HomeKitOperationError("not_ready")
        if config.payload_type != session.config.payload_type:
            raise HomeKitOperationError("invalid_request")
        try:
            async with session.adapter_lock:
                self._current(session)
                reset_video = replace(session.config, rtcp_interval=config.rtcp_interval) != config
                await session.adapter.reconfigure(config)
                self._current(session)
                session.config = config
                _measure(session, "count", "reconfigurations")
                # The report loop reads the interval before its next sleep.
                # An RTCP-only change must preserve the current decodable GOP.
                if reset_video:
                    self._reset_video_recovery(session)
        except BaseException:
            await self._settle_retirement(session)
            raise

    async def _send(self, session, packet, *, epoch=None, deadline=None, repair=None):
        retry_deadline = self._clock() + SEND_SECONDS
        if deadline is not None:
            retry_deadline = min(retry_deadline, deadline)
        while True:
            self._current(session)
            if epoch is not None and session.handle.capture_epoch != epoch:
                raise _SourceChanged
            if repair is not None and session.retransmit.get(repair[0]) is not repair[1]:
                raise _SourceChanged
            if deadline is not None and self._clock() > deadline:
                raise _VideoDeadline
            if repair is not None and not session.repair_sends.permits(self._clock(), len(packet)):
                raise _RepairRateLimited
            try:
                if session.socket.send(packet) != len(packet):
                    raise HomeKitOperationError("media_failed")
                if repair is not None:
                    session.repair_sends.record(self._clock(), len(packet))
                return
            except BlockingIOError:
                if self._clock() >= retry_deadline:
                    if deadline is not None and self._clock() >= deadline:
                        raise _VideoDeadline from None
                    raise HomeKitOperationError("media_saturated") from None
                await asyncio.sleep(0.002)

    def _timestamp(self, session, timestamp):
        return session.timeline.rtp_timestamp(
            "video", capture_epoch=session.capture_epoch, source_pts=timestamp
        )

    def _reset_video_recovery(self, session):
        # A codec/configuration reset does not change source media time.
        session.retransmit.clear()
        session.retransmit_bytes = 0
        session.repair_queue.clear()
        self._need_idr(
            session,
            bounded=session.trace_first_frame,
            request=False,
            reset=not session.awaiting_idr,
        )
        session.idr_requested = False

    def _need_idr(self, session, *, bounded=True, request=True, reset=False):
        if reset:
            session.recovery_since = None
            session.recovery_attempts = 0
            session.idr_requested = False
        if bounded and session.recovery_since is None:
            session.recovery_since = self._clock()
        session.awaiting_idr = True
        session.recovery_revision += 1
        session.retransmit.clear()
        session.retransmit_bytes = 0
        session.repair_queue.clear()
        if request and not session.idr_requested and session.adapter is not None:
            session.adapter.request_keyframe()
            session.idr_requested = True
            if getattr(session.adapter, "mode", None) in {"copy", "waiting"}:
                session.upstream_keyframe_pending = True

    async def _request_upstream_keyframe(self, session):
        # Run between completed batches, before the next source read. Never
        # age an already-read fresh AU while waiting for source feedback.
        # Audio may keep the other bounded controller work token.
        if getattr(session.adapter, "mode", None) == "adapt":
            session.upstream_keyframe_pending = False
            return
        callback = getattr(session.handle, "request_keyframe", None)
        if not callable(callback) or not (
            session.upstream_keyframe_pending or session.awaiting_idr
        ):
            return
        now = self._clock()
        if now - session.last_upstream_keyframe < 0.5:
            return
        self._current(session)
        epoch = session.handle.capture_epoch
        session.last_upstream_keyframe = now
        session.upstream_keyframe_pending = False
        _measure(session, "count", "source_keyframe_requests")
        try:
            attempted = await callback(capture_epoch=epoch)
        except HomeKitOperationError as error:
            if error.code != "busy":
                raise
            attempted = False
            session.upstream_keyframe_pending = True
        self._current(session)
        if epoch != session.handle.capture_epoch:
            attempted = False
        _measure(
            session,
            "count",
            "source_keyframe_attempted" if attempted else "source_keyframe_rejected",
        )

    def _check_recovery(self, session):
        if session.recovery_since is not None and (
            session.recovery_attempts >= MAX_RECOVERY_IDRS
            or self._clock() - session.recovery_since >= RECOVERY_SECONDS
        ):
            raise HomeKitOperationError("media_saturated")

    def _stale_access_unit(self, session, output):
        # A stale actual IDR may have consumed the adapter's pending force.
        # Request a fresh one without counting the stale unit as an attempt.
        with contextlib.suppress(RtpError):
            if any(nal[0] & 31 == 5 for nal in annexb_nalus(output.payload)):
                session.idr_requested = False
        self._need_idr(session)
        self._check_recovery(session)

    def _actual_idr(self, session, output):
        nals = annexb_nalus(output.payload)
        if not any(nal[0] & 31 == 5 for nal in nals):
            return False
        sps = next((nal for nal in reversed(nals) if nal[0] & 31 == 7), None)
        pps = next((nal for nal in reversed(nals) if nal[0] & 31 == 8), None)
        if sps is None or pps is None:
            raise RtpError("Video IDR lacks current parameters")
        try:
            info = parse_sps(sps)
            selected = session.config
            valid = (
                pps_sequence_id(pps) == info.sequence_id
                and info.profile == selected.profile
                and info.level_idc <= int(selected.level.replace(".", ""))
                and (info.width, info.height) == (selected.width, selected.height)
                and info.progressive
                and info.chroma_format == 1
                and info.bit_depth == 8
            )
        except H264Error as exc:
            raise RtpError("Video IDR parameters are invalid") from exc
        if not valid:
            raise RtpError("Video IDR parameters do not match selection")
        return True

    def _reject_access_unit(self, session, *, idr, partial=False):
        session.idr_requested = False
        self._need_idr(session)
        if idr:
            session.recovery_attempts += 1
            _measure(session, "count", "recovery_idr_rejected")
        if partial:
            _measure(session, "count", "partial_access_units")
        self._check_recovery(session)
        return False

    def _presentation_time(self, session, kind, timestamp, received_at):
        # Receipt remains an admission/freshness fact. Presentation follows the
        # original source sample clock once observed, not output completion.
        result = session.timeline.presentation(
            kind, capture_epoch=session.handle.capture_epoch, source_pts=timestamp
        )
        session.timing_mode = session.timeline.mode
        return result

    def _observe_source(self, session, packet):
        if getattr(session, "diagnostic_trace", None) is not None:
            _diagnostic(
                session,
                "source_read",
                pts=packet.rtp_timestamp,
                received_us=round(packet.received_at * 1_000_000),
                capture_epoch=packet.capture_epoch,
                size=len(packet.payload),
            )
        with contextlib.suppress(Exception):
            metrics = session.metrics
            metrics.count("source_access_units")
            metrics.count("source_bytes", len(packet.payload))
            metrics.maximum("source_access_unit_bytes", len(packet.payload))
            if session.diagnostic_source_pts is not None:
                delta = packet.rtp_timestamp - session.diagnostic_source_pts
                if delta <= 0:
                    metrics.count("source_nonincreasing_pts")
                else:
                    metrics.observe("source_pts_step", delta / 90000)
            if session.diagnostic_source_receipt is not None:
                metrics.observe(
                    "source_receipt_step", packet.received_at - session.diagnostic_source_receipt
                )
            session.diagnostic_source_pts = packet.rtp_timestamp
            session.diagnostic_source_receipt = packet.received_at

    def _observe_outputs(self, session, outputs):
        with contextlib.suppress(Exception):
            metrics = session.metrics
            for output in outputs:
                if getattr(session, "diagnostic_trace", None) is not None:
                    _diagnostic(
                        session,
                        "encoded_output",
                        payload=output.payload,
                        pts=output.timestamp,
                        capture_epoch=output.capture_epoch,
                        keyframe=output.is_keyframe is True,
                        adapted=output.mode == "adapt",
                        received_us=round(output.received_at * 1_000_000),
                    )
                metrics.count("output_access_units")
                metrics.count("output_keyframes", int(output.is_keyframe is True))
                if output.mode in {"copy", "adapt"}:
                    metrics.count("copy_units" if output.mode == "copy" else "adapted_units")
                metrics.maximum("output_access_unit_bytes", len(output.payload))
                if session.diagnostic_output_pts is not None:
                    delta = output.timestamp - session.diagnostic_output_pts
                    if delta <= 0:
                        metrics.count("output_nonincreasing_pts")
                    else:
                        metrics.observe("output_pts_step", delta / 90000)
                session.diagnostic_output_pts = output.timestamp
                with contextlib.suppress(Exception):
                    session.codec_rate.observe(
                        epoch=output.capture_epoch,
                        generation=output.configuration_generation,
                        selected_kbps=session.config.max_bit_rate,
                        source_pts=output.timestamp,
                        encoded_bytes=len(output.payload),
                    )

    @staticmethod
    def _configuration_generation(session):
        generation = getattr(session.adapter, "configuration_generation", None)
        if type(generation) is not int or generation < 1:
            raise HomeKitOperationError("media_failed")
        return generation

    def _output_receipts(self, session, outputs, *, epoch, generation):
        # A newer input may release an older decoded picture. Validate the
        # whole bounded batch before any packet admission; never substitute
        # the triggering input's receipt for missing output provenance.
        if self._configuration_generation(session) != generation:
            raise HomeKitOperationError("media_failed")
        receipts = []
        for output in outputs:
            receipt = getattr(output, "received_at", None)
            if (
                type(getattr(output, "capture_epoch", None)) is not int
                or output.capture_epoch != epoch
                or type(getattr(output, "configuration_generation", None)) is not int
                or output.configuration_generation != generation
                or type(receipt) not in {int, float}
                or not math.isfinite(receipt)
                or receipt < 0
                or receipt > self._clock()
            ):
                raise HomeKitOperationError("media_failed")
            receipts.append(receipt)
        return tuple(receipts)

    async def _send_video(self, session):
        try:
            while not session.retired:
                self._current(session)
                self._check_recovery(session)
                await self._request_upstream_keyframe(session)
                read_started = self._clock()
                batch = await session.handle.read(timeout=0.25)
                now = self._clock()
                _measure(session, "observe", "source_read", max(0, now - read_started))
                _measure(session, "maximum", "batch_packets", len(batch.packets))
                if now - session.last_renewal >= 5:
                    if not await session.handle.renew():
                        raise HomeKitOperationError("authority_changed")
                    session.last_renewal = now
                if now - session.last_source > SOURCE_SECONDS:
                    raise HomeKitOperationError("source_timeout")
                if session.capture_epoch != batch.capture_epoch:
                    if session.capture_epoch is not None:
                        _measure(session, "count", "epoch_changes")
                    session.diagnostic_source_pts = session.diagnostic_output_pts = None
                    session.diagnostic_source_receipt = session.diagnostic_keyframe_pts = None
                    self._reset_video_recovery(session)
                    session.capture_epoch = batch.capture_epoch
                elif batch.resynchronized:
                    self._need_idr(session)
                self._check_recovery(session)
                for packet in batch.packets:
                    if packet.stream is not MediaStreamKind.VIDEO:
                        continue
                    self._observe_source(session, packet)
                    session.last_source = max(session.last_source, packet.received_at)
                    async with session.adapter_lock:
                        self._current(session)
                        if session.handle.capture_epoch != packet.capture_epoch:
                            break
                        session.timeline.observe_input(
                            "video",
                            capture_epoch=packet.capture_epoch,
                            source_pts=packet.rtp_timestamp,
                            received_at=packet.received_at,
                            source_clocks=tuple(getattr(session.handle, "source_clocks", ())),
                        )
                        generation = self._configuration_generation(session)
                        feed_started = self._clock()
                        _measure(session, "count", "adapter_feeds")
                        if session.diagnostic_trace is not None:
                            _diagnostic(
                                session,
                                "conversion_start",
                                pts=packet.rtp_timestamp,
                                capture_epoch=packet.capture_epoch,
                            )
                        outputs = await session.adapter.feed(
                            packet.payload,
                            packet.rtp_timestamp,
                            capture_epoch=packet.capture_epoch,
                            received_at=packet.received_at,
                        )
                        if session.diagnostic_trace is not None:
                            _diagnostic(
                                session,
                                "conversion_end",
                                pts=packet.rtp_timestamp,
                                capture_epoch=packet.capture_epoch,
                                output_count=len(outputs),
                                elapsed_us=round((self._clock() - feed_started) * 1_000_000),
                            )
                        _measure(
                            session, "observe", "adapter_feed", max(0, self._clock() - feed_started)
                        )
                        if session.handle.capture_epoch != packet.capture_epoch:
                            break
                        receipts = self._output_receipts(
                            session, outputs, epoch=packet.capture_epoch, generation=generation
                        )
                        self._observe_outputs(session, outputs)
                        for output, receipt in zip(outputs, receipts, strict=True):
                            _measure(
                                session,
                                "observe",
                                "source_age",
                                max(0, self._clock() - receipt),
                            )
                            if self._clock() - receipt > MAX_FRAME_AGE:
                                _measure(session, "count", "stale_output_units")
                                self._stale_access_unit(session, output)
                                continue
                            try:
                                sent = await self._access_unit(
                                    session,
                                    output,
                                    packet.capture_epoch,
                                    received_at=receipt,
                                )
                                if not sent and output.mode == "copy":
                                    session.adapter.force_adaptation()
                                    if self._actual_idr(session, output):
                                        feed_started = self._clock()
                                        _measure(session, "count", "adapter_feeds")
                                        if session.diagnostic_trace is not None:
                                            _diagnostic(
                                                session,
                                                "conversion_start",
                                                pts=packet.rtp_timestamp,
                                                capture_epoch=packet.capture_epoch,
                                                adapted=True,
                                            )
                                        adapted = await session.adapter.feed(
                                            packet.payload,
                                            packet.rtp_timestamp,
                                            capture_epoch=packet.capture_epoch,
                                            received_at=packet.received_at,
                                        )
                                        if session.diagnostic_trace is not None:
                                            _diagnostic(
                                                session,
                                                "conversion_end",
                                                pts=packet.rtp_timestamp,
                                                capture_epoch=packet.capture_epoch,
                                                adapted=True,
                                                output_count=len(adapted),
                                                elapsed_us=round(
                                                    (self._clock() - feed_started) * 1_000_000
                                                ),
                                            )
                                        _measure(
                                            session,
                                            "observe",
                                            "adapter_feed",
                                            max(0, self._clock() - feed_started),
                                        )
                                        adapted_receipts = self._output_receipts(
                                            session,
                                            adapted,
                                            epoch=packet.capture_epoch,
                                            generation=generation,
                                        )
                                        self._observe_outputs(session, adapted)
                                        for unit, unit_receipt in zip(
                                            adapted, adapted_receipts, strict=True
                                        ):
                                            await self._access_unit(
                                                session,
                                                unit,
                                                packet.capture_epoch,
                                                received_at=unit_receipt,
                                            )
                            except _SourceChanged:
                                break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._end(session, "media_failed", stage="video_send", error=exc)

    async def _access_unit(self, session, output, epoch, *, received_at=None):
        send_started = self._clock()
        if len(output.payload) > MAX_ACCESS_UNIT_BYTES:
            raise HomeKitOperationError("media_saturated")
        receipt = send_started if received_at is None else received_at
        deadline = min(receipt + MAX_FRAME_AGE, send_started + MAX_ACCESS_UNIT_SECONDS)
        if self._clock() > deadline:
            _measure(session, "count", "access_unit_deadline_rejected")
            self._stale_access_unit(session, output)
            return False
        idr = self._actual_idr(session, output)
        if session.awaiting_idr and not idr:
            _measure(session, "count", "dependency_skips")
            if not session.idr_requested:
                self._need_idr(session, bounded=session.recovery_since is not None)
            self._check_recovery(session)
            return False
        # Reserve the byte clock for this complete AU. Repairs cannot steal
        # completion slots between its packets; authority/deadline stay live.
        async with session.send_lock:
            self._current(session)
            if session.handle.capture_epoch != epoch:
                self._need_idr(session)
                raise _SourceChanged
            if self._clock() > deadline:
                _measure(session, "count", "access_unit_deadline_rejected")
                self._stale_access_unit(session, output)
                return False
            config = session.config
            sequence = session.packetizer.sequence
            try:
                datagrams = session.packetizer.packetize(
                    output.payload, 0, payload_type=config.payload_type, mtu=config.mtu
                )
            finally:
                session.packetizer.sequence = sequence
            try:
                reservation = VideoWirePolicy(config.max_bit_rate).reserve(
                    tuple(len(item.packet) + 10 for item in datagrams),
                    now=self._clock(),
                    next_packet_at=session.next_packet_at,
                    deadline=deadline,
                )
            except VideoBudgetError:
                reservation = None
            if reservation is None:
                _measure(session, "count", "access_unit_budget_rejected")
                return self._reject_access_unit(session, idr=idr)
            timestamp = self._timestamp(session, output.timestamp)
            datagrams = session.packetizer.packetize(
                output.payload, timestamp, payload_type=config.payload_type, mtu=config.mtu
            )
            presentation = self._presentation_time(session, "video", output.timestamp, receipt)
            if session.diagnostic_trace is not None:
                _diagnostic(
                    session,
                    "timeline",
                    pts=output.timestamp,
                    rtp_timestamp=timestamp,
                    presentation_us=round(presentation * 1_000_000),
                    capture_epoch=epoch,
                    clock_rate=90000,
                )
            revision, before = session.recovery_revision, session.packets
            try:
                for item in datagrams:
                    self._current(session)
                    if session.diagnostic_trace is not None:
                        _diagnostic(
                            session,
                            "rtp_send",
                            sequence=item.sequence,
                            rtp_timestamp=item.timestamp,
                            size=item.payload_bytes,
                            payload_type=config.payload_type,
                            marker=bool(item.packet[1] & 0x80),
                            capture_epoch=epoch,
                            pts=output.timestamp,
                        )
                    protected = session.secure.protect(item.packet)
                    await self._paced_send(
                        session, protected, epoch=epoch, deadline=deadline, reserved=True
                    )
                    session.timeline.sent("video", capture_epoch=epoch, source_pts=output.timestamp)
                    session.last_timestamp = item.timestamp
                    session.last_packet_at = presentation
                    session.packets += 1
                    session.octets += item.payload_bytes
                    if session.diagnostic_trace is not None:
                        _diagnostic(
                            session,
                            "rtp_sent",
                            sequence=item.sequence,
                            rtp_timestamp=item.timestamp,
                            size=item.payload_bytes,
                            actual_us=round(self._clock() * 1_000_000),
                            capture_epoch=epoch,
                        )
                    _measure(session, "count", "sent_packets")
                    _measure(session, "count", "sent_payload_bytes", item.payload_bytes)
                    _measure(session, "count", "sent_protected_bytes", len(protected))
                    if revision == session.recovery_revision:
                        previous = session.retransmit.pop(item.sequence, None)
                        if previous is not None:
                            session.retransmit_bytes -= len(previous[1])
                        sent_at = self._clock()
                        session.retransmit[item.sequence] = (
                            sent_at,
                            protected,
                            item.payload_bytes,
                            epoch,
                            sent_at + RETRANSMIT_SECONDS,
                        )
                        session.retransmit_bytes += len(protected)
                        self._prune_retransmit(session)
            except _VideoDeadline:
                _measure(session, "count", "access_unit_deadline_rejected")
                return self._reject_access_unit(session, idr=idr, partial=session.packets > before)
            except HomeKitOperationError as error:
                if error.code != "media_saturated":
                    raise
                return self._reject_access_unit(session, idr=idr, partial=session.packets > before)
            except _SourceChanged:
                self._need_idr(session)
                raise
            if idr and revision == session.recovery_revision:
                session.awaiting_idr = session.idr_requested = False
                session.recovery_since = None
                session.recovery_attempts = 0
        _measure(session, "count", "sent_access_units")
        _measure(session, "observe", "access_unit_send", max(0, self._clock() - send_started))
        if idr:
            _measure(session, "count", "sent_keyframes")
            if session.diagnostic_keyframe_pts is not None:
                _measure(
                    session,
                    "observe",
                    "sent_keyframe_interval",
                    (output.timestamp - session.diagnostic_keyframe_pts) / 90000,
                )
            session.diagnostic_keyframe_pts = output.timestamp
        if not session.trace_first_frame:
            session.trace_first_frame = True
            with contextlib.suppress(Exception):
                _log.info(
                    "HomeKit live first_frame mode=%s keyframe=%s bytes=%d video_packets=%d "
                    "elapsed_ms=%s source_age_ms=%s",
                    _trace_label(output.mode, {"copy", "adapt"}),
                    idr,
                    len(output.payload),
                    session.packets,
                    _trace_age(self._clock(), session.prepared_at),
                    _trace_age(self._clock(), received_at),
                )
        return True

    async def _paced_send(
        self, session, protected, *, epoch, deadline=None, reserved=False, repair=None
    ):
        if reserved:
            return await self._paced_send_owned(
                session, protected, epoch=epoch, deadline=deadline, repair=repair
            )
        async with session.send_lock:
            return await self._paced_send_owned(
                session, protected, epoch=epoch, deadline=deadline, repair=repair
            )

    async def _paced_send_owned(self, session, protected, *, epoch, deadline, repair):
        now = self._clock()
        target = max(now, session.next_packet_at)
        limit = deadline if deadline is not None else now + MAX_ACCESS_UNIT_SECONDS
        service = VideoWirePolicy(session.config.max_bit_rate).service(len(protected))
        admission_deadline = limit - service
        if target > admission_deadline:
            raise _VideoDeadline
        if getattr(session, "diagnostic_trace", None) is not None:
            _diagnostic(
                session,
                "packet_schedule",
                scheduled_us=round(target * 1_000_000),
                size=len(protected),
                repair=repair is not None,
                capture_epoch=epoch,
            )
        if target > now:
            _measure(session, "observe", "pacer_wait", target - now)
            await asyncio.sleep(target - now)
        _measure(session, "observe", "pacer_late", max(0, self._clock() - target))
        await self._send(
            session, protected, epoch=epoch, deadline=admission_deadline, repair=repair
        )
        session.next_packet_at = self._clock() + service
        if getattr(session, "diagnostic_trace", None) is not None:
            _diagnostic(
                session,
                "packet_sent",
                scheduled_us=round(target * 1_000_000),
                actual_us=round(self._clock() * 1_000_000),
                size=len(protected),
                repair=repair is not None,
                capture_epoch=epoch,
            )

    def _prune_retransmit(self, session):
        cutoff = self._clock() - RETRANSMIT_SECONDS
        while session.retransmit:
            first = next(iter(session.retransmit.values()))
            if (
                first[0] >= cutoff
                and first[4] >= self._clock()
                and len(session.retransmit) <= MAX_RETRANSMIT_PACKETS
                and session.retransmit_bytes <= MAX_RETRANSMIT_BYTES
            ):
                break
            _, item = session.retransmit.popitem(last=False)
            session.retransmit_bytes -= len(item[1])

    async def _receive_feedback(self, session):
        try:
            loop = asyncio.get_running_loop()
            window, received = self._clock(), 0
            while not session.retired:
                protected = await loop.sock_recv(session.socket, 16 * 1024 + 65)
                self._current(session)
                _measure(session, "count", "feedback_datagrams")
                if self._clock() - window >= 1:
                    window, received = self._clock(), 0
                received += 1
                if received > 128:
                    _measure(session, "count", "feedback_rate_limited")
                    await asyncio.sleep(0.01)
                    continue
                try:
                    feedback = parse_feedback(
                        session.secure.unprotect_rtcp(protected), session.prepared.ssrc
                    )
                except (SrtpError, RtpError):
                    _measure(session, "count", "feedback_rejected")
                    continue
                now = self._clock()
                session.last_feedback = now
                if session.diagnostic_trace is not None:
                    for report in feedback.receiver_reports:
                        _diagnostic(
                            session,
                            "receiver_report",
                            fraction_lost=report.fraction_lost,
                            lost=report.packets_lost,
                            highest_sequence=report.highest_sequence,
                            jitter=report.jitter,
                            lsr=report.lsr,
                            dlsr=report.dlsr,
                            clock_rate=90000,
                        )
                _measure(session, "count", "receiver_reports", feedback.reports)
                _measure(session, "count", "nack_requests", len(feedback.nack))
                _measure(session, "count", "pli_requests", int(feedback.pli))
                with contextlib.suppress(Exception):
                    session.receiver_stats.observe(feedback.receiver_reports, now)
                if feedback.bye:
                    self._end(session, "receiver_closed", stage="video_feedback")
                    return
                if feedback.pli and now - session.last_pli >= 0.5:
                    _measure(session, "count", "pli_applied")
                    session.last_pli = now
                    session.adapter.request_keyframe()
                    if getattr(session.adapter, "mode", None) in {"copy", "waiting"}:
                        session.upstream_keyframe_pending = True
                if now - session.last_nack_window >= 1:
                    session.last_nack_window, session.nack_count = now, 0
                self._prune_retransmit(session)
                for index, sequence in enumerate(feedback.nack):
                    cached = session.retransmit.get(sequence)
                    if cached is None or cached[3] != session.handle.capture_epoch:
                        _measure(session, "count", "nack_cache_misses")
                        continue
                    if not session.repair_requests.permits(now, len(cached[1])):
                        _measure(session, "count", "nack_rate_limited", len(feedback.nack) - index)
                        break
                    queued = session.repair_queue.enqueue(sequence, cached)
                    if queued == "queued":
                        session.repair_requests.record(now, len(cached[1]))
                        session.nack_count += 1
                    elif queued == "duplicate":
                        _measure(session, "count", "repair_duplicate_requests")
                    elif queued == "full":
                        _measure(session, "count", "repair_queue_full")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._end(session, "feedback_failed", stage="video_feedback", error=exc)

    async def _repair_video(self, session):
        item = None
        try:
            while not session.retired:
                item = await session.repair_queue.take()
                if item is None:
                    return
                sequence, cached = item
                try:
                    self._current(session)
                    self._prune_retransmit(session)
                    if session.retransmit.get(sequence) is not cached:
                        continue
                    async with asyncio.timeout(
                        max(0, min(RETRANSMIT_SECONDS, cached[4] - self._clock()))
                    ):
                        await self._paced_send(
                            session,
                            cached[1],
                            epoch=cached[3],
                            deadline=cached[4],
                            repair=(sequence, cached),
                        )
                    session.packets += 1
                    session.octets += cached[2]
                    _measure(session, "count", "retransmit_packets")
                    _measure(session, "count", "retransmit_payload_bytes", cached[2])
                    _measure(session, "count", "retransmit_protected_bytes", len(cached[1]))
                except (_SourceChanged, _VideoDeadline, TimeoutError):
                    _measure(session, "count", "repair_deadline_rejected")
                except _RepairRateLimited:
                    _measure(session, "count", "repair_send_rate_limited")
                except HomeKitOperationError as exc:
                    if exc.code != "media_saturated":
                        raise
                    _measure(session, "count", "repair_deadline_rejected")
                finally:
                    session.repair_queue.finish(item)
                    item = cached = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._end(session, "feedback_failed", stage="video_feedback", error=exc)
        finally:
            # Clear even cancellation tracebacks after the actual worker joins.
            item = None

    async def _reports(self, session):
        try:
            while not session.retired:
                await asyncio.sleep(session.config.rtcp_interval)
                self._current(session)
                if self._clock() - session.last_feedback > RECEIVER_SECONDS:
                    raise HomeKitOperationError("receiver_timeout")
                if session.last_timestamp is not None:
                    if session.capture_epoch != session.handle.capture_epoch:
                        continue
                    if session.timeline.capture_epoch != session.capture_epoch:
                        continue
                    instant = session.timeline.instant()
                    pair = session.timeline.report(
                        "video", capture_epoch=session.capture_epoch, instant=instant
                    )
                    if pair is None:
                        continue
                    report = sender_report(
                        session.prepared.ssrc,
                        pair.rtp_timestamp,
                        session.packets,
                        session.octets,
                        now=pair.wall_time,
                        cname=session.timeline.cname,
                    )
                    await self._send(session, session.secure.protect_rtcp(report))
                    if session.diagnostic_trace is not None:
                        _diagnostic(
                            session,
                            "sender_report",
                            rtp_timestamp=pair.rtp_timestamp,
                            wall_us=round(pair.wall_time * 1_000_000),
                            actual_us=round(self._clock() * 1_000_000),
                            capture_epoch=session.capture_epoch,
                            clock_rate=90000,
                        )
                    _measure(session, "count", "sender_reports")
                    with contextlib.suppress(Exception):
                        session.receiver_stats.sent(report, self._clock())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._end(session, "receiver_timeout", stage="video_reports", error=exc)

    def _end(self, session, code, *, stage="lifecycle", error=None):
        if session.retired:
            return
        self._trace_terminal(session, stage, code, error)
        session.retired = True
        self._close_socket(session)
        task = asyncio.create_task(self._ended(session, code), name="homekit-video-ended")
        self._ending.add(task)
        task.add_done_callback(self._ending.discard)
        task.add_done_callback(_observed)

    def _close_socket(self, session):
        session.repair_queue.fence()
        if session.audio is not None:
            session.audio.fence()
        descriptor = session.socket.fileno()
        if descriptor >= 0:
            loop = asyncio.get_running_loop()
            # Retire selector registrations before closing/reusing an FD. The
            # old recv future's cancellation must not remove a new socket's reader.
            loop.remove_reader(descriptor)
            loop.remove_writer(descriptor)
            session.socket.close()

    async def _ended(self, session, code):
        try:
            await self._settle_retirement(session)
        finally:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(CLEANUP_SECONDS):
                    await self._on_ended(session.boot, session.scope, session.identity, code)

    async def _expire(self):
        try:
            while self._sessions:
                expected = self._clock() + 0.25
                await asyncio.sleep(0.25)
                lag = max(0, self._clock() - expected)
                for session in tuple(self._sessions.values()):
                    if session.state == "active" and not session.retired:
                        _measure(session, "observe", "loop_lag", lag)
                await self.refresh()
        except asyncio.CancelledError:
            raise
        finally:
            self._reaper = None

    async def refresh(self):
        for session in tuple(self._sessions.values()):
            if session.retired:
                continue
            try:
                self._current(session)
            except HomeKitOperationError:
                self._end(session, "authority_changed")
            else:
                if session.state == "active" and session.recovery_since is not None:
                    try:
                        self._check_recovery(session)
                    except HomeKitOperationError as error:
                        self._end(session, "media_failed", stage="video_send", error=error)
                        continue
                if (
                    session.state == "prepared"
                    and self._clock() - session.prepared_at >= PREPARE_SECONDS
                ):
                    self._end(session, "prepare_expired")

    async def stop(self, boot, scope, session_id):
        _uuid(boot)
        _uuid(session_id)
        session = self._lookup(boot, scope, session_id) if session_id in self._sessions else None
        self._prune_stopped()
        if (boot, session_id) not in self._stopped:
            if session is None and len(self._stopped) >= MAX_STOP_TOMBSTONES:
                raise HomeKitOperationError("busy")
            self._stopped[boot, session_id] = self._clock() + STOP_TOMBSTONE_SECONDS
        if session is None:
            return
        self._trace_terminal(session, "stop", "stop_request")
        await self._settle_retirement(session)

    def _prune_stopped(self):
        for key, expires in tuple(self._stopped.items()):
            if expires <= self._clock():
                del self._stopped[key]

    async def _settle_retirement(self, session):
        self._trace_terminal(session, "lifecycle", "retirement")
        session.retired = True
        self._close_socket(session)
        if session.cleanup is None or session.cleanup.done() and session.failure is not None:
            session.cleanup = asyncio.create_task(
                self._cleanup(session), name="homekit-video-cleanup"
            )
            session.cleanup.add_done_callback(_observed)
        waiter = asyncio.create_task(self._wait_cleanup(session), name="homekit-video-cleanup-wait")
        waiter.add_done_callback(_observed)
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await settle_owned(waiter)
            raise

    async def _wait_cleanup(self, session):
        _, pending = await asyncio.wait({session.cleanup}, timeout=CLEANUP_SECONDS)
        if pending:
            session.failure = "cleanup_pending"
            session.state = "failed"
            raise HomeKitOperationError("cleanup_pending")
        await session.cleanup

    async def _cleanup(self, session):
        tasks = tuple(
            task
            for task in (
                session.start_task,
                session.sender,
                session.receiver,
                session.reports,
                session.repair_worker,
            )
            if task is not None and task is not asyncio.current_task()
        )
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=CLEANUP_SECONDS)
                if pending:
                    raise HomeKitOperationError("cleanup_pending")
            if session.audio is not None:
                await session.audio.close()
            if session.adapter is not None:
                async with session.adapter_lock:
                    await session.adapter.close()
            if session.handle is not None:
                await session.handle.close()
        except BaseException:
            session.failure = "cleanup_pending"
            session.state = "failed"
            raise
        session.secure.close()
        session.retransmit.clear()
        session.retransmit_bytes = 0
        session.repair_queue.fence()
        session.failure = None
        session.state = "stopped"
        if self._sessions.get(session.identity) is session:
            del self._sessions[session.identity]
            # Audio's final duration ledger is complete only after close joined
            # its owned sender. Retained cleanup failures remain active instead.
            with contextlib.suppress(Exception):
                self._recent.append(self._diagnostics(session))

    async def retired(self, boot):
        sessions = [item for item in self._sessions.values() if item.boot == boot]
        # Revoke every socket before waiting for one potentially slow worker.
        for session in sessions:
            self._trace_terminal(session, "lifecycle", "boot_retired")
            session.retired = True
            self._close_socket(session)
        failures = []
        for session in sessions:
            try:
                await self._settle_retirement(session)
            except Exception as exc:
                failures.append(exc)
        endings = tuple(task for task in self._ending if task is not asyncio.current_task())
        if endings:
            _, pending = await asyncio.wait(endings, timeout=CLEANUP_SECONDS)
            if pending:
                failures.append(HomeKitOperationError("cleanup_pending"))
        if failures:
            raise ExceptionGroup("HomeKit video retirement incomplete", failures)

    async def close(self):
        self._closed = True
        for boot in {item.boot for item in self._sessions.values()}:
            await self.retired(boot)
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
            self._reaper = None


def _observed(task):
    if not task.cancelled():
        task.exception()
