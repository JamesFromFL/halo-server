"""Recording-only source ownership and bounded, measured pre-event history.

No HAP service or warm camera is enabled by constructing this manager. Warm
capture needs explicit trusted owner and mains evidence plus recording policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from uuid import UUID

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media.buffer import BufferRead, EncodedPacket, MediaStreamKind
from halo_server.media.h264 import (
    MAX_ACCESS_UNIT_BYTES,
    MAX_SPS_BYTES,
    H264Error,
    parse_sps,
    split_annexb,
)
from halo_server.media.manager import settle_owned
from halo_server.media.opus import parse_opus

VIDEO_SECONDS = 12.0
VIDEO_BYTES = 32 * 1024 * 1024
AUDIO_SECONDS = 8.0
AUDIO_BYTES = 2 * 1024 * 1024
HISTORY_PACKETS = 4096
PREBUFFER_SECONDS = 4.0
MAX_GAP = 0.5
STOP_SECONDS = 3.0
MAX_IDENTITIES = 4096


@dataclass(frozen=True)
class RecordingSample:
    kind: str
    payload: bytes = field(repr=False)
    rtp_timestamp: int
    received_at: float
    capture_epoch: int
    config_epoch: int
    sequence: int

    def __post_init__(self):
        if (
            self.kind not in {"video", "audio"}
            or not isinstance(self.payload, bytes)
            or not 0
            < len(self.payload)
            <= (MAX_ACCESS_UNIT_BYTES if self.kind == "video" else 65535)
            or type(self.rtp_timestamp) is not int
            or self.rtp_timestamp < 0
            or type(self.received_at) not in {int, float}
            or not math.isfinite(self.received_at)
            or self.received_at < 0
            or any(
                type(v) is not int or v < 1
                for v in (self.capture_epoch, self.config_epoch, self.sequence)
            )
        ):
            raise HomeKitOperationError("invalid_sample")


@dataclass(frozen=True)
class RecordingHistorySnapshot:
    samples: tuple[RecordingSample, ...]
    capture_epoch: int
    config_epoch: int
    video_sps: bytes | None = field(repr=False)
    video_pps: bytes | None = field(repr=False)
    available_video_seconds: float
    available_audio_seconds: float
    timing_mode: str
    source_clocks: tuple
    ready: bool
    reduced_reason: str | None
    cursor: int
    event_at: float


class RecordingSourceBuffer:
    """Raw VIDEO source window; decodability is decided with frozen metadata.

    Kept separately from the legacy buffer whose read gate requires original
    SPS/PPS packets to remain in its rolling window. Payload bytes are shared.
    """

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._packets = deque()
        self._bytes = 0
        self._epoch = 1
        self._dropped_through = 0

    @property
    def capture_epoch(self):
        with self._condition:
            return self._epoch

    def clear(self, *, capture_epoch):
        with self._condition:
            self._epoch = capture_epoch
            self._dropped_through = 0
            self._packets.clear()
            self._bytes = 0
            self._condition.notify_all()

    def _prune(self, now):
        while self._packets and (
            len(self._packets) > HISTORY_PACKETS
            or self._bytes > VIDEO_BYTES
            or now - self._packets[0].received_at > VIDEO_SECONDS
        ):
            removed = self._packets.popleft()
            self._dropped_through = removed.sequence
            self._bytes -= len(removed.payload)

    def append(self, packet):
        if not isinstance(packet, EncodedPacket) or packet.stream is not MediaStreamKind.VIDEO:
            raise HomeKitOperationError("invalid_sample")
        with self._condition:
            if packet.capture_epoch < self._epoch:
                return
            if packet.capture_epoch != self._epoch:
                self.clear(capture_epoch=packet.capture_epoch)
            self._packets.append(packet)
            self._bytes += len(packet.payload)
            self._prune(packet.received_at)
            self._condition.notify_all()

    def read(self, *, after_sequence, timeout=0):
        if after_sequence is not None and (type(after_sequence) is not int or after_sequence < 0):
            raise HomeKitOperationError("invalid_request")
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 <= timeout <= 1:
            raise HomeKitOperationError("invalid_timeout")
        with self._condition:
            self._prune(self._clock())
            self._condition.wait_for(
                lambda: (
                    bool(self._packets)
                    and (after_sequence is None or self._packets[-1].sequence > after_sequence)
                ),
                timeout,
            )
            self._prune(self._clock())
            resync = after_sequence is not None and after_sequence < self._dropped_through
            return BufferRead(
                tuple(
                    p
                    for p in self._packets
                    if after_sequence is None or p.sequence > after_sequence
                ),
                resync,
                self._epoch,
            )


class RecordingHistory:
    """One scope/config epoch's real samples; source-lane duration is no promise."""

    def __init__(self, *, audio=False, clock=time.monotonic):
        self.audio, self._clock = audio, clock
        self._video, self._audio = deque(), deque()
        self._keyframes = set()
        self._video_bytes = self._audio_bytes = 0
        self.capture_epoch = self.config_epoch = 0
        self.sps = self.pps = None
        self.cursor = 0
        self._lost_through = 0
        self.source_clocks = ()
        self._source_cursors = {"video": 0, "audio": 0}
        self.reason = "warming"
        self._reorder = 0
        self._frame_ticks = None
        self._video_high_pts = None
        self._video_nonprogress = 0

    def clear(self, reason="source_retry"):
        self._lost_through = self.cursor
        self._video.clear()
        self._audio.clear()
        self._keyframes.clear()
        self._video_bytes = self._audio_bytes = 0
        self.sps = self.pps = None
        self.source_clocks = ()
        self._source_cursors = {"video": 0, "audio": 0}
        self.config_epoch += 1
        self.reason = reason
        self._video_high_pts = None
        self._video_nonprogress = 0

    def configure(self, configuration):
        if configuration is None:
            return False
        epoch, generation, sps, pps = configuration[:4]
        floor = configuration[4] if len(configuration) == 5 else 0.0
        if type(floor) not in {int, float} or not math.isfinite(floor) or floor < 0:
            raise HomeKitOperationError("invalid_configuration")
        if (
            type(epoch) is not int
            or epoch < 1
            or type(generation) is not int
            or generation < 1
            or not isinstance(sps, bytes)
            or not isinstance(pps, bytes)
            or not 0 < len(sps) <= MAX_SPS_BYTES
            or not 0 < len(pps) <= MAX_SPS_BYTES
            or sps[0] & 31 != 7
            or pps[0] & 31 != 8
        ):
            raise HomeKitOperationError("invalid_configuration")
        changed = (epoch, generation, sps, pps) != (
            self.capture_epoch,
            getattr(self, "_source_configuration", 0),
            self.sps,
            self.pps,
        )
        if changed:
            self.clear("source_retry" if epoch != self.capture_epoch else "config_changed")
            self.capture_epoch = epoch
            self._source_configuration = generation
            self.sps, self.pps = sps, pps
            self._configuration_floor = floor
            self._reorder, self._frame_ticks = 0, None
            with contextlib.suppress(H264Error):
                info = parse_sps(sps)
                if (
                    info.max_reorder_frames is not None
                    and 0 <= info.max_reorder_frames <= 16
                    and info.frame_rate is not None
                    and info.frame_rate > 0
                ):
                    self._reorder = info.max_reorder_frames
                    self._frame_ticks = 90000 / info.frame_rate
        return changed

    def _prune(self, now):
        for packets, seconds, maximum_bytes, name in (
            (self._video, VIDEO_SECONDS, VIDEO_BYTES, "_video_bytes"),
            (self._audio, AUDIO_SECONDS, AUDIO_BYTES, "_audio_bytes"),
        ):
            while packets and (
                len(packets) > HISTORY_PACKETS
                or getattr(self, name) > maximum_bytes
                or now - packets[0].received_at > seconds
            ):
                removed = packets.popleft()
                self._keyframes.discard(removed.sequence)
                self._lost_through = max(self._lost_through, removed.sequence)
                setattr(self, name, getattr(self, name) - len(removed.payload))

    def append(self, packet):
        if (
            packet.capture_epoch != self.capture_epoch
            or self.sps is None
            or self.pps is None
            or packet.received_at < getattr(self, "_configuration_floor", 0.0)
        ):
            return
        kind = packet.stream.value
        if kind not in {"video", "audio"} or kind == "audio" and not self.audio:
            return
        if packet.sequence <= self._source_cursors[kind]:
            return
        if kind == "video":
            keyframe = any(nal[0] & 31 == 5 for nal in split_annexb(packet.payload))
        else:
            parse_opus(packet.payload)
        self._source_cursors[kind] = packet.sequence
        previous = self._video if kind == "video" else self._audio
        discontinuity = False
        if previous:
            delta = packet.rtp_timestamp - previous[-1].rtp_timestamp
            discontinuity = (
                packet.received_at < previous[-1].received_at
                or packet.received_at - previous[-1].received_at > MAX_GAP
            )
            if kind == "audio":
                discontinuity |= delta != parse_opus(previous[-1].payload).duration_samples
            else:
                high = self._video_high_pts
                if high is None or packet.rtp_timestamp > high:
                    discontinuity |= (
                        high is not None and packet.rtp_timestamp - high > MAX_GAP * 90000
                    )
                    self._video_nonprogress = 0
                else:
                    self._video_nonprogress += 1
                    discontinuity |= (
                        delta == 0
                        or self._frame_ticks is None
                        or self._video_nonprogress > self._reorder
                        or high - packet.rtp_timestamp > self._reorder * self._frame_ticks
                    )
        if discontinuity:
            # Never bridge a same-epoch source stall with old samples.
            sps, pps, epoch, generation = (
                self.sps,
                self.pps,
                self.capture_epoch,
                self._source_configuration,
            )
            self.clear("source_discontinuity")
            self.capture_epoch, self._source_configuration, self.sps, self.pps = (
                epoch,
                generation,
                sps,
                pps,
            )
            previous = self._video if kind == "video" else self._audio
            self._source_cursors[kind] = packet.sequence
        if kind == "video":
            self._video_high_pts = max(
                self._video_high_pts or packet.rtp_timestamp, packet.rtp_timestamp
            )
        self.cursor += 1
        sample = RecordingSample(
            kind,
            packet.payload,
            packet.rtp_timestamp,
            packet.received_at,
            self.capture_epoch,
            self.config_epoch,
            self.cursor,
        )
        previous.append(sample)
        if kind == "video" and keyframe:
            self._keyframes.add(sample.sequence)
        name = "_video_bytes" if kind == "video" else "_audio_bytes"
        setattr(self, name, getattr(self, name) + len(sample.payload))
        self._prune(self._clock())

    def seed(self, other):
        self.capture_epoch, self.config_epoch = other.capture_epoch, other.config_epoch
        self._source_configuration = getattr(other, "_source_configuration", 0)
        self.sps, self.pps = other.sps, other.pps
        self._configuration_floor = getattr(other, "_configuration_floor", 0.0)
        self._source_cursors = dict(other._source_cursors)
        self._reorder, self._frame_ticks = other._reorder, other._frame_ticks
        self._video_high_pts, self._video_nonprogress = (
            other._video_high_pts,
            other._video_nonprogress,
        )
        self.source_clocks = tuple(other.source_clocks)
        for sample in sorted((*other._video, *other._audio), key=lambda item: item.sequence):
            if sample.kind == "audio" and not self.audio:
                continue
            self.cursor += 1
            if sample.sequence in other._keyframes:
                self._keyframes.add(self.cursor)
            sample = replace(sample, sequence=self.cursor)
            (self._video if sample.kind == "video" else self._audio).append(sample)
            name = "_video_bytes" if sample.kind == "video" else "_audio_bytes"
            setattr(self, name, getattr(self, name) + len(sample.payload))
        self._prune(self._clock())

    def snapshot(self, *, event_at, after_sequence=None):
        self._prune(self._clock())
        boundary = None
        idrs = [sample for sample in self._video if sample.sequence in self._keyframes]
        earlier = [sample for sample in idrs if sample.received_at <= event_at - PREBUFFER_SECONDS]
        if earlier:
            boundary = earlier[-1]
        elif idrs:
            boundary = idrs[0]
        video = tuple(
            s for s in self._video if boundary is not None and s.sequence >= boundary.sequence
        )
        start = boundary.received_at if boundary is not None else event_at
        audio = tuple(s for s in self._audio if s.received_at >= start) if self.audio else ()
        video_seconds = max(0.0, min(event_at, video[-1].received_at) - start) if video else 0.0
        audio_seconds = (
            max(0.0, min(event_at, audio[-1].received_at) - audio[0].received_at) if audio else 0.0
        )
        # Both receipt and advancing encoded media clocks must span history.
        video_pts = (
            (max(s.rtp_timestamp for s in video) - min(s.rtp_timestamp for s in video)) / 90000
            if video
            else 0.0
        )
        audio_pts = (audio[-1].rtp_timestamp - audio[0].rtp_timestamp) / 48000 if audio else 0.0
        video_seconds, audio_seconds = min(video_seconds, video_pts), min(audio_seconds, audio_pts)
        recent = bool(video) and 0 <= self._clock() - video[-1].received_at <= MAX_GAP
        ready = (
            video_seconds >= PREBUFFER_SECONDS
            and recent
            and (
                not self.audio
                or audio_seconds >= PREBUFFER_SECONDS
                and 0 <= self._clock() - audio[-1].received_at <= MAX_GAP
            )
        )
        reason = (
            None
            if ready
            else (
                "missing_keyframe"
                if not video
                else "source_stalled"
                if not recent
                else "audio_history_short"
                if self.audio and audio_seconds < PREBUFFER_SECONDS
                else "cold_reduced_history"
            )
        )
        samples = tuple(sorted((*video, *audio), key=lambda item: item.sequence))
        if after_sequence is not None:
            samples = tuple(sample for sample in samples if sample.sequence > after_sequence)
        clocks = tuple(
            item for item in self.source_clocks if item.capture_epoch == self.capture_epoch
        )
        mode = (
            "source_sender_report"
            if {item.kind for item in clocks} >= ({"video", "audio"} if self.audio else {"video"})
            else "receipt_estimate"
        )
        return RecordingHistorySnapshot(
            samples,
            self.capture_epoch,
            self.config_epoch,
            self.sps,
            self.pps,
            video_seconds,
            audio_seconds,
            mode,
            clocks,
            ready,
            reason,
            self.cursor,
            event_at,
        )

    def raw_since(self, after_sequence):
        self._prune(self._clock())
        if type(after_sequence) is not int or not 0 <= after_sequence <= self.cursor:
            raise HomeKitOperationError("invalid_request")
        if after_sequence < self._lost_through:
            raise HomeKitOperationError("history_lost")
        return tuple(
            sorted(
                (
                    sample
                    for sample in (*self._video, *self._audio)
                    if sample.sequence > after_sequence
                ),
                key=lambda item: item.sequence,
            )
        )


