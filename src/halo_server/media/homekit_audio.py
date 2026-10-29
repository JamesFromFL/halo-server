"""Negotiated audio endpoints owned by one live camera output session."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import socket
import struct
import time
from collections import deque
from dataclasses import dataclass, field

from aiortc.rtp import RtpPacket

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media.live_timeline import LiveTimeline
from halo_server.media.rtp import ReceiverReportStats, RtpError, parse_feedback, sender_report
from halo_server.media.srtp import SecureVideo, SrtpError
from halo_server.media.talkback import Talkback, TalkPacket
from halo_server.media.telemetry import StreamMetrics

# Maximum retained source contribution, not an intentional playout delay.
# Ring arrivals can pause for about one second; keep the short native tail
# through that gap without recreating the converter. Queue count/byte bounds
# and immediate permission/epoch retirement remain independent.
MAX_AUDIO_AGE = 1.2
MAX_AUDIO_PACKET = 1200
RECEIVER_SECONDS = 60.0
SEND_SECONDS = 0.25
MAX_DATAGRAM_RATE = 128
MAX_INPUT_SPANS = 512
# Queue membership includes the packet currently owned by the sender. Reserve
# response slots before conversion: queued + native response never exceeds32.
# Payloads plus one plain/protected pair stay below40KiB at the existing cap.
MAX_QUEUED_OUTPUTS = 32
MAX_NATIVE_OUTPUTS = 16
MAX_RETAINED_OUTPUT_BYTES = 40 * 1024
# Preserve nominal pacing for ordinary batches. A backlog can use at most twice
# its negotiated packet rate (100pps at20ms), with finite packet/wire credit.
MAX_CATCHUP_DATAGRAM_RATE = 100
MAX_PACING_PACKET_CREDIT = 3
CATCHUP_QUEUE_SAMPLES = 5760  # One maximum120ms Opus packet, in the48kHz clock.
CATCHUP_AGE = 0.1
_LOGGER = logging.getLogger(__name__)
_COUNTER_MAX = 2**31 - 1
_SAMPLE_BINS = (120, 240, 480, 960, 1440, 1920, 2880, 5760)
_SOURCE_CATEGORIES = (
    "source_read",
    "source_submitted",
    "source_initial_skipped",
    "source_recovery_skipped",
    "source_prefix_discarded",
    "source_stale_discarded",
    "source_epoch_discarded",
    "source_authority_discarded",
    "source_unprocessed",
)
_OUTPUT_CATEGORIES = (
    "output_generated",
    "output_iterated",
    "output_sent",
    "output_stale",
    "output_epoch",
    "output_send_fenced",
    "output_unvisited",
    "output_unfinished",
    "output_queued",
    "output_overflow",
    "output_retired",
    "output_budget_rejected",
)


class _AudioLedger:
    """One existing batch's integer durations; never payloads or native state."""

    def __init__(self, durations, metric, *, output=False):
        self.durations = durations
        self.metric = metric
        self.output = output
        self.done = self.visited = 0
        self.total("output_generated" if output else "source_read")

    def total(self, category):
        self.metric("count", f"{category}_packets", len(self.durations))
        self.metric("count", f"{category}_samples", sum(self.durations))

    def visit(self, index):
        self.visited |= 1 << index
        if self.output:
            self.metric("count", "output_iterated_packets")
            self.metric("count", "output_iterated_samples", self.durations[index])

    def record(self, index, category):
        bit = 1 << index
        if not self.done & bit:
            self.done |= bit
            self.metric("count", f"{category}_packets")
            self.metric("count", f"{category}_samples", self.durations[index])

    def remaining(self, category=None):
        for index in range(len(self.durations)):
            name = category
            if name is None:
                name = (
                    ("output_unfinished" if self.visited & (1 << index) else "output_unvisited")
                    if self.output
                    else "source_unprocessed"
                )
            self.record(index, name)


class _AudioPacer:
    """Bounded RTP packet and IP/UDP/SRTP wire credit; never media timestamps."""

    def __init__(self, config, family, now):
        nominal_rate = 1000 / config.packet_time
        self.packet_rate = min(MAX_CATCHUP_DATAGRAM_RATE, 2 * nominal_rate)
        self.network_header = 48 if family == socket.AF_INET6 else 28
        # The fixed RTP header and SRTP tag use22bytes. Header cost applies to
        # the negotiated packet cadence, in addition to its media bitrate.
        self.wire_rate = 2 * (config.max_bit_rate * 125 + (22 + self.network_header) * nominal_rate)
        self.wire_capacity = MAX_PACING_PACKET_CREDIT * (MAX_AUDIO_PACKET + self.network_header)
        self._packets = float(MAX_PACING_PACKET_CREDIT)
        self._bytes = float(self.wire_capacity)
        self._at = now

    def _refresh(self, now):
        elapsed = max(0, now - self._at)
        self._packets = min(MAX_PACING_PACKET_CREDIT, self._packets + elapsed * self.packet_rate)
        self._bytes = min(self.wire_capacity, self._bytes + elapsed * self.wire_rate)
        self._at = now

    def delay(self, size, now):
        self._refresh(now)
        return max(0, (1 - self._packets) / self.packet_rate, (size - self._bytes) / self.wire_rate)

    def sent(self, size, now):
        self._refresh(now)
        # Only the single owned egress task spends this credit, after a real
        # successful send. A blocked/fenced send cannot manufacture extra credit.
        self._packets -= 1
        self._bytes -= size


