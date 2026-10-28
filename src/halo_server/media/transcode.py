"""Bounded native H.264 copy selection and killable managed output encoding."""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from halo_server.media.h264 import (
    MAX_ACCESS_UNIT_BYTES,
    MAX_OUTPUT_ACCESS_UNIT_BYTES,
    VIDEO_CATALOG,
    H264Error,
    SpsInfo,
    VideoConfiguration,
    annexb,
    compatible,
    parse_sps,
    pps_sequence_id,
    split_annexb,
)
from halo_server.media.manager import settle_owned
from halo_server.media.worker_limits import worker_environment
from halo_server.release_execution import python_worker_command
from halo_server.release_guard import inherited_guard_fds

MAX_ENCODERS = 4
START_SECONDS = 5.0
FRAME_SECONDS = 2.0
STOP_SECONDS = 2.0
_MAX_RESPONSE = MAX_OUTPUT_ACCESS_UNIT_BYTES + 64
_HEADER = struct.Struct("!cQBI")
_COUNT_LOCK = threading.Lock()
_ENCODERS = 0
_RETIRING_WORKERS: set = set()


class VideoTransformError(H264Error):
    """The managed output cannot safely finish or retire its current operation."""


@dataclass(frozen=True)
class OutputAccessUnit:
    payload: bytes
    timestamp: int
    is_keyframe: bool
    mode: str
    source: SpsInfo
    output: SpsInfo


@dataclass(frozen=True)
class VideoCapabilities:
    ready: bool
    catalog: tuple[VideoConfiguration, ...]
    reason: str | None = None


class VideoWorker:
    """One outstanding access unit; ownership survives failed termination."""

    def __init__(self, configuration: VideoConfiguration):
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
        global _ENCODERS
        async with self._starting:
            if self._closed:
                raise VideoTransformError("Video worker is retired")
            if self._process is not None:
                return
            environment = worker_environment()
            command = python_worker_command(Path(__file__).with_name("transcode_worker.py"))
            inherited = inherited_guard_fds()
            with _COUNT_LOCK:
                if _ENCODERS >= MAX_ENCODERS:
                    raise VideoTransformError("Video encoder capacity is exhausted")
                _ENCODERS += 1
                self._reserved = True
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *command,
                    pass_fds=inherited,
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
                        raise VideoTransformError("Video worker startup was retired")
                    body = json.dumps(asdict(self.configuration), separators=(",", ":")).encode()
                    self._process.stdin.write(struct.pack("!I", len(body)) + body)
                    await self._process.stdin.drain()
                    if await self._read() != b"S":
                        raise VideoTransformError("Video encoder initialization failed")
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
            raise VideoTransformError("Video worker returned oversized data")
        return await self._process.stdout.readexactly(count)

    async def feed(self, payload: bytes, timestamp: int, *, force_idr=False):
        return await self._request(b"F", payload, timestamp, force_idr)

    async def probe(self):
        return await self._request(b"P", b"", 0, True)

    async def _request(self, kind, payload, timestamp, force):
        if self._operation.locked():
            raise VideoTransformError("Video worker already owns an access unit")
        async with self._operation:
            if (
                not isinstance(payload, bytes)
                or len(payload) > MAX_ACCESS_UNIT_BYTES
                or type(timestamp) is not int
                or not 0 <= timestamp < 2**53
                or type(force) is not bool
            ):
                raise VideoTransformError("Invalid video worker request")
            await self.start()
            if self._closed:
                raise VideoTransformError("Video worker is retired")
            try:
                async with asyncio.timeout(FRAME_SECONDS):
                    self._process.stdin.write(
                        _HEADER.pack(kind, timestamp, int(force), len(payload))
                    )
                    self._process.stdin.write(payload)
                    await self._process.stdin.drain()
                    body = await self._read()
                if self._closed or len(body) < 2 or body[:1] != b"V" or body[1] > 4:
                    raise VideoTransformError("Video worker output is unavailable")
                offset, result = 2, []
                for _ in range(body[1]):
                    if offset + 12 > len(body):
                        raise VideoTransformError("Truncated video worker result")
                    timestamp, count = struct.unpack_from("!QI", body, offset)
                    offset += 12
                    if not 0 < count <= MAX_OUTPUT_ACCESS_UNIT_BYTES or offset + count > len(body):
                        raise VideoTransformError("Invalid video worker result")
                    result.append((timestamp, body[offset : offset + count]))
                    offset += count
                if offset != len(body):
                    raise VideoTransformError("Unexpected video worker data")
                return tuple(result)
            except BaseException:
                await self.close()
                raise

    async def close(self):
        task = asyncio.create_task(self._close_owned(), name="homekit-video-worker-stop")
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
        global _ENCODERS
        async with self._closing:
            self._closed = True
            spawn = self._spawn
            if spawn is not None:
                _done, pending = await asyncio.wait({spawn}, timeout=STOP_SECONDS)
                if pending:
                    raise VideoTransformError("Video worker spawn retirement is incomplete")
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
                            raise VideoTransformError(
                                "Video worker retirement is incomplete"
                            ) from None
                if process.stdin is not None:
                    process.stdin.close()
                    with contextlib.suppress(ConnectionError, BrokenPipeError):
                        await asyncio.wait_for(process.stdin.wait_closed(), STOP_SECONDS)
                self._process = None
            if self._reserved:
                with _COUNT_LOCK:
                    _ENCODERS -= 1
                self._reserved = False