class RecordingCapture:
    def __init__(self, manager, boot, scope, identity, *, audio, warm):
        self.manager, self.boot, self.scope, self.session_id = manager, boot, scope, identity
        self.audio, self.warm = audio, warm
        self.opened_at = manager._clock()
        self.history = RecordingHistory(audio=audio, clock=manager._clock)
        self.handle = self.acquire_task = self.collector = None
        self.retired = self.cleanup_failed = False
        self.failure = None
        self.changed = asyncio.Event()
        self._closing = asyncio.Lock()

    def _authorize(self):
        if self.retired:
            raise HomeKitOperationError("cancelled")
        self.manager.controller.authorize(self.boot, self.scope, "recording")
        if self.audio:
            self.manager.controller.authorize(self.boot, self.scope, "recording_audio")
        if self.handle is not None and not self.handle.is_current():
            raise HomeKitOperationError("authority_changed")

    @property
    def current_epochs(self):
        """Cheap actual source identity fence, including not-yet-collected resets."""
        self._authorize()
        epoch = self.handle.capture_epoch if self.handle is not None else self.history.capture_epoch
        if epoch != self.history.capture_epoch:
            return epoch, self.history.config_epoch + 1
        configuration = self.handle.video_configuration if self.handle is not None else None
        final_epoch = self.handle.capture_epoch if self.handle is not None else epoch
        config_epoch = self.history.config_epoch
        if (
            final_epoch != epoch
            or configuration is not None
            and configuration[0] != final_epoch
            or configuration is None
            and self.history.sps is not None
            or configuration is not None
            and (
                configuration[1] != getattr(self.history, "_source_configuration", 0)
                or configuration[2:4] != (self.history.sps, self.history.pps)
            )
        ):
            config_epoch += 1
        return final_epoch, config_epoch

    @property
    def current_cursor(self):
        self._authorize()
        return self.history.cursor

    def snapshot(self, *, event_at=None):
        self._authorize()
        result = self.history.snapshot(
            event_at=(self.manager._clock() if self.warm else self.opened_at)
            if event_at is None
            else event_at
        )
        epochs = self.current_epochs
        if epochs != (result.capture_epoch, result.config_epoch):
            # The source can clear or change headers while the collector is
            # suspended. Never expose that collector's old sample references.
            return replace(
                result,
                samples=(),
                capture_epoch=epochs[0],
                config_epoch=epochs[1],
                video_sps=None,
                video_pps=None,
                source_clocks=(),
                ready=False,
                available_video_seconds=0.0,
                available_audio_seconds=0.0,
                reduced_reason="source_changed",
                timing_mode="receipt_estimate",
            )
        return result

    async def read_since(self, after_sequence, *, timeout=0.25):
        if after_sequence is not None and (type(after_sequence) is not int or after_sequence < 0):
            raise HomeKitOperationError("invalid_request")
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 <= timeout <= 1:
            raise HomeKitOperationError("invalid_timeout")
        self._authorize()
        if after_sequence is not None and self.history.cursor <= after_sequence:
            self.changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), timeout)
        result = self.snapshot()
        if self.failure is not None:
            raise HomeKitOperationError(self.failure)
        if (result.capture_epoch, result.config_epoch) != (
            self.history.capture_epoch,
            self.history.config_epoch,
        ):
            return result
        return (
            result
            if after_sequence is None
            else replace(result, samples=self.history.raw_since(after_sequence))
        )

    async def _collect(self):
        renewed = self.manager._clock()
        try:
            while not self.retired:
                self._authorize()
                video = await self.handle.read(timeout=0.1)
                audio = await self.handle.read_audio(timeout=0) if self.audio else None
                self._authorize()
                epoch = self.handle.capture_epoch
                if video.resynchronized or audio is not None and audio.resynchronized:
                    self.history.clear("source_history_lost")
                configuration = self.handle.video_configuration
                if configuration is None or configuration[0] != epoch:
                    self.history.clear("missing_configuration")
                    self.history.capture_epoch = epoch
                    self.changed.set()
                    continue
                self.history.configure(configuration)
                packets = tuple(video.packets) + (tuple(audio.packets) if audio is not None else ())
                for packet in sorted(
                    packets, key=lambda item: (item.received_at, item.stream.value, item.sequence)
                ):
                    if packet.capture_epoch == epoch:
                        self.history.append(packet)
                self.history.source_clocks = tuple(self.handle.source_clocks)
                self.changed.set()
                if self.manager._clock() - renewed >= 5:
                    if not await self.handle.renew():
                        raise HomeKitOperationError("expired")
                    renewed = self.manager._clock()
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except (HomeKitOperationError, H264Error) as error:
            self.failure = getattr(error, "code", "source_failed")
            self.changed.set()
        except Exception:
            self.failure = "source_failed"
            self.changed.set()
        finally:
            if not self.retired:
                self.retired = True
                self.history.clear("source_failed")
                self.changed.set()
                self.manager._start_retirement(self)

    async def close(self):
        self.retired = True
        self.history.clear("cancelled")
        self.changed.set()
        task = self.manager._start_retirement(self)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await settle_owned(task)
            raise