class _AudioTiming:
    """Constant-size metadata only; no payloads, absolute timestamps or identities."""

    def __init__(self, config, now):
        self.started = now
        self.next_snapshot = 0
        self.finished = False
        self.previous_input = self.previous_output = self.previous_wire = None
        self.values = {
            "sample_rate": config.sample_rate,
            "packet_ms": config.packet_time,
            "channels": config.channels,
            "bitrate_kbps": config.max_bit_rate,
            "bitrate_mode": config.bitrate_mode,
        }
        for name in [
            "input_packets",
            "input_bytes",
            "invalid_toc",
            "input_step_mismatch",
            "input_backward",
            "input_step_max",
            "input_gap_max",
            "epoch_resets",
            "adapter_recreates",
            "feed_calls",
            "feed_latency_ms_max",
            "output_packets",
            "output_pts_mismatch",
            "output_backward",
            "output_gap_max",
            "stale_input",
            "stale_output",
            "epoch_drop",
            "source_age_ms_max",
            "sent_packets",
            "sent_bytes",
            "wire_step_mismatch",
            "wire_step_max",
            "pacing_late_ms_max",
            "presentation_age_ms_max",
            "rtcp_sent",
            "mode_copy",
            "mode_repacketize",
            "mode_adapt",
            "mode_other",
            "live_edge_skipped",
        ]:
            self.values[name] = 0
        for prefix, bins in (
            ("duration", _SAMPLE_BINS),
            ("frame", _SAMPLE_BINS),
            ("channel", (1, 2)),
            ("bandwidth", (8000, 12000, 16000, 24000, 48000)),
        ):
            for value in (*bins, "other"):
                self.values[f"{prefix}_{value}"] = 0

    def count(self, key, amount=1):
        self.values[key] = min(_COUNTER_MAX, self.values[key] + max(0, amount))

    def maximum(self, key, value):
        self.values[key] = min(_COUNTER_MAX, max(self.values[key], max(0, int(value))))

    def source(self, packet, now):
        from halo_server.media.opus import parse_opus

        self.count("input_packets")
        self.count("input_bytes", len(packet.payload))
        self.maximum("source_age_ms_max", (now - packet.received_at) * 1000)
        try:
            parsed = parse_opus(packet.payload)
        except Exception:
            self.count("invalid_toc")
            return
        for prefix, value in (
            ("duration", parsed.duration_samples),
            ("frame", parsed.frame_samples),
            ("channel", parsed.channels),
            ("bandwidth", parsed.bandwidth_rate),
        ):
            key = f"{prefix}_{value}"
            self.count(key if key in self.values else f"{prefix}_other")
        if self.previous_input is not None:
            timestamp, duration = self.previous_input
            step = packet.rtp_timestamp - timestamp
            if step != duration:
                self.count("input_step_mismatch")
            if step <= 0:
                self.count("input_backward")
            self.maximum("input_step_max", step)
            self.maximum("input_gap_max", abs(step - duration))
        self.previous_input = packet.rtp_timestamp, parsed.duration_samples

    def reset(self):
        self.count("epoch_resets")
        self.previous_input = self.previous_output = None

    def feed(self, seconds):
        self.count("feed_calls")
        self.maximum("feed_latency_ms_max", seconds * 1000)

    def output(self, output):
        self.count("output_packets")
        mode = output.mode if output.mode in {"copy", "repacketize", "adapt"} else "other"
        self.count(f"mode_{mode}")
        if self.previous_output is not None:
            timestamp, duration = self.previous_output
            step = output.timestamp - timestamp
            if step != duration:
                self.count("output_pts_mismatch")
            if step <= 0:
                self.count("output_backward")
            self.maximum("output_gap_max", abs(step - duration))
        self.previous_output = output.timestamp, output.duration

    def sent(self, timestamp, duration, size, now, presentation):
        self.count("sent_packets")
        self.count("sent_bytes", size)
        if self.previous_wire is not None:
            previous, expected = self.previous_wire
            step = (timestamp - previous) & 0xFFFFFFFF
            if step != expected:
                self.count("wire_step_mismatch")
            self.maximum("wire_step_max", step)
        self.previous_wire = timestamp, duration
        self.maximum("presentation_age_ms_max", (now - presentation) * 1000)

    def tick(self, now):
        thresholds = (5, 15, 30)
        if not self.finished and self.next_snapshot < len(thresholds):
            threshold = thresholds[self.next_snapshot]
            if now - self.started >= threshold:
                self.next_snapshot += 1
                self.emit(f"{threshold}s", now)

    def finish(self, now):
        if not self.finished:
            self.finished = True
            self.emit("terminal", now)

    def emit(self, phase, now):
        values = dict(self.values)
        values["elapsed_ms"] = min(_COUNTER_MAX, max(0, int((now - self.started) * 1000)))
        _LOGGER.info(
            "HomeKit audio timing phase=%s metrics=%s",
            phase,
            json.dumps(values, sort_keys=True, separators=(",", ":")),
        )


