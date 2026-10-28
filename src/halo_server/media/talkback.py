"""Bounded authenticated speech into the existing generation-gated Ring owner."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from uuid import uuid4

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media.manager import settle_owned

READY_SECONDS = 3.0
IDLE_SECONDS = 1.0
MAX_LIFETIME = 300.0
MAX_QUEUE_PACKETS = 64
MAX_QUEUE_SECONDS = 1.0
MAX_PACKET_AGE = 0.25
JITTER_SECONDS = 0.04
MAX_REORDER = 64


@dataclass(frozen=True)
class TalkPacket:
    payload: bytes
    sequence: int
    timestamp: int
    received_at: float
    epoch: int


class SpeechJitter:
    """A small sequence-based reorder window; timestamps never allocate PCM."""

    def __init__(self):
        self._highest = None
        self._next = None
        self._sent = False
        self._packets = {}

    def put(self, packet):
        if self._highest is None:
            extended = packet.sequence
        else:
            delta = ((packet.sequence - self._highest + 32768) & 65535) - 32768
            extended = self._highest + delta
        if self._next is not None and (
            extended < self._next if self._sent else extended < self._next - MAX_REORDER
        ):
            return
        if self._highest is not None and abs(extended - self._highest) > MAX_REORDER:
            return
        self._highest = max(extended, self._highest if self._highest is not None else extended)
        if not self._sent:
            self._next = min(extended, self._next if self._next is not None else extended)
        if len(self._packets) < MAX_QUEUE_PACKETS:
            self._packets.setdefault(extended, packet)

    def pop(self, now):
        if not self._packets:
            return None
        first = min(self._packets)
        packet = self._packets.get(self._next)
        if packet is None:
            if now - self._packets[first].received_at < JITTER_SECONDS:
                return None
            self._next, packet = first, self._packets[first]
        if not self._sent and now - packet.received_at < JITTER_SECONDS:
            return None
        del self._packets[self._next]
        self._next += 1
        self._sent = True
        return packet


class Talkback:
    """One lazy speaker claim, retained through cancellation and late cleanup."""

    def __init__(
        self,
        controller,
        boot,
        scope,
        *,
        check,
        epoch,
        gain=100,
        decoder_factory=None,
        clock=time.monotonic,
    ):
        self.controller, self.boot, self.scope = controller, boot, scope
        self._check, self._epoch, self._clock = check, epoch, clock
        self._gain, self._decoder_factory = gain, decoder_factory
        self._queue = asyncio.Queue(MAX_QUEUE_PACKETS)
        self._task = None
        self._opening = None
        self._speaker = self._decoder = None
        self._retired = False
        self._capture_epoch = None
        self._last_packet = 0.0
        self._began = None
        self._next_pcm = 0.0
        self._last_timestamp = None
        self.cleanup_failed = False
        self.state = "idle"
        self.failure = None

    def submit(self, packet):
        if self._retired or self.state == "exhausted":
            return
        self._check("talkback")
        self._last_packet = packet.received_at
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="homekit-talkback")
            self._task.add_done_callback(_observed)
        try:
            self._queue.put_nowait(packet)
        except asyncio.QueueFull:
            # Never replace the beginning of a sentence with stale queued speech.
            self.failure = "talkback_saturated"
            self.fence()

    def _current(self, epoch):
        if self._retired or epoch != self._epoch():
            raise HomeKitOperationError("authority_changed")
        self._check("talkback")

    async def _open(self, epoch, received_at):
        self.state = "starting"
        self._speaker = await self.controller.open_speaker(
            self.boot, self.scope, session_id=str(uuid4())
        )
        self._current(epoch)
        if self._clock() - received_at > MAX_QUEUE_SECONDS:
            raise HomeKitOperationError("talkback_stale")
        # Ring's existing activation owner waits for one admitted microphone
        # frame before signalling speaker readiness. Prime only after actual
        # authenticated Opus decode; retain real speech until readiness arrives.
        await self._speaker.write(bytes(1920))
        self._current(epoch)
        if not await self._speaker.wait_ready(READY_SECONDS):
            raise HomeKitOperationError("speaker_not_ready")
        self._current(epoch)
        self.state = "active"

    async def _release_speaker(self):
        if self._opening is not None:
            if not self._opening.done():
                self._opening.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await settle_owned(self._opening)
            self._opening = None
        failures = []
        if self._speaker is not None:
            try:
                await self._speaker.close()
            except BaseException as exc:
                failures.append(exc)
            else:
                self._speaker = None
        if self._decoder is not None:
            try:
                await self._decoder.close()
            except BaseException as exc:
                failures.append(exc)
            else:
                self._decoder = None
        if failures:
            self.cleanup_failed = True
            raise failures[0]
        self.cleanup_failed = False

    async def _run(self):
        from halo_server.media.homekit_audio_codec import AudioDecoder

        jitter = SpeechJitter()
        renewed = self._clock()
        try:
            while not self._retired:
                now = self._clock()
                if self._began is not None and now - self._began >= MAX_LIFETIME:
                    self.state = "exhausted"
                    return
                try:
                    packet = await asyncio.wait_for(self._queue.get(), 0.02)
                except TimeoutError:
                    packet = None
                if packet is not None:
                    if packet.epoch != self._capture_epoch:
                        await self._release_speaker()
                        self._capture_epoch = packet.epoch
                        self._last_timestamp = None
                        self._next_pcm = 0.0
                        jitter = SpeechJitter()
                    jitter.put(packet)
                now = self._clock()
                while (ordered := jitter.pop(now)) is not None:
                    self._current(ordered.epoch)
                    if now - ordered.received_at > MAX_QUEUE_SECONDS:
                        raise HomeKitOperationError("talkback_stale")
                    if self._last_timestamp is not None:
                        delta = (
                            (ordered.timestamp - self._last_timestamp + 2**31) & 0xFFFFFFFF
                        ) - 2**31
                        if delta <= 0:
                            continue
                    self._last_timestamp = ordered.timestamp
                    if self._decoder is None:
                        self._decoder = (self._decoder_factory or AudioDecoder)(gain=self._gain)
                    frames = await self._decoder.feed(ordered.payload, capture_epoch=ordered.epoch)
                    self._current(ordered.epoch)
                    if not frames:
                        continue
                    if len(frames) > 6 or any(
                        not isinstance(frame, bytes) or len(frame) != 1920 for frame in frames
                    ):
                        raise HomeKitOperationError("invalid_pcm")
                    if self._speaker is None:
                        if self._clock() - ordered.received_at > MAX_QUEUE_SECONDS:
                            raise HomeKitOperationError("talkback_stale")
                        if self._began is None:
                            self._began = now
                        self._opening = asyncio.create_task(
                            self._open(ordered.epoch, ordered.received_at)
                        )
                        self._opening.add_done_callback(_observed)
                        await asyncio.shield(self._opening)
                        self._current(ordered.epoch)
                        # Readiness must not turn a short queue into delayed speech.
                        if self._clock() - ordered.received_at > MAX_QUEUE_SECONDS:
                            raise HomeKitOperationError("talkback_stale")
                        renewed = self._clock()
                    for frame in frames:
                        self._current(ordered.epoch)
                        if len(frame) != 1920:
                            raise HomeKitOperationError("invalid_pcm")
                        if self._clock() - ordered.received_at > MAX_QUEUE_SECONDS:
                            raise HomeKitOperationError("talkback_stale")
                        target = max(self._clock(), self._next_pcm)
                        if target > self._clock():
                            await asyncio.sleep(target - self._clock())
                        self._current(ordered.epoch)
                        if self._clock() - ordered.received_at > MAX_QUEUE_SECONDS:
                            raise HomeKitOperationError("talkback_stale")
                        await self._speaker.write(frame)
                        self._next_pcm = target + 0.02
                    if self._clock() - renewed >= 5:
                        if not await self._speaker.renew():
                            raise HomeKitOperationError("authority_changed")
                        renewed = self._clock()
                if self._speaker is not None and now - self._last_packet >= IDLE_SECONDS:
                    await self._release_speaker()
                    self.state = "idle"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = exc.code if isinstance(exc, HomeKitOperationError) else "talkback_failed"
            self.state = "failed"
            # Failed readiness/decode is not retried while the peer keeps sending.
            self._retired = True
        finally:
            await self._release_speaker()

    def fence(self):
        if self._retired:
            return
        self._retired = True
        while not self._queue.empty():
            self._queue.get_nowait()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def close(self):
        self.fence()
        if self._task is not None:
            # A previous teardown may have failed in this task's finally block.
            # Retry its retained resource objects instead of replaying that old
            # exception forever after the actual close condition recovers.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await settle_owned(self._task)
        await self._release_speaker()


def _observed(task):
    if not task.cancelled():
        with contextlib.suppress(Exception):
            task.exception()