class RecordingCaptureManager:
    def __init__(self, controller, *, clock=time.monotonic):
        self.controller, self._clock = controller, clock
        self._captures = {}
        self._history = OrderedDict()
        self._closed = False

    @property
    def pending_resources(self):
        return len(self._captures)

    @property
    def cleanup_failures(self):
        return sum(capture.cleanup_failed for capture in self._captures.values())

    def reopen(self):
        """Rearm a completely retired owner without forgetting replay history."""
        if self._captures:
            raise HomeKitOperationError("cleanup_pending")
        self._closed = False

    async def open(
        self,
        boot,
        scope,
        session_id,
        *,
        audio=False,
        warm=False,
        owner_opt_in=False,
        mains_confirmed=False,
    ):
        try:
            valid_id = isinstance(session_id, str) and str(UUID(session_id)) == session_id
        except ValueError:
            valid_id = False
        if (
            self._closed
            or not valid_id
            or any(
                type(value) is not bool for value in (audio, warm, owner_opt_in, mains_confirmed)
            )
        ):
            raise HomeKitOperationError("invalid_request")
        if warm and not (owner_opt_in and mains_confirmed):
            raise HomeKitOperationError("warm_not_authorized")
        self.controller.authorize(boot, scope, "recording")
        if audio:
            self.controller.authorize(boot, scope, "recording_audio")
        identity = boot, session_id
        now = self._clock()
        self._history = OrderedDict(
            (key, value) for key, value in self._history.items() if value > now
        )
        if identity in self._captures or identity in self._history:
            raise HomeKitOperationError("session_conflict")
        same_kind = tuple(c for c in self._captures.values() if c.warm == warm)
        if (
            len(self._history) + len(self._captures) >= MAX_IDENTITIES
            or len(same_kind) >= 2
            or any(c.scope.source_key == scope.source_key for c in same_kind)
        ):
            raise HomeKitOperationError("busy")
        capture = RecordingCapture(self, boot, scope, session_id, audio=audio, warm=warm)
        self._captures[identity] = capture
        capture.acquire_task = asyncio.create_task(
            self.controller.open_recording(boot, scope, session_id=session_id, warm=warm)
        )
        capture.acquire_task.add_done_callback(_observed)
        try:
            capture.handle = await asyncio.shield(capture.acquire_task)
            capture._authorize()
            if not warm:
                source = next(
                    (
                        other
                        for other in self._captures.values()
                        if other is not capture
                        and other.warm
                        and other.handle is not None
                        and not other.retired
                        and other.boot == boot
                        and other.scope == scope
                        and (not audio or other.audio)
                    ),
                    None,
                )
                if source is not None:
                    with contextlib.suppress(HomeKitOperationError):
                        if (
                            source.current_epochs
                            == (source.history.capture_epoch, source.history.config_epoch)
                            and source.handle.capture_epoch == capture.handle.capture_epoch
                        ):
                            capture.history.seed(source.history)
            capture.collector = asyncio.create_task(
                capture._collect(), name="homekit-recording-capture"
            )
            capture.collector.add_done_callback(_observed)
            return capture
        except BaseException:
            with contextlib.suppress(Exception):
                await capture.close()
            raise

    def _start_retirement(self, capture):
        task = getattr(capture, "_retirement", None)
        if task is None or task.done() and (task.cancelled() or task.exception() is not None):
            task = asyncio.create_task(
                self._retire_capture(capture), name="homekit-recording-capture-close"
            )
            task.add_done_callback(_observed)
            capture._retirement = task
        return task

    async def _retire_capture(self, capture):
        async with capture._closing:
            try:
                tasks = tuple(
                    task
                    for task in (capture.collector, capture.acquire_task)
                    if task is not None and task is not asyncio.current_task() and not task.done()
                )
                for task in tasks:
                    task.cancel()
                if tasks:
                    _, pending = await asyncio.wait(tasks, timeout=STOP_SECONDS)
                    if pending:
                        raise HomeKitOperationError("cleanup_pending")
                if (
                    capture.handle is None
                    and capture.acquire_task is not None
                    and not capture.acquire_task.cancelled()
                ):
                    with contextlib.suppress(Exception):
                        capture.handle = capture.acquire_task.result()
                await self.controller.retire_recording(
                    capture.boot, capture.scope, capture.session_id
                )
            except BaseException:
                capture.cleanup_failed = True
                raise
            capture.cleanup_failed = False
            self._captures.pop((capture.boot, capture.session_id), None)
            self._history[capture.boot, capture.session_id] = self._clock() + 60

    async def refresh(self):
        failures = []
        for capture in tuple(self._captures.values()):
            try:
                capture._authorize()
            except HomeKitOperationError:
                try:
                    await capture.close()
                except Exception as error:
                    failures.append(error)
        if failures:
            raise ExceptionGroup("Recording capture cleanup pending", failures)

    async def retired(self, boot):
        captures = tuple(c for c in self._captures.values() if boot is None or c.boot == boot)
        for capture in captures:
            capture.retired = True
            capture.history.clear("cancelled")
            capture.changed.set()
        results = await asyncio.gather(
            *(capture.close() for capture in captures), return_exceptions=True
        )
        errors = [error for error in results if isinstance(error, Exception)]
        if errors:
            raise ExceptionGroup("Recording capture retirement incomplete", errors)

    async def close(self):
        self._closed = True
        await self.retired(None)


def _observed(task):
    if not task.cancelled():
        task.exception()