@dataclass(frozen=True)
class AudioEndpointRequest:
    target_port: int
    key: bytes = field(repr=False)
    salt: bytes = field(repr=False)

    def __post_init__(self):
        if (
            type(self.target_port) is not int
            or not 1 <= self.target_port <= 65535
            or not isinstance(self.key, bytes)
            or len(self.key) != 16
            or not isinstance(self.salt, bytes)
            or len(self.salt) != 14
        ):
            raise HomeKitOperationError("invalid_request")


@dataclass(frozen=True)
class PreparedAudio:
    port: int
    ssrc: int
    key: bytes = field(repr=False)
    salt: bytes = field(repr=False)


@dataclass(eq=False)
class _QueuedAudio:
    output: object = field(repr=False)
    received_at: float
    epoch: int
    generation: int
    outcome: str | None = None


class AudioTrack:
    """No source lease: both directions use their parent session's live handle."""

    def __init__(
        self,
        request,
        *,
        bind,
        endpoint,
        family,
        excluded_ssrc,
        check,
        epoch,
        controller,
        boot,
        scope,
        failure,
        touch_feedback,
        last_feedback,
        clock=time.monotonic,
        presentation=None,
        adapter_factory=None,
        decoder_factory=None,
        diagnostic_failure=None,
    ):
        self.request = request
        self._check, self._epoch, self._failure = check, epoch, failure
        self._clock = clock
        # Older internal callers may still supply the constructor keyword;
        # the shared source timeline now owns all presentation correlation.
        self._timeline = None
        self._touch_feedback, self._last_seen = touch_feedback, last_feedback
        self._adapter_factory, self._decoder_factory = adapter_factory, decoder_factory
        self._diagnostic_failure = diagnostic_failure
        self._controller, self._boot, self._scope = controller, boot, scope
        self._retired = False
        self.socket = socket.socket(family, socket.SOCK_DGRAM)
        self.secure = None
        try:
            self.socket.setblocking(False)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
            self.socket.bind(bind)
            self.socket.connect(endpoint)
            ssrc = secrets.randbelow(0xFFFFFFFF) + 1
            while ssrc == excluded_ssrc:
                ssrc = secrets.randbelow(0xFFFFFFFF) + 1
            self.secure = SecureVideo(request.key, request.salt, ssrc)
            self.prepared = PreparedAudio(
                self.socket.getsockname()[1], ssrc, request.key, request.salt
            )
        except BaseException:
            self.socket.close()
            if self.secure is not None:
                self.secure.close()
            raise
        self._tasks = []
        self._output_tasks = []
        self._adapter = None
        self.talkback = None
        self.config = None
        self.handle = None
        self._sequence = secrets.randbelow(65536)
        self._base = secrets.randbits(32)
        self._last_timestamp = None
        self._last_duration = 0
        self._last_presentation = self._clock()
        self._last_feedback = self._clock()
        self._packets = self._octets = 0
        self._next_packet_at = 0.0
        self._pacing_family = family
        self._pacer = None
        self._capture_epoch = None
        self._input_spans = deque()
        self._input_end = None
        self._live_edge_pending = True
        self._submitted_any = False
        self._outputs = deque()
        self._output_bytes = 0
        self._output_ready = asyncio.Event()
        self._output_space = asyncio.Event()
        self._sending = None
        self._delivery_generation = 0
        self._permission_paused = False
        self._audio_allowed_after = float("-inf")
        self._timing = None
        self._receiver_reports = None
        self._metrics = StreamMetrics(
            counters=(
                "read_batches",
                "source_invalid_packets",
                "adapter_recreates",
                "permission_pauses",
                "catchup_packets",
                *(
                    f"{name}_{unit}"
                    for name in (*_SOURCE_CATEGORIES, *_OUTPUT_CATEGORIES)
                    for unit in ("packets", "samples")
                ),
            ),
            maxima=(
                "batch_packets",
                "batch_samples",
                "output_batch_packets",
                "input_spans",
                "queued_packets",
                "queued_bytes",
                "retained_output_packets",
                "retained_output_bytes",
            ),
            timings=(
                "read_wait",
                "feed",
                "retirement",
                "authority",
                "epoch_getter",
                "presentation",
                "source_age",
                "output_age",
                "pacing_wait",
                "pacing_late",
                "output_backpressure",
            ),
        )
        self.failure = None

    def start(self, handle, config, *, speaker_volume, timeline=None):
        from halo_server.media.homekit_audio_codec import AudioAdapter

        self.handle, self.config = handle, config
        self._timeline = timeline or self._new_timeline()
        self._receiver_reports = ReceiverReportStats(clock_rate=config.sample_rate)
        self._timing = _AudioTiming(config, self._clock())
        self._adapter = (self._adapter_factory or AudioAdapter)(config)
        self.talkback = Talkback(
            self._controller,
            self._boot,
            self._scope,
            check=self._checked,
            epoch=self._current_epoch,
            gain=speaker_volume,
            decoder_factory=self._decoder_factory,
            clock=self._clock,
        )
        self._last_feedback = self._clock()
        self._tasks = [
            asyncio.create_task(self._send_audio(), name="homekit-audio-send"),
            asyncio.create_task(self._receive(), name="homekit-audio-receive"),
            asyncio.create_task(self._reports(), name="homekit-audio-sr"),
        ]
        for task in self._tasks:
            task.add_done_callback(_observed)

    @property
    def rtp_origin(self):
        return self._base

    def _new_timeline(self):
        # Standalone local callers still use the same fixed correlation. The
        # live manager supplies its shared two-track owner in production.
        return LiveTimeline(
            audio_rate=self.config.sample_rate,
            video_origin=0,
            audio_origin=self._base,
            clock=self._clock,
        )

    @property
    def cleanup_failed(self):
        return self.talkback is not None and self.talkback.cleanup_failed

    @property
    def sent_packets(self):
        return self._packets

    @property
    def sent_octets(self):
        return self._octets

    def diagnostics(self):
        config = self.config
        receiver = None
        with contextlib.suppress(BaseException):
            if self._receiver_reports is not None:
                receiver = self._receiver_reports.snapshot(self._clock())
        return {
            "format": (
                {
                    "sample_rate": config.sample_rate,
                    "packet_ms": config.packet_time,
                    "channels": config.channels,
                    "bitrate_kbps": config.max_bit_rate,
                    "bitrate_mode": config.bitrate_mode,
                }
                if config is not None
                else None
            ),
            "sample_clock_rate": 48000,
            # Native lookahead/FIFO progress is not inferred from fed-minus-sent.
            "native_pending_samples": None,
            "queue": {"packets": len(self._outputs), "bytes": self._output_bytes},
            "receiver_reports": receiver,
            "metrics": self._metrics.snapshot(),
        }

    def _receiver_metric(self, method, *args):
        with contextlib.suppress(BaseException):
            if self._receiver_reports is not None:
                getattr(self._receiver_reports, method)(*args)

    def _metric(self, method, *args):
        with contextlib.suppress(BaseException):
            getattr(self._metrics, method)(*args)

    def _measure(self, name, began):
        with contextlib.suppress(BaseException):
            self._metrics.observe(name, self._clock() - began)

    def _checked(self, permission):
        began = self._clock()
        try:
            return self._check(permission)
        finally:
            self._measure("authority", began)

    def _current_epoch(self):
        began = self._clock()
        try:
            return self._epoch()
        finally:
            self._measure("epoch_getter", began)

    def _source_ledger(self, packets):
        from halo_server.media.opus import parse_opus

        durations = []
        for packet in packets:
            try:
                durations.append(parse_opus(packet.payload).duration_samples)
            except Exception:
                durations.append(0)
                self._metric("count", "source_invalid_packets")
            self._metric("observe", "source_age", max(0, self._clock() - packet.received_at))
        self._metric("count", "read_batches")
        self._metric("maximum", "batch_packets", len(packets))
        self._metric("maximum", "batch_samples", sum(durations))
        return _AudioLedger(tuple(durations), self._metric)

    def _failed(self, stage, code, error):
        self.failure = code
        self._trace("finish", self._clock())
        if self._diagnostic_failure is not None:
            with contextlib.suppress(BaseException):
                # The owner emits fixed metadata only. Diagnostics cannot
                # interrupt the existing whole-session retirement callback.
                self._diagnostic_failure(stage, code, error)
        self._failure(code)

    def _trace(self, method, *args):
        # Even a failing log handler must leave delivery and retirement intact.
        with contextlib.suppress(BaseException):
            if self._timing is not None:
                getattr(self._timing, method)(*args)

    def _may(self, permission):
        if self._retired:
            raise HomeKitOperationError("authority_changed")
        try:
            self._checked(permission)
        except HomeKitOperationError as exc:
            if exc.code == "policy_denied":
                return False
            raise
        return True

    async def _send(
        self, packet, *, permission=None, epoch=None, fresh_until=None, generation=None
    ):
        deadline = self._clock() + SEND_SECONDS
        while True:
            if self._retired:
                raise HomeKitOperationError("authority_changed")
            self._checked(permission)
            if generation is not None and generation != self._delivery_generation:
                return False
            if epoch is not None and epoch != self._current_epoch():
                return False
            if fresh_until is not None and self._clock() > fresh_until:
                return False
            try:
                if self.socket.send(packet) != len(packet):
                    raise HomeKitOperationError("media_failed")
                return True
            except BlockingIOError:
                if self._clock() >= deadline:
                    raise HomeKitOperationError("media_saturated") from None
                await asyncio.sleep(0.002)

    def _reset(self, epoch):
        self._delivery_generation += 1
        self._discard_outputs("output_epoch")
        if self._capture_epoch is not None:
            self._trace("reset")
        self._capture_epoch = epoch
        self._next_packet_at = 0
        self._input_spans.clear()
        self._input_end = None
        self._live_edge_pending = True

    def _expired_prefix(self):
        return bool(self._input_spans and self._clock() - self._input_spans[0][2] > MAX_AUDIO_AGE)

    async def _discard_expired_prefix(self):
        if self._expired_prefix():
            from halo_server.media.homekit_audio_codec import AudioAdapter

            began = self._clock()
            try:
                await self._adapter.close()
            finally:
                self._measure("retirement", began)
            self._checked("live_audio")
            if self._retired:
                raise HomeKitOperationError("authority_changed")
            self._adapter = (self._adapter_factory or AudioAdapter)(self.config)
            self._trace("count", "adapter_recreates")
            self._metric("count", "adapter_recreates")
            self._input_spans.clear()
            self._input_end = None
            return True
        return False

    def _output_receipt(self, output, fallback):
        start, end = output.timestamp, output.timestamp + output.duration
        receipts = [
            received for first, last, received in self._input_spans if last > start and first < end
        ]
        # Native encoder lookahead can precede the first submitted timestamp.
        # Conservatively retain the oldest receipt if no overlap is provable.
        received = (
            min(receipts)
            if receipts
            else (self._input_spans[0][2] if self._input_spans else fallback)
        )
        return received

    def _timestamp(self, timestamp):
        return self._timeline.rtp_timestamp(
            "audio", capture_epoch=self._capture_epoch, source_pts=timestamp
        )

    async def _send_audio(self):
        # One supervisor retains both owners until they actually finish. In
        # particular, cancelling a read must not orphan a pacing/socket task.
        self._output_tasks = [
            asyncio.create_task(self._produce_audio(), name="homekit-audio-convert"),
            asyncio.create_task(self._egress_audio(), name="homekit-audio-egress"),
        ]
        try:
            done, _pending = await asyncio.wait(
                self._output_tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                await task
        finally:
            for task in self._output_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._output_tasks, return_exceptions=True)

    async def _produce_audio(self):
        try:
            while not self._retired:
                self._checked(None)
                self._trace("tick", self._clock())
                if not await self._ready_audio():
                    await asyncio.sleep(0.05)
                    continue
                try:
                    began = self._clock()
                    try:
                        batch = await self.handle.read_audio(timeout=0.25)
                    finally:
                        self._measure("read_wait", began)
                except HomeKitOperationError as exc:
                    if exc.code != "policy_denied":
                        raise
                    # The handle itself observed denial before/after its wait.
                    # Even if permission is already back by the next poll, this
                    # is a real privacy boundary for queued and native PCM.
                    self._pause_delivery()
                    await asyncio.sleep(0.05)
                    continue
                await self._audio_batch(batch)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed("audio_send", "audio_output_failed", exc)

    def _pause_delivery(self):
        if not self._permission_paused:
            self._permission_paused = True
            self._audio_allowed_after = float("inf")
            self._delivery_generation += 1
            self._discard_outputs("output_send_fenced")
            self._metric("count", "permission_pauses")

    async def _ready_audio(self):
        if not self._may("live_audio"):
            self._pause_delivery()
        if self._permission_paused:
            if self._adapter is not None:
                began = self._clock()
                try:
                    await self._adapter.close()
                finally:
                    self._measure("retirement", began)
                self._adapter = None
                self._input_spans.clear()
                self._input_end = None
                self._live_edge_pending = True
            if not self._may("live_audio"):
                return False
            from halo_server.media.homekit_audio_codec import AudioAdapter

            self._checked("live_audio")
            if self._retired:
                raise HomeKitOperationError("authority_changed")
            self._adapter = (self._adapter_factory or AudioAdapter)(self.config)
            self._permission_paused = False
            # Same-epoch off/on is still a privacy boundary: an old source read
            # cannot supply PCM that was buffered before permission resumed.
            self._audio_allowed_after = self._clock()
            self._trace("count", "adapter_recreates")
            self._metric("count", "adapter_recreates")
        return True

    async def _audio_batch(self, batch):
        from halo_server.media.opus import parse_opus

        ledger = self._source_ledger(batch.packets)
        try:
            if batch.capture_epoch != self._capture_epoch:
                self._reset(batch.capture_epoch)
            if not await self._ready_audio():
                ledger.remaining("source_authority_discarded")
                return
            packets = batch.packets
            if not packets:
                # Idle alone grants no new delivery. Defer the conservative
                # prefix reset until there is a real fresh batch to reuse.
                return
            for packet in packets:
                self._trace("source", packet, self._clock())
            # Live viewers start at the current complete packet. Replaying
            # almost-expired history can make converter startup expire the
            # next prefix repeatedly, even while Ring supplies clean audio.
            # Recording reads retain their separate pre-roll semantics.
            if self._expired_prefix():
                # The source read has already advanced its cursor. Keep this
                # bounded fresh batch while the old native owner is joined;
                # dropping it here creates a reset/discard cascade on bursts.
                await self._discard_expired_prefix()
            if getattr(batch, "resynchronized", False):
                self._live_edge_pending = True
            if packets and self._clock() - packets[0].received_at > MAX_AUDIO_AGE:
                self._live_edge_pending = True
            if self._live_edge_pending and packets:
                self._trace("count", "live_edge_skipped", len(packets) - 1)
                category = (
                    "source_recovery_skipped" if self._submitted_any else "source_initial_skipped"
                )
                for skipped_index in range(len(packets) - 1):
                    ledger.record(skipped_index, category)
                packets = packets[-1:]
            offset = len(batch.packets) - len(packets)
            for relative_index, packet in enumerate(packets):
                input_index = offset + relative_index
                if packet.received_at < self._audio_allowed_after:
                    ledger.record(input_index, "source_authority_discarded")
                    continue
                if packet.capture_epoch != self._current_epoch():
                    self._trace("count", "epoch_drop")
                    ledger.record(input_index, "source_epoch_discarded")
                    continue
                if self._clock() - packet.received_at > MAX_AUDIO_AGE:
                    self._trace("count", "stale_input")
                    ledger.record(input_index, "source_stale_discarded")
                    continue
                if self._expired_prefix():
                    await self._discard_expired_prefix()
                await self._reserve_output_capacity()
                # Both native retirement and bounded queue backpressure yield;
                # neither grants an old packet any new delivery lifetime.
                if packet.capture_epoch != self._current_epoch():
                    self._trace("count", "epoch_drop")
                    ledger.record(input_index, "source_epoch_discarded")
                    break
                if not await self._ready_audio():
                    ledger.remaining("source_authority_discarded")
                    break
                if packet.received_at < self._audio_allowed_after:
                    ledger.record(input_index, "source_authority_discarded")
                    continue
                if self._clock() - packet.received_at > MAX_AUDIO_AGE:
                    self._trace("count", "stale_input")
                    ledger.record(input_index, "source_stale_discarded")
                    continue
                if self._expired_prefix():
                    await self._discard_expired_prefix()
                    if packet.capture_epoch != self._current_epoch():
                        ledger.record(input_index, "source_epoch_discarded")
                        break
                    if self._clock() - packet.received_at > MAX_AUDIO_AGE:
                        ledger.record(input_index, "source_stale_discarded")
                        continue
                if self._input_end is not None and packet.rtp_timestamp != self._input_end:
                    self._input_spans.clear()
                self._input_end = packet.rtp_timestamp + parse_opus(packet.payload).duration_samples
                if len(self._input_spans) >= MAX_INPUT_SPANS:
                    raise HomeKitOperationError("media_saturated")
                self._input_spans.append(
                    (
                        packet.rtp_timestamp,
                        self._input_end,
                        packet.received_at,
                    )
                )
                self._metric("maximum", "input_spans", len(self._input_spans))
                if self._timeline is None:
                    self._timeline = self._new_timeline()
                self._timeline.observe_input(
                    "audio",
                    capture_epoch=packet.capture_epoch,
                    source_pts=packet.rtp_timestamp,
                    received_at=packet.received_at,
                    source_clocks=tuple(getattr(self.handle, "source_clocks", ())),
                )
                ledger.record(input_index, "source_submitted")
                self._submitted_any = True
                feed_started = self._clock()
                try:
                    outputs = await self._adapter.feed(
                        packet.payload, packet.rtp_timestamp, capture_epoch=packet.capture_epoch
                    )
                finally:
                    self._trace("feed", self._clock() - feed_started)
                    self._measure("feed", feed_started)
                self._live_edge_pending = False
                try:
                    await self._audio_outputs(outputs, packet)
                finally:
                    # Do not retain a sent/evicted previous native response in
                    # this local while awaiting the next read/feed reservation.
                    del outputs
                if self._permission_paused:
                    ledger.remaining("source_authority_discarded")
                    break
        finally:
            ledger.remaining()

    def _output_result(self, item, category):
        if item.outcome is None:
            item.outcome = category
            self._metric("count", f"{category}_packets")
            self._metric("count", f"{category}_samples", item.output.duration)

    def _remove_output(self, item):
        self._outputs.remove(item)
        self._output_bytes -= len(item.output.payload)
        self._output_space.set()

    async def _reserve_output_capacity(self):
        if len(self._outputs) > MAX_QUEUED_OUTPUTS - MAX_NATIVE_OUTPUTS:
            began = self._clock()
            try:
                async with asyncio.timeout(MAX_AUDIO_AGE):
                    while len(self._outputs) > MAX_QUEUED_OUTPUTS - MAX_NATIVE_OUTPUTS:
                        self._output_space.clear()
                        await self._output_space.wait()
                        self._checked("live_audio")
                        if self._retired:
                            raise HomeKitOperationError("authority_changed")
            except TimeoutError:
                # A stalled sender cannot turn source admission into an
                # unbounded wait. Preserve its charged item and drop oldest
                # unsent packets to reserve a complete bounded response.
                while len(self._outputs) > MAX_QUEUED_OUTPUTS - MAX_NATIVE_OUTPUTS:
                    index = 1 if self._sending is self._outputs[0] else 0
                    item = self._outputs[index]
                    self._output_result(item, "output_overflow")
                    self._remove_output(item)
            finally:
                self._measure("output_backpressure", began)

    def _discard_outputs(self, category):
        for item in tuple(self._outputs):
            self._output_result(item, category)
            # The sender may still reference its current item across an await.
            # Keep it charged until its finally block releases actual ownership.
            if item is not self._sending:
                self._remove_output(item)
        self._output_ready.set()

    def _retained_outputs(self, staging_packets=0, staging_bytes=0):
        count = len(self._outputs) + staging_packets
        # Reserve one plain and one protected datagram, including headers.
        size = self._output_bytes + staging_bytes + 2 * MAX_AUDIO_PACKET
        if count > MAX_QUEUED_OUTPUTS or size > MAX_RETAINED_OUTPUT_BYTES:
            raise HomeKitOperationError("media_saturated")
        self._metric("maximum", "retained_output_packets", count)
        self._metric("maximum", "retained_output_bytes", size)
        self._metric("maximum", "queued_packets", len(self._outputs))
        self._metric("maximum", "queued_bytes", self._output_bytes)

    async def _audio_outputs(self, outputs, packet):
        generation = self._delivery_generation
        ledger = _AudioLedger(
            tuple(output.duration for output in outputs), self._metric, output=True
        )
        self._metric("maximum", "output_batch_packets", len(outputs))
        try:
            if len(outputs) > MAX_NATIVE_OUTPUTS or any(
                not 0 < len(output.payload) <= MAX_AUDIO_PACKET - 22 for output in outputs
            ):
                raise HomeKitOperationError("media_saturated")
            staging_bytes = sum(len(output.payload) for output in outputs)
            self._retained_outputs(len(outputs), staging_bytes)
            receipts = tuple(self._output_receipt(output, packet.received_at) for output in outputs)
            # First establish every receipt, then advance the generated frontier.
            # Sending, queue eviction and stale disposal must not prune metadata
            # before another output in this same response is classified.
            if outputs:
                end = max(output.timestamp + output.duration for output in outputs)
                while self._input_spans and self._input_spans[0][1] <= end:
                    self._input_spans.popleft()
            # Receipt attribution advances at generation, independent of which
            # outputs are later sent, stale, or dropped by bounded queue policy.
            for index, output in enumerate(outputs):
                ledger.visit(index)
                self._trace("output", output)
                received_at = receipts[index]
                staging_bytes -= len(output.payload)
                if packet.capture_epoch != self._current_epoch():
                    self._trace("count", "epoch_drop")
                    ledger.record(index, "output_epoch")
                    continue
                self._metric("observe", "output_age", max(0, self._clock() - received_at))
                if self._clock() - received_at > MAX_AUDIO_AGE:
                    self._trace("count", "stale_output")
                    ledger.record(index, "output_stale")
                    continue
                if (
                    self._permission_paused
                    or generation != self._delivery_generation
                    or not self._may("live_audio")
                ):
                    self._pause_delivery()
                    ledger.record(index, "output_send_fenced")
                    continue
                if len(self._outputs) >= MAX_QUEUED_OUTPUTS:
                    # Prefer the live edge without revoking the current sender's
                    # ownership or disguising the missing media timestamp span.
                    evicted = self._outputs[1 if self._sending is self._outputs[0] else 0]
                    self._output_result(evicted, "output_overflow")
                    self._remove_output(evicted)
                item = _QueuedAudio(output, received_at, packet.capture_epoch, generation)
                self._outputs.append(item)
                self._output_bytes += len(output.payload)
                ledger.record(index, "output_queued")
                self._retained_outputs(len(outputs) - index - 1, staging_bytes)
                self._output_ready.set()
        finally:
            ledger.remaining()

    async def _egress_audio(self):
        try:
            while not self._retired:
                if not self._outputs:
                    self._output_ready.clear()
                    await self._output_ready.wait()
                    continue
                await self._send_output(self._outputs[0])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed("audio_send", "audio_output_failed", exc)

    async def _send_output(self, item):
        self._sending = item
        output, received_at = item.output, item.received_at
        try:
            if item.outcome is not None:
                return
            if item.generation != self._delivery_generation or item.epoch != self._current_epoch():
                self._output_result(item, "output_epoch")
                return
            if self._permission_paused or not self._may("live_audio"):
                self._pause_delivery()
                self._output_result(item, "output_send_fenced")
                return
            if self._clock() - received_at > MAX_AUDIO_AGE:
                self._trace("count", "stale_output")
                self._output_result(item, "output_stale")
                return
            if self._next_packet_at:
                self._metric("observe", "pacing_late", max(0, self._clock() - self._next_packet_at))
                self._trace(
                    "maximum", "pacing_late_ms_max", (self._clock() - self._next_packet_at) * 1000
                )
            now = self._clock()
            if self._pacer is None:
                self._pacer = _AudioPacer(self.config, self._pacing_family, now)
            wire_size = len(output.payload) + 22 + self._pacer.network_header
            queued_samples = sum(
                queued.output.duration for queued in self._outputs if queued.outcome is None
            )
            catchup = queued_samples > CATCHUP_QUEUE_SAMPLES or now - received_at >= CATCHUP_AGE
            interval = output.duration / 48000 / (2 if catchup else 1)
            target = max(now, self._next_packet_at, now + self._pacer.delay(wire_size, now))
            if target > received_at + MAX_AUDIO_AGE:
                # Do not block fresh successors on a head whose bounded packet
                # or wire allowance cannot admit it before its own deadline.
                self._output_result(item, "output_budget_rejected")
                return
            if target > self._clock():
                began = self._clock()
                try:
                    await asyncio.sleep(target - self._clock())
                finally:
                    self._measure("pacing_wait", began)
            if item.outcome is not None:
                return
            if self._clock() - received_at > MAX_AUDIO_AGE:
                self._trace("count", "stale_output")
                self._output_result(item, "output_stale")
                return
            if item.generation != self._delivery_generation or item.epoch != self._current_epoch():
                self._output_result(item, "output_epoch")
                return
            if self._permission_paused or not self._may("live_audio"):
                self._pause_delivery()
                self._output_result(item, "output_send_fenced")
                return
            timestamp = self._timestamp(output.timestamp)
            plain = (
                struct.pack(
                    "!BBHII",
                    0x80,
                    self.config.payload_type,
                    self._sequence,
                    timestamp,
                    self.prepared.ssrc,
                )
                + output.payload
            )
            protected = self.secure.protect(plain)
            # Native SRTP consumed this index even if a final fence rejects it.
            self._sequence = (self._sequence + 1) & 65535
            if not await self._send(
                protected,
                permission="live_audio",
                epoch=item.epoch,
                fresh_until=received_at + MAX_AUDIO_AGE,
                generation=item.generation,
            ):
                self._output_result(item, "output_send_fenced")
                return
            self._pacer.sent(wire_size, self._clock())
            if catchup:
                self._metric("count", "catchup_packets")
            self._output_result(item, "output_sent")
            self._timeline.sent("audio", capture_epoch=item.epoch, source_pts=output.timestamp)
            self._last_timestamp = timestamp
            self._last_duration = output.duration * self.config.sample_rate // 48000
            began = self._clock()
            try:
                self._last_presentation = self._timeline.presentation(
                    "audio", capture_epoch=item.epoch, source_pts=output.timestamp
                )
            finally:
                self._measure("presentation", began)
            self._packets += 1
            self._octets += len(output.payload)
            self._trace(
                "sent",
                timestamp,
                self._last_duration,
                len(output.payload),
                self._clock(),
                self._last_presentation,
            )
            self._next_packet_at = target + interval
        finally:
            self._output_result(item, "output_unfinished")
            self._remove_output(item)
            self._sending = None

    async def _receive(self):
        from halo_server.media.opus import parse_opus

        try:
            loop = asyncio.get_running_loop()
            window, count = self._clock(), 0
            while not self._retired:
                protected = await loop.sock_recv(self.socket, 4096)
                self._checked(None)
                now = self._clock()
                if now - window >= 1:
                    window, count = now, 0
                count += 1
                if count > MAX_DATAGRAM_RATE:
                    await asyncio.sleep(0.01)
                    continue
                try:
                    if len(protected) >= 2 and 192 <= protected[1] <= 223:
                        feedback = parse_feedback(
                            self.secure.unprotect_rtcp(protected), self.prepared.ssrc
                        )
                        self._receiver_metric("observe", feedback.receiver_reports, now)
                        self._last_feedback = now
                        self._touch_feedback(now)
                        if feedback.bye:
                            self._failure("receiver_closed")
                            return
                        continue
                    clear = self.secure.unprotect(protected)
                    packet = RtpPacket.parse(clear)
                    if packet.payload_type != self.config.payload_type:
                        continue
                    parsed = parse_opus(packet.payload)
                    if parsed.channels != self.config.channels or parsed.duration_samples > 5760:
                        continue
                except (SrtpError, RtpError, ValueError, RuntimeError):
                    continue
                if not self._may("talkback"):
                    continue
                self._last_feedback = now
                self._touch_feedback(now)
                self.talkback.submit(
                    TalkPacket(
                        packet.payload,
                        packet.sequence_number,
                        packet.timestamp,
                        now,
                        self._current_epoch(),
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed("audio_feedback", "audio_feedback_failed", exc)

    async def _reports(self):
        try:
            while not self._retired:
                await asyncio.sleep(self.config.rtcp_interval)
                self._checked(None)
                self._trace("tick", self._clock())
                if self._clock() - max(self._last_feedback, self._last_seen()) > RECEIVER_SECONDS:
                    self._failure("receiver_timeout")
                    return
                if (
                    self._last_timestamp is not None
                    and self._capture_epoch == self._current_epoch()
                    and self._may("live_audio")
                ):
                    if self._timeline.capture_epoch != self._capture_epoch:
                        continue
                    pair = self._timeline.report(
                        "audio", capture_epoch=self._capture_epoch, instant=self._timeline.instant()
                    )
                    if pair is None:
                        continue
                    report = sender_report(
                        self.prepared.ssrc,
                        pair.rtp_timestamp,
                        self._packets,
                        self._octets,
                        now=pair.wall_time,
                    )
                    sent = await self._send(
                        self.secure.protect_rtcp(report),
                        permission="live_audio",
                        epoch=self._capture_epoch,
                    )
                    if sent:
                        self._receiver_metric("sent", report, self._clock())
                        self._trace("count", "rtcp_sent")
                        self._trace(
                            "maximum",
                            "presentation_age_ms_max",
                            (self._clock() - self._last_presentation) * 1000,
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed("audio_reports", "audio_report_failed", exc)

    def fence(self):
        self._retired = True
        self._delivery_generation += 1
        self._discard_outputs("output_retired")
        self._trace("finish", self._clock())
        descriptor = self.socket.fileno()
        if descriptor >= 0:
            loop = asyncio.get_running_loop()
            loop.remove_reader(descriptor)
            loop.remove_writer(descriptor)
            self.socket.close()
        if self.talkback is not None:
            self.talkback.fence()

    async def close(self):
        self.fence()
        tasks = (*self._tasks, *self._output_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.talkback is not None:
            await self.talkback.close()
        if self._adapter is not None:
            began = self._clock()
            try:
                await self._adapter.close()
            finally:
                self._measure("retirement", began)
        self.secure.close()
        self._input_spans.clear()


def _observed(task):
    if not task.cancelled():
        with contextlib.suppress(Exception):
            task.exception()