class AccessUnitAdapter:
    def __init__(self, configuration: VideoConfiguration):
        self.configuration = configuration
        self._worker: VideoWorker | None = None
        self._closed = False
        self._operation = asyncio.Lock()
        self._sps = self._pps = None
        self._source: SpsInfo | None = None
        self._output: SpsInfo | None = None
        self._epoch = None
        self._waiting_idr = True
        self._force = True
        self._mode = "waiting"
        self._adapt_requested = False
        self._last_raw = self._timestamp = None
        self._bucket_timestamp = None
        self._tokens = configuration.max_bit_rate * 1000

    @property
    def mode(self):
        return self._mode

    @property
    def source_format(self):
        return self._source

    @property
    def output_format(self):
        return self._output

    def request_keyframe(self):
        if self._mode == "adapt":
            self._force = True
        else:
            self._waiting_idr = True

    def force_adaptation(self):
        """Use real encoding at the next valid source IDR, including a caller retry."""
        self._adapt_requested = True
        if self._mode != "adapt":
            self._waiting_idr = True

    async def reconfigure(self, configuration):
        if self._operation.locked():
            raise VideoTransformError("Video configuration change is busy")
        async with self._operation:
            if self._closed:
                raise VideoTransformError("Video output is retired")
            if self.configuration != configuration:
                await self._reset(clear_parameters=False)
                self.configuration = configuration
                self._adapt_requested = False
                self._tokens = configuration.max_bit_rate * 1000

    async def _reset(self, *, clear_parameters):
        if self._worker is not None:
            await self._worker.close()
            self._worker = None
        self._waiting_idr, self._force, self._mode = True, True, "waiting"
        self._output = None
        self._last_raw = self._timestamp = self._bucket_timestamp = None
        self._tokens = self.configuration.max_bit_rate * 1000
        if clear_parameters:
            self._sps = self._pps = self._source = None
            self._adapt_requested = False

    def _copy_budget(self, payload, timestamp):
        maximum = self.configuration.max_bit_rate * 1000
        if self._bucket_timestamp is not None:
            self._tokens = min(
                maximum, self._tokens + maximum * (timestamp - self._bucket_timestamp) / 90000
            )
        self._bucket_timestamp = timestamp
        if len(payload) * 8 > self._tokens:
            return False
        self._tokens -= len(payload) * 8
        return True

    async def feed(self, payload: bytes, rtp_timestamp: int, *, capture_epoch: int = 0):
        if self._operation.locked():
            raise VideoTransformError("Video output already owns an access unit")
        async with self._operation:
            if self._closed:
                raise VideoTransformError("Video output is retired")
            if (
                type(capture_epoch) is not int
                or capture_epoch < 0
                or type(rtp_timestamp) is not int
                or not 0 <= rtp_timestamp < 2**53
            ):
                raise VideoTransformError("Invalid source video epoch or timestamp")
            if self._epoch != capture_epoch:
                await self._reset(clear_parameters=True)
                self._epoch = capture_epoch
            nals = split_annexb(payload)
            parameter_changed = False
            for nal in nals:
                if nal[0] & 31 == 7:
                    info = parse_sps(nal)
                    if self._sps is not None and self._sps != nal:
                        parameter_changed = True
                    self._sps, self._source = nal, info
                elif nal[0] & 31 == 8:
                    if len(nal) > 65536:
                        raise VideoTransformError("H.264 picture parameters exceed limit")
                    if self._pps is not None and self._pps != nal:
                        parameter_changed = True
                    self._pps = nal
            if parameter_changed:
                await self._reset(clear_parameters=False)
            keyframe = any(nal[0] & 31 == 5 for nal in nals)
            if not any(nal[0] & 31 in {1, 5} for nal in nals):
                return ()
            if (
                self._source is None
                or self._sps is None
                or self._pps is None
                or pps_sequence_id(self._pps) != self._source.sequence_id
                or self._waiting_idr
                and not keyframe
            ):
                return ()
            raw = rtp_timestamp & 0xFFFFFFFF
            if self._last_raw is None:
                timestamp = raw
            else:
                delta = ((raw - self._last_raw + 2**31) & 0xFFFFFFFF) - 2**31
                if (
                    abs(delta) > 900000
                    or delta == 0
                    or self._timestamp + delta < 0
                    or delta < 0
                    and self._source.max_reorder_frames == 0
                ):
                    await self._reset(clear_parameters=False)
                    if not keyframe:
                        return ()
                    timestamp = raw
                else:
                    timestamp = self._timestamp + delta
                    if (
                        self._source.max_reorder_frames == 0
                        and delta > 0
                        and delta * self.configuration.fps < 90000
                    ):
                        self._adapt_requested = True
            self._last_raw, self._timestamp = raw, timestamp
            self._waiting_idr = False
            if keyframe:
                types = {nal[0] & 31 for nal in nals}
                prefix = [nal for nal in (self._sps, self._pps) if nal[0] & 31 not in types]
                if prefix:
                    payload = annexb(prefix) + payload
                    if len(payload) > MAX_ACCESS_UNIT_BYTES:
                        raise VideoTransformError("H.264 access unit exceeds source limit")
            if (
                self._mode != "adapt"
                and not self._adapt_requested
                and compatible(self._source, self.configuration)
                and self._copy_budget(payload, timestamp)
            ):
                self._mode, self._output = "copy", self._source
                return (
                    OutputAccessUnit(
                        payload, timestamp, keyframe, "copy", self._source, self._source
                    ),
                )
            if self._mode != "adapt" and not keyframe:
                self._waiting_idr = True
                return ()
            self._mode = "adapt"
            if self._worker is None:
                self._worker = VideoWorker(self.configuration)
            if not self._source.progressive:
                raise VideoTransformError("Interlaced source video is not qualified")
            encoded = await self._worker.feed(payload, timestamp, force_idr=self._force)
            result = []
            for timestamp, output in encoded:
                out_nals = split_annexb(output)
                for nal in out_nals:
                    if nal[0] & 31 == 7:
                        self._output = parse_sps(nal)
                if self._output is None or not _actual_output(self._output, self.configuration):
                    raise VideoTransformError("Actual encoder output does not match selection")
                result.append(
                    OutputAccessUnit(
                        output,
                        timestamp,
                        any(nal[0] & 31 == 5 for nal in out_nals),
                        "adapt",
                        self._source,
                        self._output,
                    )
                )
            if result:
                self._force = False
            return tuple(result)

    async def close(self):
        self._closed = True
        if self._worker is not None:
            await self._worker.close()
            self._worker = None


