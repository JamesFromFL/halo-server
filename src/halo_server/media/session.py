"""One leased upstream Ring media session for one camera."""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import logging
import math
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from enum import StrEnum

from halo_server.media.audio import (
    OPUS_FRAME_DURATION_MS,
    AudioNormalizationError,
    NormalizedOpusEncoder,
)
from halo_server.media.buffer import EncodedMediaBuffer, MediaBufferError
from halo_server.media.leases import (
    LeaseAction,
    LeaseActionType,
    LeasePurpose,
    MediaLease,
    MediaLeaseCoordinator,
)
from halo_server.media.legacy_audio import (
    NATIVE_QUEUE_SECONDS,
    LegacyAudioOwner,
    LegacyInitializationError,
    LegacyQueueError,
    LegacyTimingError,
    wait_native,
)
from halo_server.media.negotiated_codecs import sanitize_codec_diagnostics
from halo_server.media.opus import OpusError, OriginalOpusBuffer, parse_opus
from halo_server.media.recording_capture import RecordingSourceBuffer
from halo_server.media.ring_media import (
    EncodedFrameAdapterError,
    MediaCompatibilityError,
    MicrophonePcmBuffer,
    RingMediaProgressError,
    SourceClockObservation,
)
from halo_server.media.supervisor import supervise_receiver
from halo_server.media.telemetry import StreamMetrics

_log = logging.getLogger(__name__)

STOP_TIMEOUT = 5.0
RETRY_INITIAL_SECONDS = 1.0
RETRY_MAX_SECONDS = 30.0


