"""Original Opus copy/repacketization and killable native conversion owners."""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from halo_server.media.manager import settle_owned
from halo_server.media.opus import (
    MAX_AUDIO_PAYLOAD,
    MAX_OPUS_PACKET,
    AudioConfiguration,
    AudioOutput,
    OpusError,
    parse_opus,
    repacketize,
)
from halo_server.media.worker_limits import worker_environment

MAX_WORKERS = 4
START_SECONDS = 3.0
FRAME_SECONDS = 1.0
STOP_SECONDS = 1.0
_MAX_RESPONSE = 16 * 12000
_HEADER = struct.Struct("!cqI")
_COUNT_LOCK = threading.Lock()
_WORKERS = 0
_RETIRING_WORKERS: set = set()


class AudioTransformError(OpusError):
    """Audio cannot safely complete or retire its current operation."""


@dataclass(frozen=True)
class AudioCapabilities:
    ready: bool
    catalog: tuple[AudioConfiguration, ...]
    reason: str | None = None


class AudioWorker:
    """One outstanding access unit; ownership survives failed termination."""

    def __init__(self, configuration: AudioConfiguration, *, mode="encode"):
        self.mode = mode
        self.configuration = configuration
        self._process = None
        self._spawn: asyncio.Task | None = None
        self._reserved = False
        self._closed = False
        self._starting = asyncio.Lock()
        self._operation = asyncio.Lock()
        self._closing = asyncio.Lock()

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    async def start(self):
        global _WORKERS
        async with self._starting:
            if self._closed:
                raise AudioTransformError("Audio worker is retired")
            if self._process is not None:
                return
            environment = worker_environment()
            with _COUNT_LOCK:
                if _WORKERS >= MAX_WORKERS:
                    raise AudioTransformError("Audio converter capacity is exhausted")
                _WORKERS += 1
                self._reserved = True
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("audio_worker.py")),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=65536,
                    umask=0o077,
                    env=environment,
                )
            )
            self._spawn = spawn
            try:
                async with asyncio.timeout(START_SECONDS):
                    self._process = await asyncio.shield(spawn)
                    if self._closed:
                        raise AudioTransformError("Audio worker startup was retired")
                    body = json.dumps(
                        {"mode": self.mode, "configuration": asdict(self.configuration)},
                        separators=(",", ":"),
                    ).encode()
                    self._process.stdin.write(struct.pack("!I", len(body)) + body)
                    await self._process.stdin.drain()
                    if await self._read() != b"S":
                        raise AudioTransformError("Audio converter initialization failed")
            except BaseException:
                await self.close()
                raise
            finally:
                if self._spawn is spawn and spawn.done():
                    self._spawn = None

    async def _read(self):
        header = await self._process.stdout.readexactly(4)
        count = struct.unpack("!I", header)[0]
        if not 0 < count <= _MAX_RESPONSE:
            raise AudioTransformError("Audio worker returned oversized data")
        return await self._process.stdout.readexactly(count)

    async def feed(self, payload: bytes, timestamp: int):
        return await self._request(b"F", payload, timestamp)

    async def flush(self):
        return await self._request(b"D", b"", 0)

    async def _request(self, kind, payload, timestamp):
        if self._operation.locked():
            raise AudioTransformError("Audio worker is busy")
        async with self._operation:
            if (
                not isinstance(payload, bytes)
                or len(payload) > MAX_OPUS_PACKET
                or type(timestamp) is not int
                or not -(2**53) < timestamp < 2**53
            ):
                raise AudioTransformError("Invalid audio worker input")
            if kind == b"F":
                parse_opus(payload)
            await self.start()
            if self._closed:
                raise AudioTransformError("Audio worker is retired")
            try:
                async with asyncio.timeout(FRAME_SECONDS):
                    self._process.stdin.write(_HEADER.pack(kind, timestamp, len(payload)))
                    self._process.stdin.write(payload)
                    await self._process.stdin.drain()
                    body = await self._read()
                if self._closed or len(body) < 2 or body[:1] != b"A" or body[1] > 16:
                    raise AudioTransformError("Audio conversion failed")
                offset, result = 2, []
                for _ in range(body[1]):
                    if offset + 14 > len(body):
                        raise AudioTransformError("Truncated audio output")
                    pts, duration, count = struct.unpack_from("!qHI", body, offset)
                    offset += 14
                    limit = MAX_AUDIO_PAYLOAD if self.mode == "encode" else 1920
                    if not 0 < count <= limit or offset + count > len(body):
                        raise AudioTransformError("Invalid audio output size")
                    data = body[offset : offset + count]
                    if self.mode == "decode":
                        if count != 1920 or duration != 960:
                            raise AudioTransformError("Invalid PCM output")
                    else:
                        parsed = parse_opus(data)
                        if (
                            parsed.duration_samples != self.configuration.packet_time * 48
                            or duration != parsed.duration_samples
                            or parsed.channels != 1
                            or parsed.bandwidth_rate > self.configuration.sample_rate
                        ):
                            raise AudioTransformError("Incompatible encoded output")
                    result.append((pts, duration, data))
                    offset += count
                if offset != len(body):
                    raise AudioTransformError("Unexpected audio worker data")
                return tuple(result)
            except BaseException:
                await self.close()
                raise

    async def close(self):
        self._closed = True
        task = asyncio.create_task(self._close_owned(), name="homekit-audio-worker-stop")
        try:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await settle_owned(task)
                raise
        except BaseException:
            if self._reserved:
                _RETIRING_WORKERS.add(self)
            raise
        finally:
            if not self._reserved:
                _RETIRING_WORKERS.discard(self)

    async def _close_owned(self):
        global _WORKERS
        async with self._closing:
            self._closed = True
            spawn = self._spawn
            if spawn is not None:
                _done, pending = await asyncio.wait({spawn}, timeout=STOP_SECONDS)
                if pending:
                    raise AudioTransformError("Audio worker spawn retirement is incomplete")
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    self._process = spawn.result()
                self._spawn = None
            process = self._process
            if process is not None:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), STOP_SECONDS)
                    except TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                        try:
                            await asyncio.wait_for(process.wait(), STOP_SECONDS)
                        except TimeoutError:
                            raise AudioTransformError(
                                "Audio worker retirement is incomplete"
                            ) from None
                if process.stdin is not None:
                    process.stdin.close()
                    with contextlib.suppress(ConnectionError, BrokenPipeError):
                        await asyncio.wait_for(process.stdin.wait_closed(), STOP_SECONDS)
                self._process = None
            if self._reserved:
                with _COUNT_LOCK:
                    _WORKERS -= 1
                self._reserved = False