def _actual_output(info, selected):
    return (
        info.profile == selected.profile
        and info.level_idc <= int(selected.level.replace(".", ""))
        and (info.width, info.height) == (selected.width, selected.height)
        and info.progressive
        and info.chroma_format == 1
        and info.bit_depth == 8
    )


async def retire_video_workers() -> None:
    """Retry retained output workers, including a failed account-free startup probe."""
    failures = []
    for worker in tuple(_RETIRING_WORKERS):
        try:
            await worker.close()
        except Exception as error:
            failures.append(error)
    if failures:
        raise ExceptionGroup("Video workers still require retirement", failures)


async def video_capabilities() -> VideoCapabilities:
    """Bounded native encode/SPS/independent decode check, without a Ring capture."""
    try:
        async with asyncio.timeout(20):
            await retire_video_workers()
            for width, height, profile, level in (
                (320, 240, "baseline", "3.1"),
                (320, 240, "main", "3.1"),
                (320, 240, "high", "3.1"),
                (1920, 1080, "high", "4.0"),
            ):
                selected = VideoConfiguration(
                    width, height, 15 if width == 320 else 30, profile, level, 2000
                )
                worker = VideoWorker(selected)
                try:
                    output = await worker.probe()
                    info = next(
                        parse_sps(nal)
                        for _, data in output
                        for nal in split_annexb(data)
                        if nal[0] & 31 == 7
                    )
                    if not _actual_output(info, selected):
                        raise VideoTransformError("Actual video encoder qualification failed")
                finally:
                    await worker.close()
        return VideoCapabilities(True, VIDEO_CATALOG)
    except Exception:
        return VideoCapabilities(
            False,
            (),
            "native_video_retirement_pending" if _RETIRING_WORKERS else "native_video_unavailable",
        )
