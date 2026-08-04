"""One leased upstream Ring media session for one camera."""

from __future__ import annotations

import concurrent.futures
import logging
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


class MediaSessionState(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    READY = "ready"
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
    ) -> None:
        self._client = client
        self._device = device
        self._camera_id = str(device.id)
        self._leases = lease_coordinator or MediaLeaseCoordinator()
        self._buffer = buffer or EncodedMediaBuffer()
        self._receiver_supervisor = receiver_supervisor
        self._on_raw_audio = on_raw_audio
        self._audio_encoder_factory = audio_encoder_factory
        self._lock = threading.RLock()
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
        with self._lock:
            intercom_was_active = self._intercom_active_locked()
            lease, actions = self._leases.acquire(
                purpose,
                ttl=ttl,
                lease_id=lease_id,
                now=now,
            )
            try:
                self._apply_actions_locked(actions)
            except BaseException:
                failure = self._failure
                self._apply_actions_locked(self._leases.release(lease.id, now=now))
                self._state = MediaSessionState.FAILED
                self._failure = failure or "receiver_submission_failed"
                raise
            if purpose is LeasePurpose.INTERCOM and not intercom_was_active:
                intercom_generation = self._microphone_source.begin_intercom()
                _log.info(
                    "Ring intercom lease opened camera_id=%s generation=%d intercom_generation=%d",
                    self._camera_id,
                    lease.generation,
                    intercom_generation,
                )
            return lease

    def renew(
        self,
        lease_id: str,
        *,
        ttl: float | None = None,
        now: float | None = None,
    ) -> MediaLease | None:
        with self._lock:
            lease, actions = self._leases.renew(lease_id, ttl=ttl, now=now)
            self._apply_actions_locked(actions)
            return lease

    def release(self, lease_id: str, *, now: float | None = None) -> None:
        with self._lock:
            intercom_was_active = self._intercom_active_locked()
            self._apply_actions_locked(self._leases.release(lease_id, now=now))
            self._finish_intercom_if_inactive_locked(intercom_was_active)

    def expire(self, *, now: float | None = None) -> None:
        with self._lock:
            intercom_was_active = self._intercom_active_locked()
            self._apply_actions_locked(self._leases.expire(now=now))
            self._finish_intercom_if_inactive_locked(intercom_was_active)

    def stop_all(self, reason: str = "service_stop") -> None:
        with self._lock:
            intercom_was_active = self._intercom_active_locked()
            self._apply_actions_locked(self._leases.stop_all(reason))
            self._finish_intercom_if_inactive_locked(intercom_was_active)

    def push_intercom_audio(self, lease_id: str, payload: bytes) -> None:
        """Queue one normalized PCM frame for an active intercom lease."""
        with self._lock:
            lease = next(
                (
                    candidate
                    for candidate in self._leases.snapshot().leases
                    if candidate.id == lease_id
                ),
                None,
            )
            if lease is None or lease.purpose is not LeasePurpose.INTERCOM:
                raise RuntimeError("intercom lease is not active")
            self._microphone_source.push(payload)
            if self._microphone_source.snapshot().accepted_frames == 1:
                _log.info(
                    "Ring intercom first PCM frame accepted camera_id=%s generation=%d bytes=%d",
                    self._camera_id,
                    self._runner_generation,
                    len(payload),
                )

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

    def _apply_actions_locked(self, actions: list[LeaseAction]) -> None:
        for action in actions:
            if action.type is LeaseActionType.START_UPSTREAM:
                self._start_locked(action)
            elif action.type is LeaseActionType.STOP_UPSTREAM:
                self._stop_locked(action)

    def _start_locked(self, action: LeaseAction) -> None:
        if self._future is not None and not self._future.done():
            return
        self._runner_generation = action.generation
        self._state = MediaSessionState.STARTING
        self._failure = None
        self._opus_frame_duration_ms = None
        self._started_at = time.monotonic()
        self._first_video_at = None
        self._first_audio_at = None
        self._buffer.clear()
        self._audio_encoder = self._audio_encoder_factory()
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
        try:
            future = self._client.submit(coroutine)
        except BaseException:
            coroutine.close()
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

    def _stop_locked(self, action: LeaseAction) -> None:
        if action.generation != self._runner_generation:
            return
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
        self._stop = None
        if stop is not None:
            stop.set()
        if future is not None and not future.done():
            try:
                future.result(timeout=STOP_TIMEOUT)
            except concurrent.futures.TimeoutError:
                future.cancel()
            except Exception as exc:
                _log.debug("Ring media receiver stopped with an error: %s", exc)
        if action.generation == self._runner_generation:
            self._future = None
            self._audio_encoder = None
            self._microphone_source.clear()
            self._state = MediaSessionState.IDLE
            self._failure = None

    def _intercom_active_locked(self) -> bool:
        return any(
            lease.purpose is LeasePurpose.INTERCOM for lease in self._leases.snapshot().leases
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
            now = time.monotonic()
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
                self._failure = "encoded_video_rejected"
                _log.warning("Rejected encoded Ring video for %s: %s", self._device.name, exc)
                return
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
                now = time.monotonic()
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
        with self._lock:
            if generation != self._runner_generation or future is not self._future:
                return
            self._future = None
            self._stop = None
            self._audio_encoder = None
            if self._leases.snapshot().upstream_requested:
                self._state = MediaSessionState.FAILED
                self._failure = self._failure or "receiver_stopped"
            else:
                self._state = MediaSessionState.IDLE
                self._failure = None
            _log.info(
                "Ring media generation completed camera_id=%s generation=%d state=%s "
                "failure=%s elapsed=%.3f",
                self._camera_id,
                generation,
                self._state,
                self._failure or "none",
                self._elapsed_locked(),
            )

    def _accepts_media_locked(self, generation: int) -> bool:
        return (
            generation == self._runner_generation
            and self._stop is not None
            and not self._stop.is_set()
        )

    def _elapsed_locked(self, now: float | None = None) -> float:
        if self._started_at is None:
            return 0.0
        return max(0.0, (time.monotonic() if now is None else now) - self._started_at)