class AudioAdapter:
    """Preserve eligible source bytes, group actual frames, otherwise convert."""

    def __init__(self, configuration: AudioConfiguration):
        self.configuration = configuration
        self._worker = None
        self._pending = []
        self._epoch = None
        self._expected = None
        self._closed = False
        self._operation = asyncio.Lock()
        self.mode = "copy"
        self._tokens = configuration.max_bit_rate * 1000
        self._token_timestamp = None

    async def _reset(self, epoch):
        if self._worker is not None:
            await self._worker.close()
            if self._closed:
                raise AudioTransformError("Audio output retired during reset")
            self._worker = None
        self._pending.clear()
        self._epoch, self._expected = epoch, None
        self.mode = "copy"
        self._tokens = self.configuration.max_bit_rate * 1000
        self._token_timestamp = None

    def _budget(self, payload, timestamp):
        rate = self.configuration.max_bit_rate * 1000
        if self._token_timestamp is not None:
            self._tokens = min(
                rate, self._tokens + max(0, timestamp - self._token_timestamp) * rate / 48000
            )
        self._token_timestamp = timestamp
        self._tokens -= len(payload) * 8
        return self._tokens >= 0

    async def _adapt(self, inputs):
        if self._closed:
            raise AudioTransformError("Audio output is retired")
        if self._worker is None:
            self._worker = AudioWorker(self.configuration)
        worker = self._worker
        self.mode = "adapt"
        result = []
        for payload, timestamp in inputs:
            for pts, duration, data in await worker.feed(payload, timestamp):
                if self._closed:
                    raise AudioTransformError("Audio output retired during conversion")
                result.append(AudioOutput(data, pts, duration, "adapt"))
        return tuple(result)

    async def feed(self, payload, timestamp, *, capture_epoch=0):
        parsed = parse_opus(payload)
        if (
            type(timestamp) is not int
            or not 0 <= timestamp < 2**53
            or type(capture_epoch) is not int
            or capture_epoch < 0
        ):
            raise AudioTransformError("Invalid audio source identity")
        if self._closed or self._operation.locked():
            raise AudioTransformError("Audio output is closed or busy")
        async with self._operation:
            if self._epoch != capture_epoch or (
                self._expected is not None and timestamp != self._expected
            ):
                await self._reset(capture_epoch)
            self._expected = timestamp + parsed.duration_samples
            selected = self.configuration
            convertible = (
                selected.gain != 100
                or parsed.channels != 1
                or parsed.bandwidth_rate > selected.sample_rate
                or selected.packet_time * 48 % parsed.frame_samples != 0
                or not self._budget(payload, timestamp)
                or (
                    selected.bitrate_mode == "cbr"
                    and len(payload) != selected.max_bit_rate * parsed.duration_samples // 384
                )
            )
            if self.mode == "adapt" or convertible:
                inputs = self._pending + [(payload, timestamp)]
                self._pending = []
                result = await self._adapt(inputs)
            elif (
                not self._pending
                and parsed.duration_samples == selected.packet_time * 48
                and len(payload) <= MAX_AUDIO_PAYLOAD
            ):
                result = (AudioOutput(payload, timestamp, parsed.duration_samples, "copy"),)
            else:
                if self._pending and parse_opus(self._pending[0][0]).toc != parsed.toc:
                    inputs, self._pending = self._pending + [(payload, timestamp)], []
                    result = await self._adapt(inputs)
                else:
                    self._pending.append((payload, timestamp))
                    if len(self._pending) > 48:
                        raise AudioTransformError("Audio grouping capacity exceeded")
                    frames = []
                    for data, pts in self._pending:
                        packet = parse_opus(data)
                        frames.extend(
                            (bytes((packet.toc,)) + frame, pts + index * packet.frame_samples)
                            for index, frame in enumerate(packet.frames)
                        )
                    target = selected.packet_time * 48 // parsed.frame_samples
                    result = []
                    while len(frames) >= target:
                        group, frames = frames[:target], frames[target:]
                        data = repacketize(item[0] for item in group)
                        if len(data) > MAX_AUDIO_PAYLOAD:
                            inputs, self._pending = self._pending, []
                            result = await self._adapt(inputs)
                            break
                        result.append(
                            AudioOutput(data, group[0][1], selected.packet_time * 48, "repacketize")
                        )
                    else:
                        self._pending = list(frames)
                        result = tuple(result)
            if self._closed:
                raise AudioTransformError("Audio output retired during conversion")
            return tuple(result)

    async def flush(self):
        """Explicit authorized drain for qualification/end-of-input, never privacy stop."""
        if self._closed or self._operation.locked():
            raise AudioTransformError("Audio output is closed or busy")
        async with self._operation:
            prefix = ()
            if self._pending:
                inputs, self._pending = self._pending, []
                prefix = await self._adapt(inputs)
            if self._worker is None:
                return ()
            result = prefix + tuple(
                AudioOutput(data, pts, duration, "adapt")
                for pts, duration, data in await self._worker.flush()
            )
            if self._closed:
                raise AudioTransformError("Audio output is retired")
            return result

    async def close(self):
        self._closed = True
        self._pending.clear()
        if self._worker is not None:
            await self._worker.close()
            self._worker = None