class MediaSessionState(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(frozen=True)
class MediaSessionSnapshot:
    camera_id: str
    state: MediaSessionState
    generation: int
    lease_count: int
    lease_purposes: tuple[LeasePurpose, ...]
    buffer_ready: bool
    buffer_packets: int
    buffer_bytes: int
    opus_frame_duration_ms: float | None
    failure: str | None
    capture_attempt: int = 0


@dataclass(frozen=True)
class _ReceiverStopRequest:
    generation: int
    future: concurrent.futures.Future | None


class CameraMediaSession:
    """Share one upstream Ring receiver across bounded consumer leases."""

    def __init__(
        self,
        client,
        device,
        *,
        lease_coordinator: MediaLeaseCoordinator | None = None,
        buffer: EncodedMediaBuffer | None = None,
        receiver_supervisor=supervise_receiver,
        on_raw_audio: Callable[[bytes, int, int], None] | None = None,
        audio_encoder_factory=NormalizedOpusEncoder,
        clock: Callable[[], float] = time.monotonic,
        retry_initial: float = RETRY_INITIAL_SECONDS,
        retry_max: float = RETRY_MAX_SECONDS,
    ) -> None:
        _validate_retry_delay(retry_initial, "initial retry delay")
        _validate_retry_delay(retry_max, "maximum retry delay")
        if retry_initial > retry_max:
            raise ValueError("initial retry delay cannot exceed maximum retry delay")
        self._client = client
        self._device = device
        self._camera_id = str(device.id)
        self._clock = clock
        self._leases = lease_coordinator or MediaLeaseCoordinator(clock=clock)
        self._buffer = buffer or EncodedMediaBuffer(clock=clock)
        self._original_audio_buffer = OriginalOpusBuffer(clock=clock)
        self._recording_video_buffer = RecordingSourceBuffer(clock=clock)
        self._source_clocks = {}
        self._video_configuration_epoch = 0
        self._video_sps = self._video_pps = None
        self._video_configuration_floor = 0.0
        self._recording_seen_idr = False
        self._receiver_supervisor = receiver_supervisor
        self._on_raw_audio = on_raw_audio
        self._audio_encoder_factory = audio_encoder_factory
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._stop: threading.Event | None = None
        self._future: concurrent.futures.Future | None = None
        self._runner_generation = 0
        self._state = MediaSessionState.IDLE
        self._failure: str | None = None
        self._opus_frame_duration_ms: float | None = None
        self._audio_encoder: NormalizedOpusEncoder | None = None
        self._audio_owner = None
        self._retiring_audio = set()
        self._receiver_completed = None
        self._keyframe_requester = None
        self._legacy_failure = None
        self._legacy_callback_local = threading.local()
        self._microphone_source = MicrophonePcmBuffer()
        self._started_at: float | None = None
        self._first_video_at: float | None = None
        self._first_audio_at: float | None = None
        self._retry_initial = float(retry_initial)
        self._retry_max = float(retry_max)
        self._retry_delay = self._retry_initial
        self._retry_at: float | None = None
        self._capture_attempt = 0
        self._permanent_failure = False
        self._diagnostic_clock = time.monotonic
        self._diagnostic_lock = threading.Lock()
        self._diagnostic_revision = object()
        self._ingress_local = threading.local()
        self._last_ingress = {}
        self._last_pts = {}
        self._receiver_statistics = None
        self._receiver_statistics_at = None
        self._receiver_pipeline = None
        self._ring_stream_info = None
        self._codec_negotiation = None
        self._metrics = StreamMetrics(
            counters=(
                "audio_ingress_frames",
                "video_ingress_frames",
                "audio_admitted_frames",
                "video_admitted_frames",
                "audio_admitted_bytes",
                "video_admitted_bytes",
                "audio_admitted_samples",
                "audio_admission_rejected",
                "video_admission_rejected",
                "audio_pts_duplicate",
                "video_pts_duplicate",
                "audio_pts_backward",
                "video_pts_backward",
                "video_idrs",
                "legacy_audio_calls",
                "legacy_discontinuities",
                "legacy_timing_unavailable",
                "legacy_admission_busy",
                "receiver_stats_missing",
            ),
            timings=(
                "audio_ingress_interval",
                "video_ingress_interval",
                "audio_pre_admission",
                "video_pre_admission",
                "audio_entry_to_admission",
                "video_entry_to_admission",
                "audio_admission_wait",
                "video_admission_wait",
                "audio_pts_step",
                "video_pts_step",
                "source_clock_read_wait",
                "source_clock_write_wait",
                "authority_wait",
                "legacy_audio_wait",
                "legacy_encode",
                "source_loop_wakeup_lag",
                "receiver_stats_collection",
            ),
        )

    def _diagnostic_time(self):
        try:
            value = self._diagnostic_clock()
            return (
                value
                if type(value) in {int, float} and math.isfinite(value) and value >= 0
                else None
            )
        except BaseException:
            return None

    def _metric(self, method, name, value=1):
        with suppress(BaseException):
            getattr(self._metrics, method)(name, value)

    @contextmanager
    def _measured_state_lock(self, name):
        started = self._diagnostic_time()
        acquired = None
        try:
            with self._lock:
                acquired = self._diagnostic_time()
                yield
        finally:
            if started is not None and acquired is not None:
                self._metric("observe", name, max(0, acquired - started))

    def _encoded_ingress(self, generation, kind, timestamp, entered):
        if (
            generation != self._runner_generation
            or kind not in {"audio", "video"}
            or type(timestamp) is not int
            or type(entered) not in {int, float}
            or not math.isfinite(entered)
            or entered < 0
        ):
            return None
        with self._diagnostic_lock:
            if generation != self._runner_generation or self._stop is None or self._stop.is_set():
                return None
            token = (generation, kind, timestamp, entered, self._diagnostic_revision)
            previous = self._last_ingress.get(kind)
            self._last_ingress[kind] = entered
        self._ingress_local.entry = token
        self._metric("count", f"{kind}_ingress_frames")
        if previous is not None:
            self._metric("observe", f"{kind}_ingress_interval", max(0, entered - previous))

        def clear():
            if getattr(self._ingress_local, "entry", None) is token:
                del self._ingress_local.entry

        return clear

    def _ingress_entry(self, generation, kind, timestamp):
        token = getattr(self._ingress_local, "entry", None)
        with self._diagnostic_lock:
            return (
                token[3]
                if token is not None
                and token[:3] == (generation, kind, timestamp)
                and token[4] is self._diagnostic_revision
                else None
            )

    def _admitted(self, kind, packet, entered):
        with suppress(BaseException):
            self._observe_admitted(kind, packet, entered)

    def _observe_admitted(self, kind, packet, entered):
        # Only metadata derived from already-admitted packets is observed here.
        self._metric("count", f"{kind}_admitted_frames")
        self._metric("count", f"{kind}_admitted_bytes", len(packet.payload))
        finished = self._diagnostic_time()
        if finished is not None and entered is not None:
            self._metric("observe", f"{kind}_entry_to_admission", max(0, finished - entered))
        with self._diagnostic_lock:
            previous = self._last_pts.get(kind)
            self._last_pts[kind] = packet.capture_epoch, packet.rtp_timestamp
        if previous is not None and previous[0] == packet.capture_epoch:
            delta = packet.rtp_timestamp - previous[1]
            if delta > 0:
                self._metric(
                    "observe", f"{kind}_pts_step", delta / (48000 if kind == "audio" else 90000)
                )
            elif delta == 0:
                self._metric("count", f"{kind}_pts_duplicate")
            else:
                self._metric("count", f"{kind}_pts_backward")
        if kind == "audio":
            with suppress(BaseException):
                self._metric(
                    "count", "audio_admitted_samples", parse_opus(packet.payload).duration_samples
                )
        elif 5 in packet.h264_nal_types:
            self._metric("count", "video_idrs")

    def _receiver_stats(self, generation, snapshot, lag, duration):
        with self._diagnostic_lock:
            stop = self._stop
            revision = self._diagnostic_revision
            if generation != self._runner_generation or stop is None or stop.is_set():
                return
        safe = None
        pipeline = None
        stream_info = None
        codec_negotiation = None
        if isinstance(snapshot, dict):
            stream_info = _safe_stream_info(snapshot.get("stream_info"))
            codec_negotiation = sanitize_codec_diagnostics(snapshot.get("codec_negotiation"))
            with suppress(BaseException):
                pipeline = _safe_pipeline(snapshot.get("pipeline"))
            safe = {}
            for kind in ("audio", "video"):
                row = snapshot.get(kind)
                safe[kind] = None
                if not isinstance(row, dict):
                    continue
                fields = {}
                for name in ("packets_received", "packets_lost", "jitter_ticks"):
                    value = row.get(name)
                    low = -(2**53 - 1) if name == "packets_lost" else 0
                    fields[name] = (
                        value if type(value) is int and low <= value <= 2**53 - 1 else None
                    )
                fields["clock_rate"] = 48000 if kind == "audio" else 90000
                safe[kind] = fields
        with self._diagnostic_lock:
            if (
                revision is not self._diagnostic_revision
                or generation != self._runner_generation
                or self._stop is not stop
                or stop.is_set()
            ):
                return
            self._receiver_statistics = safe
            self._receiver_pipeline = pipeline
            self._ring_stream_info = stream_info
            self._codec_negotiation = codec_negotiation
            self._receiver_statistics_at = self._diagnostic_time()
        if safe is None or all(row is None for row in safe.values()):
            self._metric("count", "receiver_stats_missing")
        self._metric("observe", "source_loop_wakeup_lag", lag)
        self._metric("observe", "receiver_stats_collection", duration)

    def diagnostics(self):
        now = self._diagnostic_time()
        owner = self._audio_owner
        with self._diagnostic_lock:
            received = self._receiver_statistics_at
            age = None if now is None or received is None else max(0, now - received)
            statistics = self._receiver_statistics if age is not None and age <= 3 else None
            safe = (
                {kind: dict(row) if row is not None else None for kind, row in statistics.items()}
                if statistics is not None
                else None
            )
            pipeline = (
                copy.deepcopy(self._receiver_pipeline) if age is not None and age <= 3 else None
            )
            stream_info = (
                copy.deepcopy(self._ring_stream_info) if age is not None and age <= 3 else None
            )
            codec_negotiation = (
                copy.deepcopy(self._codec_negotiation) if age is not None and age <= 3 else None
            )
            if stream_info is not None and stream_info["age_ms"] is not None:
                stream_info["age_ms"] = min(2**53 - 1, stream_info["age_ms"] + round(age * 1000))
        return {
            "metrics": self._metrics.snapshot(),
            "receiver_stats": safe,
            "receiver_stats_age_ms": round(age * 1000) if age is not None else None,
            "receiver_stats_kind": "public_nonaggregate",
            "receiver_pipeline": pipeline,
            "ring_stream_info": stream_info,
            "codec_negotiation": codec_negotiation,
            "legacy_audio": {
                "failure": self._legacy_failure,
                "cleanup_pending": len(self._retiring_audio),
                "owner": owner.diagnostics() if owner is not None else None,
            },
        }

    @property
    def buffer(self) -> EncodedMediaBuffer:
        return self._buffer

    @property
    def original_audio_buffer(self) -> OriginalOpusBuffer:
        return self._original_audio_buffer

    @property
    def recording_video_buffer(self):
        return self._recording_video_buffer

    def source_clock_snapshot(self):
        with self._measured_state_lock("source_clock_read_wait"):
            epoch = self._buffer.capture_epoch
            return tuple(
                observation
                for observation in self._source_clocks.values()
                if observation.capture_epoch == epoch
                and 0 <= self._clock() - observation.received_at <= 30
            )

    def video_configuration_snapshot(self):
        """Current bounded codec metadata, independent of rolling media age."""
        with self._lock:
            if self._video_sps is None or self._video_pps is None:
                return None
            return (
                self._buffer.capture_epoch,
                self._video_configuration_epoch,
                self._video_sps,
                self._video_pps,
                self._video_configuration_floor,
            )

    def _clear_media_locked(self):
        self._keyframe_requester = None
        self._retire_audio_locked()
        self._buffer.clear()
        self._original_audio_buffer.clear(capture_epoch=self._buffer.capture_epoch)
        self._recording_video_buffer.clear(capture_epoch=self._buffer.capture_epoch)
        self._source_clocks.clear()
        self._video_configuration_epoch = 0
        self._video_sps = self._video_pps = None
        self._recording_seen_idr = False
        with self._diagnostic_lock:
            # One opaque revision slot: no counter growth, wrap or ABA reuse.
            self._diagnostic_revision = object()
            self._last_ingress.clear()
            self._last_pts.clear()
            self._receiver_statistics = self._receiver_statistics_at = None
            self._receiver_pipeline = None
            self._ring_stream_info = None
            self._codec_negotiation = None

    def _source_clock(self, generation, observation):
        with self._measured_state_lock("source_clock_write_wait"):
            if (
                not self._accepts_media_locked(generation)
                or not isinstance(observation, SourceClockObservation)
                or observation.kind not in {"audio", "video"}
            ):
                return
            if observation.sender_ntp == 0:
                self._source_clocks.pop(observation.kind, None)
                return
            self._source_clocks[observation.kind] = replace(
                observation, capture_epoch=self._buffer.capture_epoch
            )

    def _encoded_audio(self, generation, payload, timestamp):
        entered = self._ingress_entry(generation, "audio", timestamp)
        started = self._diagnostic_time()
        if entered is not None and started is not None:
            self._metric("observe", "audio_pre_admission", max(0, started - entered))
        with self._measured_state_lock("audio_admission_wait"):
            if not self._accepts_media_locked(generation):
                self._metric("count", "audio_admission_rejected")
                return False
            epoch = self._buffer.capture_epoch
            if self._original_audio_buffer.capture_epoch != epoch:
                self._original_audio_buffer.clear(capture_epoch=epoch)
                self._source_clocks.clear()
            try:
                packet = self._original_audio_buffer.append_audio(payload, timestamp)
            except OpusError:
                # Original audio cannot compromise the legacy normalized lane
                # or turn a functioning video session into a false failure.
                self._metric("count", "audio_admission_rejected")
                return False
            self._admitted("audio", packet, entered)
            return True

    @property
    def receiver_retired(self) -> bool:
        """Completion evidence for the shared manager's retirement barrier."""
        with self._lock:
            return (
                self._future is None
                and not self._retiring_audio
                and self._audio_owner is None
                and not self._leases.snapshot().leases
                and self._state is MediaSessionState.IDLE
            )

    def owns_source(self, client, device) -> bool:
        """Match the exact account client and reconciled SDK wrapper owner."""
        with self._lock:
            return self._client is client and self._device is device

    def is_lease_active(self, lease_id: str) -> bool:
        """Check delivery authority without renewing or mutating lease state."""
        with self._measured_state_lock("authority_wait"):
            lease = self._current_lease_locked(lease_id)
            return lease is not None and self._clock() < lease.expires_at

    def _attach_keyframes(self, generation, callback):
        with self._lock:
            if self._accepts_media_locked(generation) and callable(callback):
                self._keyframe_requester = (generation, self._buffer.capture_epoch, callback)

    def request_keyframe(self, lease_id, capture_epoch, *, admission):
        if type(capture_epoch) is not int or capture_epoch < 0:
            return False
        with self._lock:
            owner = self._keyframe_requester

        def current():
            if not admission():
                return False
            with self._lock:
                lease = self._current_lease_locked(lease_id)
                return bool(
                    owner is not None
                    and self._keyframe_requester is owner
                    and owner[1] == capture_epoch == self._buffer.capture_epoch
                    and self._accepts_media_locked(owner[0])
                    and lease is not None
                    and lease.purpose is LeasePurpose.HOMEKIT_LIVE
                    and self._clock() < lease.expires_at
                )

        if not current():
            return False
        try:
            future = owner[2](current)
        except Exception:
            return False
        try:
            attempted = bool(future.result(timeout=1.1))
        except (TimeoutError, concurrent.futures.CancelledError):
            future.cancel()
            return False
        except Exception:
            return False
        return attempted and current()

    def acquire(
        self,
        purpose: LeasePurpose,
        *,
        ttl: float | None = None,
        lease_id: str | None = None,
        now: float | None = None,
    ) -> MediaLease:
        stop_requests: list[_ReceiverStopRequest] = []
        try:
            with self._lock:
                current_time = self._now(now)
                intercom_was_active = self._intercom_active_locked()
                lease, actions = self._leases.acquire(
                    purpose,
                    ttl=ttl,
                    lease_id=lease_id,
                    now=current_time,
                )
                try:
                    stop_requests.extend(self._apply_actions_locked(actions))
                    self._retry_if_due_locked(current_time)
                except BaseException:
                    failure = self._failure
                    stop_requests.extend(
                        self._apply_actions_locked(self._leases.release(lease.id, now=current_time))
                    )
                    self._state = MediaSessionState.FAILED
                    self._failure = failure or "receiver_submission_failed"
                    raise
                if purpose is LeasePurpose.INTERCOM and not intercom_was_active:
                    intercom_generation = self._microphone_source.begin_intercom()
                    _log.info(
                        "Ring intercom lease opened camera_id=%s generation=%d "
                        "intercom_generation=%d",
                        self._camera_id,
                        lease.generation,
                        intercom_generation,
                    )
                current_lease = self._current_lease_locked(lease.id) or lease
        except BaseException:
            self._wait_for_stop_requests(stop_requests)
            raise
        self._wait_for_stop_requests(stop_requests)
        return current_lease

    def renew(
        self,
        lease_id: str,
        *,
        ttl: float | None = None,
        now: float | None = None,
    ) -> MediaLease | None:
        stop_requests: list[_ReceiverStopRequest]
        with self._lock:
            current_time = self._now(now)
            lease, actions = self._leases.renew(lease_id, ttl=ttl, now=current_time)
            stop_requests = self._apply_actions_locked(actions)
            self._retry_if_due_locked(current_time)
            current_lease = None if lease is None else self._current_lease_locked(lease.id) or lease
        self._wait_for_stop_requests(stop_requests)
        return current_lease

    def release(self, lease_id: str, *, now: float | None = None) -> None:
        with self._lock:
            current_time = self._now(now)
            intercom_was_active = self._intercom_active_locked()
            stop_requests = self._apply_actions_locked(
                self._leases.release(lease_id, now=current_time)
            )
            self._finish_intercom_if_inactive_locked(intercom_was_active)
            self._clear_retry_without_demand_locked()
        self._wait_for_stop_requests(stop_requests)

    def expire(self, *, now: float | None = None) -> None:
        with self._lock:
            current_time = self._now(now)
            intercom_was_active = self._intercom_active_locked()
            stop_requests = self._apply_actions_locked(self._leases.expire(now=current_time))
            self._finish_intercom_if_inactive_locked(intercom_was_active)
            self._clear_retry_without_demand_locked()
            self._retry_if_due_locked(current_time)
        self._wait_for_stop_requests(stop_requests)

    def stop_all(self, reason: str = "service_stop") -> None:
        with self._lock:
            intercom_was_active = self._intercom_active_locked()
            stop_requests = self._apply_actions_locked(self._leases.stop_all(reason))
            self._finish_intercom_if_inactive_locked(intercom_was_active)
            self._clear_retry_without_demand_locked()
        self._wait_for_stop_requests(stop_requests)

    def push_intercom_audio(self, lease_id: str, payload: bytes) -> None:
        """Queue one normalized PCM frame for an active intercom lease."""
        with self._lock:
            lease = self._current_lease_locked(lease_id)
            if lease is None or lease.purpose is not LeasePurpose.INTERCOM:
                raise RuntimeError("intercom lease is not active")
            generation = self._microphone_source.snapshot().intercom_generation
        self.push_intercom_generation(generation, payload)

    def intercom_generation(self, lease_id: str) -> int | None:
        """Return the PCM generation for one currently active intercom lease."""
        with self._lock:
            lease = self._current_lease_locked(lease_id)
            if lease is None or lease.purpose is not LeasePurpose.INTERCOM:
                return None
            return self._microphone_source.snapshot().intercom_generation

    def push_intercom_generation(self, generation: int, payload: bytes, *, admission=None) -> None:
        """Queue PCM through the short, generation-scoped microphone lock."""
        if not self._microphone_source.push_intercom(generation, payload, admission=admission):
            raise RuntimeError("intercom lease is not active")
        if self._microphone_source.snapshot().accepted_frames == 1:
            _log.info(
                "Ring intercom first PCM frame accepted camera_id=%s generation=%d bytes=%d",
                self._camera_id,
                self._runner_generation,
                len(payload),
            )

    def wait_intercom_ready(self, lease_id: str, timeout: float) -> bool:
        """Wait until Ring acknowledges the speaker for one active talk lease."""
        with self._lock:
            lease = self._current_lease_locked(lease_id)
            if lease is None or lease.purpose is not LeasePurpose.INTERCOM:
                return False
            generation = self._microphone_source.snapshot().intercom_generation
        return self.wait_intercom_generation_ready(generation, timeout)

    def wait_intercom_generation_ready(self, generation: int, timeout: float) -> bool:
        """Wait for speaker readiness using an already validated generation."""
        return self._microphone_source.wait_speaker_ready(generation, timeout)

    def snapshot(self) -> MediaSessionSnapshot:
        with self._lock:
            leases = self._leases.snapshot()
            media = self._buffer.snapshot()
            return MediaSessionSnapshot(
                camera_id=self._camera_id,
                state=self._state,
                generation=self._runner_generation,
                lease_count=len(leases.leases),
                lease_purposes=tuple(
                    sorted(
                        (lease.purpose for lease in leases.leases),
                        key=str,
                    )
                ),
                buffer_ready=media.ready,
                buffer_packets=len(media.packets),
                buffer_bytes=media.payload_bytes,
                opus_frame_duration_ms=self._opus_frame_duration_ms,
                failure=self._failure,
                capture_attempt=self._capture_attempt,
            )

    def _apply_actions_locked(
        self,
        actions: list[LeaseAction],
    ) -> list[_ReceiverStopRequest]:
        stop_requests: list[_ReceiverStopRequest] = []
        for action in actions:
            if action.type is LeaseActionType.START_UPSTREAM:
                self._start_locked(action)
            elif action.type is LeaseActionType.STOP_UPSTREAM:
                request = self._stop_locked(action)
                if request is not None:
                    stop_requests.append(request)
        return stop_requests

    def _start_locked(self, action: LeaseAction) -> None:
        if self._future is not None and not self._future.done():
            _log.info(
                "Ring media generation waiting for teardown camera_id=%s "
                "active_generation=%d pending_generation=%d",
                self._camera_id,
                self._runner_generation,
                action.generation,
            )
            return
        if self._retiring_audio:
            return
        self._runner_generation = action.generation
        self._capture_attempt += 1
        self._permanent_failure = False
        self._retry_at = None
        self._state = MediaSessionState.STARTING
        self._failure = None
        self._opus_frame_duration_ms = None
        self._started_at = self._clock()
        self._first_video_at = None
        self._first_audio_at = None
        self._clear_media_locked()
        self._audio_owner = LegacyAudioOwner(
            self._audio_encoder_factory, retired=self._audio_retired
        )
        self._legacy_failure = None
        self._microphone_source.clear()
        stop = threading.Event()
        self._stop = stop
        receiver_kwargs = {
            "on_attempt": lambda attempt: self._attempt_started(action.generation, attempt),
            "on_attempt_end": lambda attempt, error: self._attempt_ended(
                action.generation, attempt, error
            ),
            "on_encoded_video": lambda payload, timestamp: self._encoded_video(
                action.generation,
                payload,
                timestamp,
            ),
            "on_encoded_audio": lambda payload, timestamp: self._encoded_audio(
                action.generation, payload, timestamp
            ),
            "on_keyframe_requester": lambda callback: self._attach_keyframes(
                action.generation, callback
            ),
            "on_source_clock": lambda observation: self._source_clock(
                action.generation, observation
            ),
            "on_encoded_ingress": lambda kind, timestamp, entered: self._encoded_ingress(
                action.generation, kind, timestamp, entered
            ),
            "on_receiver_stats": lambda snapshot, lag, duration: self._receiver_stats(
                action.generation, snapshot, lag, duration
            ),
            "decode_video": False,
            "decode_audio": True,
            "on_decoded_audio": lambda frame: self._decoded_audio(action.generation, frame),
            "on_encoded_incompatible": lambda kind, mime_type: self._incompatible(
                action.generation,
                kind,
                mime_type,
            ),
            "microphone_source": self._microphone_source,
        }
        coroutine = None
        try:
            coroutine = self._receiver_supervisor(
                self._device,
                None,
                lambda payload, channels, rate: self._raw_audio(
                    action.generation,
                    payload,
                    channels,
                    rate,
                ),
                stop,
                receiver_kwargs=receiver_kwargs,
            )
            future = self._client.submit(coroutine)
        except BaseException:
            if coroutine is not None:
                close = getattr(coroutine, "close", None)
                if callable(close):
                    close()
            stop.set()
            self._stop = None
            self._audio_encoder = None
            self._retire_audio_locked()
            self._state = MediaSessionState.FAILED
            self._failure = "receiver_submission_failed"
            raise
        self._future = future
        _log.info(
            "Ring media generation started camera_id=%s kind=%s generation=%d reason=%s",
            self._camera_id,
            getattr(self._device, "kind", "unknown"),
            action.generation,
            action.reason,
        )
        future.add_done_callback(lambda completed: self._runner_done(action.generation, completed))

    def _stop_locked(self, action: LeaseAction) -> _ReceiverStopRequest | None:
        if action.generation != self._runner_generation:
            return None
        stop = self._stop
        future = self._future
        elapsed = self._elapsed_locked()
        media = self._buffer.snapshot()
        _log.info(
            "Ring media generation stopping camera_id=%s generation=%d reason=%s "
            "state=%s elapsed=%.3f packets=%d bytes=%d",
            self._camera_id,
            action.generation,
            action.reason,
            self._state,
            elapsed,
            len(media.packets),
            media.payload_bytes,
        )
        self._cancel_retry_locked()
        self._state = MediaSessionState.STOPPING
        self._failure = None
        self._clear_media_locked()
        self._opus_frame_duration_ms = None
        if stop is not None:
            stop.set()
        if future is None:
            self._stop = None
            self._audio_encoder = None
            self._microphone_source.clear()
            if not self._retiring_audio:
                self._state = MediaSessionState.IDLE
                self._failure = None
            self._condition.notify_all()
            return _ReceiverStopRequest(action.generation, None) if self._retiring_audio else None
        return _ReceiverStopRequest(action.generation, future)

    def _intercom_active_locked(self) -> bool:
        return any(
            lease.purpose is LeasePurpose.INTERCOM for lease in self._leases.snapshot().leases
        )

    def _current_lease_locked(self, lease_id: str) -> MediaLease | None:
        return next(
            (lease for lease in self._leases.snapshot().leases if lease.id == lease_id),
            None,
        )

    def _finish_intercom_if_inactive_locked(self, was_active: bool) -> None:
        if not was_active or self._intercom_active_locked():
            return
        stats = self._microphone_source.snapshot()
        _log.info(
            "Ring intercom lease closed camera_id=%s generation=%d accepted=%d "
            "consumed=%d dropped=%d queued=%d",
            self._camera_id,
            self._runner_generation,
            stats.accepted_frames,
            stats.consumed_frames,
            stats.dropped_frames,
            stats.queued_frames,
        )
        self._microphone_source.end_intercom()

    def _attempt_started(self, generation: int, attempt: int) -> None:
        with self._lock:
            if not self._accepts_media_locked(generation) or attempt == 1:
                return
            if self._retiring_audio:
                raise AudioNormalizationError("Previous legacy audio cleanup pending")
            self._capture_attempt += 1
            self._clear_media_locked()
            self._state = MediaSessionState.STARTING
            self._failure = None
            self._opus_frame_duration_ms = None
            self._started_at = self._clock()
            self._first_video_at = None
            self._first_audio_at = None
            self._audio_owner = LegacyAudioOwner(
                self._audio_encoder_factory, retired=self._audio_retired
            )
            self._legacy_failure = None
            self._microphone_source.clear()

    def _attempt_ended(self, generation: int, _attempt: int, error: Exception | None) -> None:
        with self._lock:
            if not self._accepts_media_locked(generation):
                return
            self._clear_media_locked()
            self._opus_frame_duration_ms = None
            self._audio_encoder = None
            self._microphone_source.clear()
            self._state = MediaSessionState.FAILED
            if isinstance(error, (MediaCompatibilityError, EncodedFrameAdapterError)):
                self._permanent_failure = True
                self._failure = "media_incompatible"
            elif isinstance(error, RingMediaProgressError):
                self._failure = str(error)
            else:
                self._failure = "receiver_restarting"

    def _observe_video_configuration_locked(self, payload, received_at):
        from halo_server.media.h264 import MAX_SPS_BYTES, H264Error, split_annexb

        try:
            nals = split_annexb(payload)
        except H264Error:
            return
        sps = next((nal for nal in reversed(nals) if nal[0] & 31 == 7), None)
        pps = next((nal for nal in reversed(nals) if nal[0] & 31 == 8), None)
        if any(nal is not None and len(nal) > MAX_SPS_BYTES for nal in (sps, pps)):
            return
        changed = False
        if sps is not None and sps != self._video_sps:
            self._video_sps, self._video_pps = bytes(sps), None
            changed = True
        if pps is not None and pps != self._video_pps:
            self._video_pps = bytes(pps)
            changed = True
        if changed:
            self._video_configuration_epoch += 1
            self._video_configuration_floor = received_at
            self._recording_seen_idr = False

    def _encoded_video(self, generation: int, payload: bytes, timestamp: int) -> bool:
        entered = self._ingress_entry(generation, "video", timestamp)
        started = self._diagnostic_time()
        if entered is not None and started is not None:
            self._metric("observe", "video_pre_admission", max(0, started - entered))
        with self._measured_state_lock("video_admission_wait"):
            if not self._accepts_media_locked(generation):
                self._metric("count", "video_admission_rejected")
                return False
            now = self._clock()
            if self._first_video_at is None:
                self._first_video_at = now
                _log.info(
                    "Ring media first video camera_id=%s generation=%d elapsed=%.3f "
                    "bytes=%d rtp_timestamp=%d",
                    self._camera_id,
                    generation,
                    self._elapsed_locked(now),
                    len(payload),
                    timestamp,
                )
            try:
                packet = self._buffer.append_video(payload, timestamp)
            except MediaBufferError as exc:
                self._metric("count", "video_admission_rejected")
                self._state = MediaSessionState.FAILED
                self._failure = "encoded_video_rejected"
                if self._stop is not None:
                    self._stop.set()
                _log.warning("Rejected encoded Ring video for %s: %s", self._device.name, exc)
                return False
            self._admitted("video", packet, entered)
            self._observe_video_configuration_locked(payload, packet.received_at)
            self._recording_video_buffer.append(packet)
            if (
                5 in packet.h264_nal_types
                and self._video_sps is not None
                and self._video_pps is not None
            ):
                self._recording_seen_idr = True
            recording_ready = self._recording_seen_idr and any(
                lease.purpose
                in {LeasePurpose.HOMEKIT_RECORDING, LeasePurpose.HOMEKIT_RECORDING_WARM}
                and lease.expires_at > now
                for lease in self._leases.snapshot().leases
            )
            useful = (self._buffer.ready or recording_ready) and bool(
                packet.h264_nal_types & {1, 5}
            )
            if not useful:
                return False
            self._media_recovered_locked()
            if self._state is not MediaSessionState.READY and self._buffer.ready:
                self._state = MediaSessionState.READY
                _log.info(
                    "Ring media keyframe ready camera_id=%s generation=%d elapsed=%.3f",
                    self._camera_id,
                    generation,
                    self._elapsed_locked(now),
                )
            return True

    def _retire_audio_locked(self):
        owner, self._audio_owner = self._audio_owner, None
        self._audio_encoder = None
        if owner is not None:
            self._retiring_audio.add(owner)
            owner.fence()

    def _audio_retired(self, owner):
        with self._condition:
            self._retiring_audio.discard(owner)
            self._finish_runner_locked()
            self._condition.notify_all()

    def _legacy_claim(self, generation):
        self._metric("count", "legacy_audio_calls")
        with self._measured_state_lock("legacy_audio_wait"):
            owner = self._audio_owner
            if not self._accepts_media_locked(generation) or owner is None:
                return None
            if not owner.claim():
                self._metric("count", "legacy_admission_busy")
                return None
            return (
                generation,
                self._buffer.capture_epoch,
                owner,
                self._stop,
                self._capture_attempt,
            )

    def _legacy_current_locked(self, token):
        generation, epoch, owner, stop, attempt = token
        return (
            self._accepts_media_locked(generation)
            and self._buffer.capture_epoch == epoch
            and self._audio_owner is owner
            and self._stop is stop
            and self._capture_attempt == attempt
        )

    def _legacy_result(self, token, result, error):
        callback = None
        with self._lock:
            if not self._legacy_current_locked(token):
                return
            if isinstance(error, (LegacyTimingError, LegacyQueueError)):
                # Unknown timing cannot justify invented legacy gap coverage.
                # Original Opus/video ownership and delivery remain independent.
                self._legacy_failure = (
                    "legacy_audio_timing_unavailable"
                    if isinstance(error, LegacyTimingError)
                    else "legacy_audio_capacity"
                )
                self._metric(
                    "count",
                    "legacy_timing_unavailable"
                    if isinstance(error, LegacyTimingError)
                    else "legacy_admission_busy",
                )
                return
            if error is None and result is not None:
                try:
                    for packet in result.packets:
                        self._buffer.append_audio(packet.payload, packet.rtp_timestamp)
                    self._opus_frame_duration_ms = OPUS_FRAME_DURATION_MS
                    self._first_audio_at = self._first_audio_at or self._clock()
                    self._legacy_failure = None
                    self._metric("observe", "legacy_encode", result.encode_seconds)
                    if result.discontinuity:
                        self._metric("count", "legacy_discontinuities")
                    callback = self._on_raw_audio
                except MediaBufferError as exc:
                    error = exc
            if error is not None:
                self._state = MediaSessionState.FAILED
                self._failure = (
                    "audio_encoder_initialization_failed"
                    if isinstance(error, LegacyInitializationError)
                    else "audio_normalization_failed"
                )
                self._legacy_failure = self._failure
                if self._stop is not None:
                    self._stop.set()
                callback = None
        if callback is not None:
            # Preserve the existing Ring-loop callback affinity and retain the
            # claim through the external call. No internal lock is held here.
            with self._lock:
                if not self._legacy_current_locked(token):
                    return
            self._legacy_callback_local.owner = token[2]
            try:
                callback(result.payload, result.channels, result.rate)
            finally:
                del self._legacy_callback_local.owner

    def _raw_audio(self, generation, payload, channels, rate):
        """Compatibility synchronous callback; waits outside camera state."""
        token = self._legacy_claim(generation)
        if token is None:
            return
        owner = token[2]
        result = error = None
        try:
            try:
                future = owner.submit(payload, channels, rate)
                try:
                    result = future.result(timeout=NATIVE_QUEUE_SECONDS)
                except concurrent.futures.TimeoutError:
                    owner.cancel_queued(future)
                    result = future.result()
            except BaseException as exc:
                error = exc
            self._legacy_result(token, result, error)
        finally:
            owner.release()
            payload = result = error = None

    async def _decoded_audio(self, generation, frame):
        """Production private callback: one retained native job off Ring's loop."""
        token = self._legacy_claim(generation)
        if token is None:
            return
        owner = token[2]
        result = error = None
        try:
            try:
                result = await wait_native(owner.submit(frame=frame), owner)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                error = exc
            self._legacy_result(token, result, error)
        finally:
            owner.release()
            # A retained cancelled Task may retain its traceback after native
            # completion. Its frame must not keep uncharged decoded media.
            frame = result = error = None

    def _incompatible(self, generation: int, kind: str, mime_type: str) -> None:
        with self._lock:
            if not self._accepts_media_locked(generation):
                return
            self._state = MediaSessionState.FAILED
            self._failure = f"unsupported_{kind}_codec"
            self._permanent_failure = True
            if self._stop is not None:
                self._stop.set()
        _log.warning(
            "Ring camera %s negotiated unsupported %s codec %s",
            self._device.name,
            kind,
            mime_type,
        )

    def _runner_done(
        self,
        generation: int,
        future: concurrent.futures.Future,
    ) -> None:
        with self._condition:
            if generation != self._runner_generation or future is not self._future:
                self._condition.notify_all()
                return
            self._receiver_completed = (generation, self._state is MediaSessionState.STOPPING)
            self._future = None
            # Completion ends this receiver's callback authority immediately.
            # Native cleanup may still be owned and joined asynchronously.
            if self._stop is not None:
                self._stop.set()
            self._retire_audio_locked()
            if self._retiring_audio:
                self._state = MediaSessionState.STOPPING
            self._finish_runner_locked()
            self._condition.notify_all()

    def _finish_runner_locked(self):
        if self._receiver_completed is None or self._retiring_audio:
            return
        generation, was_stopping = self._receiver_completed
        self._receiver_completed = None
        self._stop = None
        self._audio_encoder = None
        self._microphone_source.clear()
        leases = self._leases.snapshot()
        if leases.upstream_requested and was_stopping:
            try:
                self._start_locked(
                    LeaseAction(
                        LeaseActionType.START_UPSTREAM, leases.generation, "receiver_replacement"
                    )
                )
            except Exception:
                self._state = MediaSessionState.FAILED
                self._failure = "receiver_submission_failed"
                self._schedule_retry_locked()
        elif leases.upstream_requested:
            self._state = MediaSessionState.FAILED
            self._failure = self._failure or "receiver_stopped"
            self._schedule_retry_locked()
        else:
            self._state = MediaSessionState.IDLE
            self._failure = None
            self._cancel_retry_locked()
        _log.info(
            "Ring media generation completed camera_id=%s generation=%d state=%s failure=%s",
            self._camera_id,
            generation,
            self._state,
            self._failure or "none",
        )

    def _wait_for_stop_requests(self, requests):
        with self._condition:
            retiring = tuple(self._retiring_audio)
            if not requests and retiring:
                requests = [_ReceiverStopRequest(self._runner_generation, self._future)]
        for owner in retiring:
            owner.fence()  # Retry a retained close failure; never replace its owner.
        for request in requests:
            deadline = time.monotonic() + STOP_TIMEOUT
            with self._condition:
                reentrant = any(
                    owner.active_thread == threading.get_ident()
                    or getattr(self._legacy_callback_local, "owner", None) is owner
                    for owner in self._retiring_audio
                )
                if reentrant:
                    self._failure = "receiver_cleanup_pending"
                    return
            if request.future is not None:
                try:
                    request.future.result(timeout=max(0.001, deadline - time.monotonic()))
                except concurrent.futures.TimeoutError:
                    pass
                except Exception as exc:
                    _log.debug("Ring media receiver stopped with an error: %s", exc)
            with self._condition:
                while (
                    request.future is not None and self._future is request.future
                ) or self._retiring_audio:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if request.generation == self._runner_generation:
                            self._state = MediaSessionState.STOPPING
                            self._failure = "receiver_stop_timeout"
                        break
                    self._condition.wait(remaining)

    def _retry_if_due_locked(self, now: float) -> None:
        if (
            self._retry_at is None
            or now < self._retry_at
            or (self._future is not None and not self._future.done())
            or self._retiring_audio
            or not self._leases.snapshot().leases
        ):
            return
        self._retry_at = None
        actions = self._leases.restart_upstream("receiver_retry")
        try:
            self._apply_actions_locked(actions)
        except Exception as exc:
            _log.warning(
                "Could not restart Ring media for camera %s: %s",
                self._camera_id,
                exc,
            )
            self._schedule_retry_locked(now)

    def _schedule_retry_locked(self, now: float | None = None) -> None:
        if self._permanent_failure:
            self._retry_at = None
            return
        if not self._leases.snapshot().leases:
            self._cancel_retry_locked()
            return
        current_time = self._now(now)
        self._retry_at = current_time + self._retry_delay
        self._retry_delay = min(self._retry_delay * 2, self._retry_max)

    def _cancel_retry_locked(self) -> None:
        self._retry_at = None
        self._retry_delay = self._retry_initial

    def _clear_retry_without_demand_locked(self) -> None:
        if self._leases.snapshot().leases:
            return
        self._cancel_retry_locked()
        if self._future is None and not self._retiring_audio:
            self._state = MediaSessionState.IDLE
            self._failure = None

    def _media_recovered_locked(self) -> None:
        self._retry_at = None
        self._retry_delay = self._retry_initial

    def _accepts_media_locked(self, generation: int) -> bool:
        return (
            generation == self._runner_generation
            and self._stop is not None
            and not self._stop.is_set()
        )

    def _elapsed_locked(self, now: float | None = None) -> float:
        if self._started_at is None:
            return 0.0
        return max(0.0, (self._clock() if now is None else now) - self._started_at)

    def _now(self, supplied: float | None) -> float:
        value = self._clock() if supplied is None else supplied
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("monotonic time must be numeric")
        normalized = float(value)
        if not math.isfinite(normalized) or normalized < 0:
            raise ValueError("monotonic time must be finite and non-negative")
        return normalized


def _validate_retry_delay(value: float, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{label} must be finite and positive")


def _safe_stream_info(value):
    """Finite SDK observation only; never a raw signaling dictionary."""
    if not isinstance(value, dict):
        return None
    if (
        type(value.get("state")) is not str
        or value["state"] not in {"absent", "unavailable", "not_seen", "observed"}
        or value.get("installed_before_reader") is not None
        and type(value["installed_before_reader"]) is not bool
        or value.get("transcoding") is not None
        and type(value["transcoding"]) is not bool
        or value.get("reason") is not None
        and (
            type(value["reason"]) is not str
            or value["reason"] not in {"codec_mismatch", "other", "unknown"}
        )
    ):
        return None
    fields = ("observations", "invalid_messages", "oversized_messages")
    if any(type(value.get(name)) is not int or not 0 <= value[name] < 2**53 for name in fields):
        return None
    age = value.get("age_ms")
    if age is not None and (type(age) is not int or not 0 <= age < 2**53):
        return None
    if value["state"] == "observed":
        if (
            type(value.get("transcoding")) is not bool
            or age is None
            or value.get("reason") is None
            or value["observations"] == 0
        ):
            return None
    elif value.get("transcoding") is not None or value.get("reason") is not None or age is not None:
        return None
    return {
        name: value.get(name)
        for name in ("state", "installed_before_reader", "transcoding", "reason", *fields, "age_ms")
    }


def _safe_selected_coverage(value):
    if (
        not isinstance(value, dict)
        or type(value.get("state")) is not str
        or value["state"]
        not in {
            "covered",
            "uncovered",
            "unobserved",
            "unavailable",
        }
    ):
        return None
    counts = {"current_transports": 2, "current_protocols": 16, "nominated_pairs": 4}
    booleans = ("transports_covered", "protocols_covered", "nominated_covered", "wrappers_current")
    for key, maximum in counts.items():
        item = value.get(key)
        if item is not None and (type(item) is not int or not 0 <= item <= maximum):
            return None
    if any(value.get(key) is not None and type(value[key]) is not bool for key in booleans):
        return None
    if value["state"] == "covered" and (
        any(value.get(key) is not True for key in booleans)
        or any(value.get(key) is None or value[key] == 0 for key in counts)
    ):
        return None
    return {key: value.get(key) for key in ("state", *counts, *booleans)}


def _safe_pipeline(value):
    """A fixed numeric schema; receiver snapshots cannot smuggle identities."""
    if not isinstance(value, dict) or value.get("period_ms") != 100 or value.get("capacity") != 64:
        return None

    def number(item):
        if type(item) is not int or not 0 <= item <= 2**53 - 1:
            raise ValueError("Invalid diagnostic number")
        return item

    bins = value.get("bins")
    if not isinstance(bins, list) or len(bins) > 64:
        return None
    result = {
        "period_ms": 100,
        "capacity": 64,
        "end_elapsed_ms": number(value.get("end_elapsed_ms")),
        "shared_dtls": value.get("shared_dtls") if type(value.get("shared_dtls")) is bool else None,
        "bins": [
            {
                field: number(row.get(field))
                for field in (
                    "elapsed_ms",
                    "audio_rtp",
                    "video_rtp",
                    "audio_frames",
                    "video_frames",
                )
            }
            for row in bins
        ],
    }
    metrics = value["metrics"]
    fields = ("audio_rtp", "video_rtp", "audio_frames", "video_frames")
    timings = (
        "audio_rtp_gap",
        "video_rtp_gap",
        "audio_frame_gap",
        "video_frame_gap",
        "audio_handler",
        "video_handler",
        "wakeup_lag",
    )
    bounds = StreamMetrics().snapshot()["bucket_bounds_us"]
    if metrics.get("bucket_bounds_us") != bounds:
        return None
    safe_timings = {}
    for name in timings:
        row = metrics["timings"][name]
        if not isinstance(row["buckets"], list) or len(row["buckets"]) != len(bounds) + 1:
            return None
        safe_timings[name] = {
            **{field: number(row[field]) for field in ("count", "sum_us", "max_us")},
            "buckets": [number(item) for item in row["buckets"]],
        }
    result["metrics"] = {
        "counters": {field: number(metrics["counters"][field]) for field in fields},
        "maxima": {},
        "timings": safe_timings,
        "bucket_bounds_us": bounds,
        "overflow": metrics.get("overflow") is True,
    }
    with suppress(BaseException):
        lower = _safe_lower_boundary(value.get("lower_boundary"), number, bounds)
        if lower is not None:
            result["lower_boundary"] = lower
    with suppress(BaseException):
        from halo_server.media.send_clock import safe_send_clock

        send_clock = safe_send_clock(value.get("send_clock"))
        if send_clock is not None:
            result["send_clock"] = send_clock
    return result


def _safe_lower_boundary(value, number, bounds):
    if not isinstance(value, dict):
        return None
    from halo_server.media.receiver_metrics import (
        LOWER_BIN_FIELDS,
        LOWER_COUNTERS,
        LOWER_TIMINGS,
        MAX_LOWER_CONNECTIONS,
        MAX_LOWER_PROTOCOLS,
    )

    if (
        type(value.get("installed")) is not bool
        or value.get("reason") not in {"ready", "contract_unavailable"}
        or number(value.get("connections")) > MAX_LOWER_CONNECTIONS
        or number(value.get("protocols")) > MAX_LOWER_PROTOCOLS
        or value.get("udp_drops") is not None
    ):
        return None
    bins = value.get("bins")
    metrics = value.get("metrics")
    if not isinstance(bins, list) or len(bins) > 64 or not isinstance(metrics, dict):
        return None
    if metrics.get("bucket_bounds_us") != bounds:
        return None
    timings = {}
    for name in LOWER_TIMINGS:
        row = metrics["timings"][name]
        if not isinstance(row["buckets"], list) or len(row["buckets"]) != len(bounds) + 1:
            return None
        timings[name] = {
            **{field: number(row[field]) for field in ("count", "sum_us", "max_us")},
            "buckets": [number(item) for item in row["buckets"]],
        }
    result = {
        "installed": value["installed"],
        "reason": value["reason"],
        "connections": value["connections"],
        "protocols": value["protocols"],
        "udp_drops": None,
        "bins": [
            {field: number(row[field]) for field in ("elapsed_ms", *LOWER_BIN_FIELDS)}
            for row in bins
        ],
        "metrics": {
            "counters": {name: number(metrics["counters"][name]) for name in LOWER_COUNTERS},
            "maxima": {},
            "timings": timings,
            "bucket_bounds_us": bounds,
            "overflow": metrics.get("overflow") is True,
        },
    }
    coverage = _safe_selected_coverage(value.get("selected_transport_coverage"))
    if coverage is not None:
        result["selected_transport_coverage"] = coverage
    return result
