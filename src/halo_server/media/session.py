"""One leased upstream Ring media session for one camera."""

from __future__ import annotations

import concurrent.futures
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
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
from halo_server.media.ring_media import MicrophonePcmBuffer
from halo_server.media.supervisor import supervise_receiver

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


@dataclass(frozen=True)
class _ReceiverStopRequest:
    generation: int
    future: concurrent.futures.Future


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
        self._microphone_source = MicrophonePcmBuffer()
        self._started_at: float | None = None
        self._first_video_at: float | None = None
        self._first_audio_at: float | None = None
        self._retry_initial = float(retry_initial)
        self._retry_max = float(retry_max)
        self._retry_delay = self._retry_initial
        self._retry_at: float | None = None

    @property
    def buffer(self) -> EncodedMediaBuffer:
        return self._buffer

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

    def push_intercom_generation(self, generation: int, payload: bytes) -> None:
        """Queue PCM through the short, generation-scoped microphone lock."""
        if not self._microphone_source.push_intercom(generation, payload):
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
        self._runner_generation = action.generation
        self._retry_at = None
        self._state = MediaSessionState.STARTING
        self._failure = None
        self._opus_frame_duration_ms = None
        self._started_at = self._clock()
        self._first_video_at = None
        self._first_audio_at = None
        self._buffer.clear()
        try:
            self._audio_encoder = self._audio_encoder_factory()
        except BaseException:
            self._state = MediaSessionState.FAILED
            self._failure = "audio_encoder_initialization_failed"
            raise
        self._microphone_source.clear()
        stop = threading.Event()
        self._stop = stop
        receiver_kwargs = {
            "on_encoded_video": lambda payload, timestamp: self._encoded_video(
                action.generation,
                payload,
                timestamp,
            ),
            "decode_video": False,
            "decode_audio": True,
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
        if stop is not None:
            stop.set()
        if future is None:
            self._stop = None
            self._audio_encoder = None
            self._microphone_source.clear()
            self._state = MediaSessionState.IDLE
            self._failure = None
            self._condition.notify_all()
            return None
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

    def _encoded_video(self, generation: int, payload: bytes, timestamp: int) -> None:
        with self._lock:
            if not self._accepts_media_locked(generation):
                return
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
                self._buffer.append_video(payload, timestamp)
            except MediaBufferError as exc:
                self._state = MediaSessionState.FAILED
                self._failure = "encoded_video_rejected"
                if self._stop is not None:
                    self._stop.set()
                _log.warning("Rejected encoded Ring video for %s: %s", self._device.name, exc)
                return
            self._media_recovered_locked()
            if self._state is not MediaSessionState.READY and self._buffer.ready:
                self._state = MediaSessionState.READY
                _log.info(
                    "Ring media keyframe ready camera_id=%s generation=%d elapsed=%.3f",
                    self._camera_id,
                    generation,
                    self._elapsed_locked(now),
                )

    def _raw_audio(
        self,
        generation: int,
        payload: bytes,
        channels: int,
        rate: int,
    ) -> None:
        callback = None
        with self._lock:
            if self._accepts_media_locked(generation):
                now = self._clock()
                if self._first_audio_at is None:
                    self._first_audio_at = now
                    _log.info(
                        "Ring media first audio camera_id=%s generation=%d elapsed=%.3f "
                        "bytes=%d channels=%d rate=%d",
                        self._camera_id,
                        generation,
                        self._elapsed_locked(now),
                        len(payload),
                        channels,
                        rate,
                    )
                callback = self._on_raw_audio
                try:
                    encoder = self._audio_encoder
                    if encoder is None:
                        raise AudioNormalizationError("Opus encoder is unavailable")
                    for packet in encoder.encode(payload, channels, rate):
                        self._buffer.append_audio(packet.payload, packet.rtp_timestamp)
                    self._opus_frame_duration_ms = OPUS_FRAME_DURATION_MS
                    self._media_recovered_locked()
                except (AudioNormalizationError, MediaBufferError) as exc:
                    self._state = MediaSessionState.FAILED
                    self._failure = "audio_normalization_failed"
                    if self._stop is not None:
                        self._stop.set()
                    _log.warning(
                        "Could not normalize Ring audio for %s: %s",
                        self._device.name,
                        exc,
                    )
                    callback = None
        if callback is not None:
            callback(payload, channels, rate)

    def _incompatible(self, generation: int, kind: str, mime_type: str) -> None:
        with self._lock:
            if not self._accepts_media_locked(generation):
                return
            self._state = MediaSessionState.FAILED
            self._failure = f"unsupported_{kind}_codec"
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
            was_stopping = self._state is MediaSessionState.STOPPING
            self._future = None
            self._stop = None
            self._audio_encoder = None
            self._microphone_source.clear()
            leases = self._leases.snapshot()
            if leases.upstream_requested and was_stopping:
                try:
                    self._start_locked(
                        LeaseAction(
                            LeaseActionType.START_UPSTREAM,
                            leases.generation,
                            "receiver_replacement",
                        )
                    )
                except Exception as exc:
                    self._state = MediaSessionState.FAILED
                    self._failure = "receiver_submission_failed"
                    self._schedule_retry_locked()
                    _log.warning(
                        "Could not replace stopped Ring media for camera %s: %s",
                        self._camera_id,
                        exc,
                    )
            elif leases.upstream_requested:
                self._state = MediaSessionState.FAILED
                self._failure = self._failure or "receiver_stopped"
                self._schedule_retry_locked()
            else:
                self._state = MediaSessionState.IDLE
                self._failure = None
                self._cancel_retry_locked()
            _log.info(
                "Ring media generation completed camera_id=%s generation=%d state=%s "
                "failure=%s elapsed=%.3f",
                self._camera_id,
                generation,
                self._state,
                self._failure or "none",
                self._elapsed_locked(),
            )
            self._condition.notify_all()

    def _wait_for_stop_requests(
        self,
        requests: list[_ReceiverStopRequest],
    ) -> None:
        for request in requests:
            deadline = time.monotonic() + STOP_TIMEOUT
            try:
                request.future.result(timeout=STOP_TIMEOUT)
            except concurrent.futures.TimeoutError:
                pass
            except Exception as exc:
                _log.debug("Ring media receiver stopped with an error: %s", exc)

            with self._condition:
                while self._future is request.future:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if request.generation == self._runner_generation:
                            self._state = MediaSessionState.STOPPING
                            self._failure = "receiver_stop_timeout"
                        _log.warning(
                            "Ring media receiver teardown timed out camera_id=%s generation=%d",
                            self._camera_id,
                            request.generation,
                        )
                        break
                    self._condition.wait(remaining)

    def _retry_if_due_locked(self, now: float) -> None:
        if (
            self._retry_at is None
            or now < self._retry_at
            or (self._future is not None and not self._future.done())
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
        if self._future is None:
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