class AudioDecoder:
    """Authenticated return Opus to finite, exact 20 ms mono S16LE frames."""

    def __init__(self, *, gain=100):
        self.configuration = AudioConfiguration(24000, 20, "vbr", 24, gain=gain)
        self._worker = None
        self._epoch = None
        self._timestamp = 0
        self._closed = False
        self._operation = asyncio.Lock()

    async def feed(self, payload, *, capture_epoch=0):
        parsed = parse_opus(payload)
        if type(capture_epoch) is not int or capture_epoch < 0:
            raise AudioTransformError("Invalid audio generation")
        if self._closed or self._operation.locked():
            raise AudioTransformError("Audio decoder is closed or busy")
        async with self._operation:
            if self._epoch != capture_epoch:
                if self._worker is not None:
                    await self._worker.close()
                    if self._closed:
                        raise AudioTransformError("Audio decoder retired during reset")
                self._worker = None
                self._timestamp, self._epoch = 0, capture_epoch
            if self._worker is None:
                self._worker = AudioWorker(self.configuration, mode="decode")
            result = await self._worker.feed(payload, self._timestamp)
            self._timestamp += parsed.duration_samples
            if self._closed:
                raise AudioTransformError("Audio decoder retired during conversion")
            return tuple(data for _pts, _duration, data in result)

    async def close(self):
        self._closed = True
        if self._worker is not None:
            await self._worker.close()
            self._worker = None


async def retire_audio_workers():
    failed = False
    for worker in tuple(_RETIRING_WORKERS):
        try:
            await worker.close()
        except BaseException:
            failed = True
    if failed:
        raise AudioTransformError("Audio worker retirement remains incomplete")


async def audio_capabilities():
    catalog = tuple(
        AudioConfiguration(rate, duration, mode, 24)
        for rate in (8000, 16000, 24000)
        for duration in (20, 30, 40, 60)
        for mode in ("cbr", "vbr")
    )
    try:
        async with asyncio.timeout(15):
            await retire_audio_workers()
            for rate in (8000, 16000, 24000):
                for mode in ("cbr", "vbr"):
                    adapter = AudioAdapter(AudioConfiguration(rate, 30, mode, 24))
                    try:
                        output = []
                        for index in range(6):
                            output.extend(await adapter.feed(b"\xf8\xff\xfe", index * 960))
                        if not output or any(
                            parse_opus(item.payload).duration_samples != 1440 for item in output
                        ):
                            raise AudioTransformError("Audio preflight failed")
                        decoder = AudioDecoder()
                        try:
                            if not await decoder.feed(output[0].payload):
                                raise AudioTransformError("Audio decoder preflight failed")
                        finally:
                            await decoder.close()
                    finally:
                        await adapter.close()
        return AudioCapabilities(True, catalog)
    except (Exception, asyncio.CancelledError):
        if asyncio.current_task().cancelling():
            raise
        return AudioCapabilities(False, (), "native_audio_unavailable")
