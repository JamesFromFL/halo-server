"""One scoped native UDP output owner over Halo's existing capture handles."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import math
import secrets
import socket
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from uuid import UUID

from halo_server.homekit.models import HomeKitOperationError, MediaScope
from halo_server.media.buffer import MediaStreamKind
from halo_server.media.homekit_audio import AudioEndpointRequest, AudioTrack, PreparedAudio
from halo_server.media.manager import settle_owned
from halo_server.media.rtp import (
    MAX_ACCESS_UNIT_BYTES,
    H264Packetizer,
    RtpError,
    parse_feedback,
    sender_report,
)
from halo_server.media.srtp import SecureVideo, SrtpError

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


class _SourceChanged(RuntimeError):
    """An inner capture attempt retired while encoded output was being prepared."""


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
    cleanup: asyncio.Task | None = None
    failure: str | None = None
    adapter_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_source: float = 0
    last_feedback: float = 0
    last_renewal: float = 0
    source_base: int | None = None
    source_last: int | None = None
    output_base: int = field(default_factory=lambda: secrets.randbits(32))
    last_timestamp: int | None = None
    last_packet_at: float = 0
    discarded_stale: bool = False
    capture_epoch: int | None = None
    next_packet_at: float = 0
    packets: int = 0
    octets: int = 0
    retransmit: OrderedDict = field(default_factory=OrderedDict)
    retransmit_bytes: int = 0
    last_nack_window: float = 0
    nack_count: int = 0
    last_pli: float = 0
    clock_anchor: tuple | None = None
    timing_mode: str = "receiver_estimated"
    trace_first_frame: bool = False
    trace_terminal: bool = False


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
        try:
            now = self._clock()
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
                    presentation=lambda kind, timestamp, received_at: self._presentation_time(
                        session, kind, timestamp, received_at
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
            factory = self._adapter_factory or AccessUnitAdapter
            session.adapter = factory(session.config)
            if session.audio is not None:
                session.audio.start(
                    session.handle, session.audio_config, speaker_volume=session.speaker_volume
                )
            session.last_source = session.last_feedback = session.last_renewal = self._clock()
            session.state = "active"
            session.sender = asyncio.create_task(
                self._send_video(session), name="homekit-video-send"
            )
            session.receiver = asyncio.create_task(
                self._receive_feedback(session), name="homekit-video-rtcp"
            )
            session.reports = asyncio.create_task(self._reports(session), name="homekit-video-sr")
            for task in (session.sender, session.receiver, session.reports):
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
                await session.adapter.reconfigure(config)
                self._current(session)
                session.config = config
                self._reset_timestamp(session)
        except BaseException:
            await self._settle_retirement(session)
            raise

    async def _send(self, session, packet, *, epoch=None):
        deadline = self._clock() + SEND_SECONDS
        while True:
            self._current(session)
            if epoch is not None and session.handle.capture_epoch != epoch:
                raise _SourceChanged
            try:
                if session.socket.send(packet) != len(packet):
                    raise HomeKitOperationError("media_failed")
                return
            except BlockingIOError:
                if self._clock() >= deadline:
                    raise HomeKitOperationError("media_saturated") from None
                await asyncio.sleep(0.002)

    def _timestamp(self, session, timestamp):
        if session.source_base is None:
            session.source_base = timestamp
        elif session.source_last is not None and timestamp < session.source_last:
            session.source_base = timestamp
            session.output_base = (
                (session.last_timestamp or session.output_base) + 3000
            ) & 0xFFFFFFFF
        result = (session.output_base + timestamp - session.source_base) & 0xFFFFFFFF
        session.source_last = timestamp
        return result

    def _reset_timestamp(self, session):
        session.source_base = session.source_last = None
        if session.last_timestamp is not None:
            step = 90000 // session.config.fps
            session.output_base = (session.last_timestamp + step) & 0xFFFFFFFF
        session.retransmit.clear()
        session.retransmit_bytes = 0

    def _presentation_time(self, session, kind, timestamp, received_at):
        """Use one source SR anchor for both tracks, with an honest fallback.

        The observer's RTP domain is the same extended domain as the encoded
        hook. An incoming SR never changes the output RTP sequence or origin.
        """
        epoch = session.handle.capture_epoch
        now = self._clock()
        observations = tuple(getattr(session.handle, "source_clocks", ()))
        valid = [
            item
            for item in observations
            if (
                item.capture_epoch == epoch
                and item.kind in {"audio", "video"}
                and type(item.sender_ntp) is int
                and 0 < item.sender_ntp < 2**64
                and 0 <= now - item.received_at <= 60
            )
        ]
        observation = next((item for item in valid if item.kind == kind), None)
        if observation is not None:
            rate = 48000 if kind == "audio" else 90000
            delta = (timestamp - observation.rtp_timestamp) / rate
            if abs(delta) <= 60:
                if session.clock_anchor is None or session.clock_anchor[0] != epoch:
                    first = min(valid, key=lambda item: item.received_at)
                    session.clock_anchor = epoch, first.sender_ntp, first.received_at
                _, anchor_ntp, anchor_time = session.clock_anchor
                ntp_delta = (observation.sender_ntp - anchor_ntp) / 2**32
                presentation = anchor_time + ntp_delta + delta
                if abs(presentation - received_at) <= 5:
                    session.timing_mode = "source_sender_report"
                    return presentation
        session.timing_mode = "receiver_estimated"
        return received_at

    async def _send_video(self, session):
        try:
            while not session.retired:
                self._current(session)
                batch = await session.handle.read(timeout=0.25)
                now = self._clock()
                if now - session.last_renewal >= 5:
                    if not await session.handle.renew():
                        raise HomeKitOperationError("authority_changed")
                    session.last_renewal = now
                if now - session.last_source > SOURCE_SECONDS:
                    raise HomeKitOperationError("source_timeout")
                if session.capture_epoch != batch.capture_epoch:
                    self._reset_timestamp(session)
                    session.capture_epoch = batch.capture_epoch
                for packet in batch.packets:
                    if packet.stream is not MediaStreamKind.VIDEO:
                        continue
                    session.last_source = max(session.last_source, packet.received_at)
                    async with session.adapter_lock:
                        self._current(session)
                        outputs = await session.adapter.feed(
                            packet.payload,
                            packet.rtp_timestamp,
                            capture_epoch=getattr(
                                packet, "capture_epoch", getattr(batch, "capture_epoch", 0)
                            ),
                        )
                        if session.handle.capture_epoch != packet.capture_epoch:
                            break
                        for output in outputs:
                            if self._clock() - packet.received_at > MAX_FRAME_AGE:
                                session.discarded_stale = True
                                if output.mode == "copy":
                                    session.adapter.request_keyframe()
                                continue
                            if session.discarded_stale:
                                session.discarded_stale = False
                                if not output.is_keyframe:
                                    session.adapter.request_keyframe()
                                    continue
                            try:
                                sent = await self._access_unit(
                                    session,
                                    output,
                                    packet.capture_epoch,
                                    received_at=packet.received_at,
                                )
                                if not sent and output.mode == "copy":
                                    session.adapter.force_adaptation()
                                    if output.is_keyframe:
                                        adapted = await session.adapter.feed(
                                            packet.payload,
                                            packet.rtp_timestamp,
                                            capture_epoch=packet.capture_epoch,
                                        )
                                        for unit in adapted:
                                            if not await self._access_unit(
                                                session,
                                                unit,
                                                packet.capture_epoch,
                                                received_at=packet.received_at,
                                            ):
                                                raise HomeKitOperationError("media_saturated")
                                elif not sent:
                                    raise HomeKitOperationError("media_saturated")
                            except _SourceChanged:
                                break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._end(session, "media_failed", stage="video_send", error=exc)

    async def _access_unit(self, session, output, epoch, *, received_at=None):
        if len(output.payload) > MAX_ACCESS_UNIT_BYTES:
            raise HomeKitOperationError("media_saturated")
        config = session.config
        sequence = session.packetizer.sequence
        datagrams = session.packetizer.packetize(
            output.payload, 0, payload_type=config.payload_type, mtu=config.mtu
        )
        # A full access unit is retained and paced. Native/UDP queues never
        # accumulate an unbounded series of partially transmitted reference frames.
        started = max(self._clock(), session.next_packet_at)
        rate = config.max_bit_rate * 1000
        duration = sum(len(item.packet) + 10 for item in datagrams) * 8 / rate
        session.packetizer.sequence = sequence
        if started + duration > self._clock() + MAX_ACCESS_UNIT_SECONDS:
            return False
        timestamp = self._timestamp(session, output.timestamp)
        datagrams = session.packetizer.packetize(
            output.payload, timestamp, payload_type=config.payload_type, mtu=config.mtu
        )
        presentation = self._presentation_time(
            session,
            "video",
            output.timestamp,
            self._clock() if received_at is None else received_at,
        )
        for item in datagrams:
            self._current(session)
            protected = session.secure.protect(item.packet)
            await self._paced_send(
                session, protected, epoch=epoch, deadline=started + MAX_ACCESS_UNIT_SECONDS
            )
            session.last_timestamp = item.timestamp
            session.last_packet_at = presentation
            session.packets += 1
            session.octets += item.payload_bytes
            previous = session.retransmit.pop(item.sequence, None)
            if previous is not None:
                session.retransmit_bytes -= len(previous[1])
            session.retransmit[item.sequence] = (
                self._clock(),
                protected,
                item.payload_bytes,
                epoch,
            )
            session.retransmit_bytes += len(protected)
            self._prune_retransmit(session)
        if not session.trace_first_frame:
            session.trace_first_frame = True
            with contextlib.suppress(Exception):
                _log.info(
                    "HomeKit live first_frame mode=%s keyframe=%s bytes=%d video_packets=%d "
                    "elapsed_ms=%s source_age_ms=%s",
                    _trace_label(output.mode, {"copy", "adapt"}),
                    output.is_keyframe is True,
                    len(output.payload),
                    session.packets,
                    _trace_age(self._clock(), session.prepared_at),
                    _trace_age(self._clock(), received_at),
                )
        return True

    async def _paced_send(self, session, protected, *, epoch, deadline=None):
        # Loss recovery shares the same byte clock as new media. An authenticated
        # NACK cannot turn a bounded cache into a burst outside the selected rate.
        async with session.send_lock:
            now = self._clock()
            target = max(now, session.next_packet_at)
            if target > (deadline if deadline is not None else now + MAX_ACCESS_UNIT_SECONDS):
                raise HomeKitOperationError("media_saturated")
            if target > now:
                await asyncio.sleep(target - now)
            await self._send(session, protected, epoch=epoch)
            session.next_packet_at = target + len(protected) * 8 / (
                session.config.max_bit_rate * 1000
            )

    def _prune_retransmit(self, session):
        cutoff = self._clock() - RETRANSMIT_SECONDS
        while session.retransmit:
            first = next(iter(session.retransmit.values()))
            if (
                first[0] >= cutoff
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
                if self._clock() - window >= 1:
                    window, received = self._clock(), 0
                received += 1
                if received > 128:
                    await asyncio.sleep(0.01)
                    continue
                try:
                    feedback = parse_feedback(
                        session.secure.unprotect_rtcp(protected), session.prepared.ssrc
                    )
                except (SrtpError, RtpError):
                    continue
                now = self._clock()
                session.last_feedback = now
                if feedback.bye:
                    self._end(session, "receiver_closed", stage="video_feedback")
                    return
                if feedback.pli and now - session.last_pli >= 0.5:
                    session.last_pli = now
                    session.adapter.request_keyframe()
                if now - session.last_nack_window >= 1:
                    session.last_nack_window, session.nack_count = now, 0
                self._prune_retransmit(session)
                for sequence in feedback.nack:
                    if session.nack_count >= 64:
                        break
                    cached = session.retransmit.get(sequence)
                    if cached is not None:
                        try:
                            await self._paced_send(session, cached[1], epoch=cached[3])
                        except _SourceChanged:
                            continue
                        session.packets += 1
                        session.octets += cached[2]
                        session.nack_count += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._end(session, "feedback_failed", stage="video_feedback", error=exc)

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
                    timestamp = (
                        session.last_timestamp
                        + int(max(0, self._clock() - session.last_packet_at) * 90000)
                    ) & 0xFFFFFFFF
                    report = sender_report(
                        session.prepared.ssrc, timestamp, session.packets, session.octets
                    )
                    await self._send(session, session.secure.protect_rtcp(report))
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
                await asyncio.sleep(0.25)
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
            for task in (session.start_task, session.sender, session.receiver, session.reports)
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
        session.failure = None
        session.state = "stopped"
        if self._sessions.get(session.identity) is session:
            del self._sessions[session.identity]

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
