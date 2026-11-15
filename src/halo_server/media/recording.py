"""Frozen recording codecs and bounded ownership of one native fMP4 worker."""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
import threading
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

from halo_server.media.h264 import MAX_ACCESS_UNIT_BYTES, VideoConfiguration, parse_sps
from halo_server.media.manager import settle_owned
from halo_server.media.mp4 import MAX_FRAGMENT_BYTES, MAX_INIT_BYTES, inspect_fragment, inspect_init
from halo_server.media.opus import MAX_OPUS_PACKET, parse_opus
from halo_server.media.worker_limits import worker_environment
from halo_server.release_execution import python_worker_command
from halo_server.release_guard import inherited_guard_fds

START_SECONDS = 3.0
FRAME_SECONDS = 2.0
STOP_SECONDS = 1.0
_HEADER = struct.Struct("!cqqI")
_LOCK = threading.Lock()
_WORKERS = 0
_OWNED: set = set()


class RecordingError(RuntimeError):
    def __init__(self, code="recording_unavailable"):
        self.code = code
        super().__init__("Recording media is unavailable")


@dataclass(frozen=True)
class RecordingAudioConfiguration:
    sample_rate: int = 24000
    bit_rate: int = 32000
    channels: int = 1
    bitrate_mode: str = "vbr"

    def __post_init__(self):
        if (
            type(self.sample_rate) is not int
            or self.sample_rate not in {16000, 24000, 48000}
            or type(self.bit_rate) is not int
            or not 16000 <= self.bit_rate <= 128000
            or type(self.channels) is not int
            or self.channels != 1
            or self.bitrate_mode != "vbr"
        ):
            raise RecordingError("invalid_configuration")


@dataclass(frozen=True)
class RecordingConfiguration:
    video: VideoConfiguration
    audio: RecordingAudioConfiguration | None = None
    fragment_length_ms: int = 4000
    i_frame_interval_ms: int = 2000

    def __post_init__(self):
        if (
            not isinstance(self.video, VideoConfiguration)
            or (self.audio is not None and not isinstance(self.audio, RecordingAudioConfiguration))
            or type(self.fragment_length_ms) is not int
            or self.fragment_length_ms != 4000
            or type(self.i_frame_interval_ms) is not int
            or not math_ceil(Fraction(1000, self.video.fps)) <= self.i_frame_interval_ms <= 4000
        ):
            raise RecordingError("invalid_configuration")


@dataclass(frozen=True)
class RecordingPacket:
    kind: str
    payload: bytes
    sequence: int
    duration_seconds: Fraction = Fraction(0)

    @property
    def duration_ms(self):
        return math_ceil(self.duration_seconds * 1000)


def math_ceil(value):
    return (value.numerator + value.denominator - 1) // value.denominator


@dataclass(frozen=True)
class RecordingCapabilities:
    ready: bool
    reason: str | None = None


class RecordingWorker:
    """One immutable recording, one request at a time, including failed retirement."""

    def __init__(
        self,
        configuration: RecordingConfiguration,
        *,
        video_sps: bytes,
        video_pps: bytes,
        source_clocks: tuple = (),
        capture_epoch: int,
        config_epoch: int,
        valid=lambda: True,
    ):
        if (
            not isinstance(configuration, RecordingConfiguration)
            or not isinstance(video_sps, bytes)
            or not 4 <= len(video_sps) <= 65536
            or video_sps[0] & 31 != 7
            or not isinstance(video_pps, bytes)
            or not 2 <= len(video_pps) <= 65536
            or video_pps[0] & 31 != 8
            or type(capture_epoch) is not int
            or capture_epoch < 1
            or type(config_epoch) is not int
            or config_epoch < 1
            or not callable(valid)
        ):
            raise RecordingError("invalid_configuration")
        self.configuration = configuration
        self.capture_epoch, self.config_epoch = capture_epoch, config_epoch
        self._valid = valid
        self._configuration = {
            "video": asdict(configuration.video),
            "audio": asdict(configuration.audio) if configuration.audio else None,
            "fragment_length_ms": configuration.fragment_length_ms,
            "i_frame_interval_ms": configuration.i_frame_interval_ms,
            "video_sps": video_sps.hex(),
            "video_pps": video_pps.hex(),
            "clocks": [
                {
                    "kind": clock.kind,
                    "rtp_timestamp": clock.rtp_timestamp,
                    "sender_ntp": clock.sender_ntp,
                }
                for clock in source_clocks
                if clock.capture_epoch == capture_epoch and clock.kind in {"video", "audio"}
            ],
        }
        if len(self._configuration["clocks"]) > 2:
            raise RecordingError("invalid_configuration")
        self._process = None
        self._spawn = None
        self._reserved = False
        self._closed = False
        self._finished = False
        self._starting = asyncio.Lock()
        self._operation = asyncio.Lock()
        self._closing = asyncio.Lock()
        self._movie = None
        self._sequence = 0
        self._ends = {}
        self._sample_sequences = {}

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    @property
    def pending_resources(self):
        return int(self._reserved)

    def _current(self):
        if self._closed or not self._valid():
            raise RecordingError("invalidated")

    async def start(self):
        global _WORKERS
        async with self._starting:
            self._current()
            if self._process is not None:
                return
            environment = worker_environment()
            command = python_worker_command(Path(__file__).with_name("recording_worker.py"))
            inherited = inherited_guard_fds()
            with _LOCK:
                # Recording admission is scoped to each published camera by
                # the owning manager. Keep every process tracked until reaped,
                # without imposing a second, unrelated camera-count ceiling.
                _WORKERS += 1
                self._reserved = True
                _OWNED.add(self)
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
                    self._current()
                    data = json.dumps(self._configuration, separators=(",", ":")).encode()
                    if len(data) > 300000:
                        raise RecordingError("invalid_configuration")
                    self._process.stdin.write(struct.pack("!I", len(data)) + data)
                    await self._process.stdin.drain()
                    if await self._read() != b"S":
                        raise RecordingError()
            except BaseException:
                await self.close()
                raise
            finally:
                if self._spawn is spawn and spawn.done():
                    self._spawn = None

    async def _read(self):
        count = struct.unpack("!I", await self._process.stdout.readexactly(4))[0]
        if not 1 <= count <= 2 * MAX_FRAGMENT_BYTES + MAX_INIT_BYTES + 16:
            raise RecordingError("invalid_output")
        return await self._process.stdout.readexactly(count)

    async def feed(self, sample):
        kind = getattr(sample, "kind", None)
        payload = getattr(sample, "payload", None)
        stamp = getattr(sample, "rtp_timestamp", None)
        received = getattr(sample, "received_at", None)
        sequence = getattr(sample, "sequence", None)
        if (
            kind not in {"video", "audio"}
            or not isinstance(payload, bytes)
            or not payload
            or len(payload) > (MAX_ACCESS_UNIT_BYTES if kind == "video" else MAX_OPUS_PACKET)
            or type(stamp) is not int
            or not -(2**53) < stamp < 2**53
            or type(received) not in {int, float}
            or not 0 <= received < 2**43
            or type(sequence) is not int
            or sequence < 0
            or sequence <= self._sample_sequences.get(kind, -1)
            or getattr(sample, "capture_epoch", None) != self.capture_epoch
            or getattr(sample, "config_epoch", None) != self.config_epoch
        ):
            raise RecordingError("invalid_sample")
        if kind == "audio":
            if self.configuration.audio is None:
                raise RecordingError("invalidated")
            parse_opus(payload)
        result = await self._request(
            b"V" if kind == "video" else b"A", payload, stamp, int(received * 1_000_000_000)
        )
        self._sample_sequences[kind] = sequence
        return result

    async def finish(self):
        if self._finished:
            return ()
        result = await self._request(b"F", b"", 0, 0)
        self._finished = True
        return result

    async def _request(self, kind, payload, stamp, received):
        if self._operation.locked():
            raise RecordingError("busy")
        async with self._operation:
            self._current()
            if self._finished:
                raise RecordingError("invalidated")
            try:
                await self.start()
                async with asyncio.timeout(FRAME_SECONDS):
                    self._process.stdin.write(_HEADER.pack(kind, stamp, received, len(payload)))
                    for offset in range(0, len(payload), 65536):
                        self._process.stdin.write(payload[offset : offset + 65536])
                        await self._process.stdin.drain()
                    await self._process.stdin.drain()
                    body = await self._read()
                self._current()
                if body[:1] != b"O" or len(body) < 2 or body[1] > 2:
                    raise RecordingError()
                packets, offset, total = [], 2, 0
                for _ in range(body[1]):
                    if offset + 5 > len(body):
                        raise RecordingError("invalid_output")
                    output_kind, count = (
                        body[offset : offset + 1],
                        struct.unpack_from("!I", body, offset + 1)[0],
                    )
                    offset += 5
                    maximum = MAX_INIT_BYTES if output_kind == b"I" else MAX_FRAGMENT_BYTES
                    if (
                        output_kind not in {b"I", b"M"}
                        or not 1 <= count <= maximum
                        or offset + count > len(body)
                    ):
                        raise RecordingError("invalid_output")
                    data = body[offset : offset + count]
                    offset += count
                    total += count
                    if total > 2 * MAX_FRAGMENT_BYTES:
                        raise RecordingError("invalid_output")
                    if output_kind == b"I":
                        if self._movie is not None:
                            raise RecordingError("invalid_output")
                        self._movie = inspect_init(data, audio=self.configuration.audio is not None)
                        video = next(track for track in self._movie.tracks if track.kind == "video")
                        if (video.width, video.height) != (
                            self.configuration.video.width,
                            self.configuration.video.height,
                        ):
                            raise RecordingError("invalid_output")
                        actual = parse_sps(video.sps)
                        selected = self.configuration.video
                        if (
                            actual.profile != selected.profile
                            or actual.level_idc > int(selected.level.replace(".", ""))
                            or actual.frame_rate != selected.fps
                            or actual.max_reorder_frames != 0
                        ):
                            raise RecordingError("invalid_output")
                        if self.configuration.audio:
                            track = next(
                                track for track in self._movie.tracks if track.kind == "audio"
                            )
                            if (track.sample_rate, track.channels) != (
                                self.configuration.audio.sample_rate,
                                self.configuration.audio.channels,
                            ):
                                raise RecordingError("invalid_output")
                        packets.append(RecordingPacket("init", data, 0))
                    else:
                        if self._movie is None:
                            raise RecordingError("invalid_output")
                        metadata = inspect_fragment(
                            data,
                            self._movie,
                            maximum_seconds=Fraction(self.configuration.fragment_length_ms, 1000),
                        )
                        if metadata.sequence != self._sequence + 1:
                            raise RecordingError("invalid_output")
                        for track in metadata.tracks:
                            if (
                                track.identifier in self._ends
                                and track.start != self._ends[track.identifier]
                            ) or (
                                track.identifier not in self._ends
                                and track.start > 8 * track.timescale
                            ):
                                raise RecordingError("invalid_output")
                            self._ends[track.identifier] = track.start + track.duration
                        self._sequence = metadata.sequence
                        packets.append(
                            RecordingPacket(
                                "fragment", data, metadata.sequence, metadata.duration_seconds
                            )
                        )
                if offset != len(body):
                    raise RecordingError("invalid_output")
                self._current()
                return tuple(packets)
            except BaseException as error:
                await self.close()
                if isinstance(error, (asyncio.CancelledError, RecordingError)):
                    raise
                raise RecordingError(
                    "timeout" if isinstance(error, TimeoutError) else "recording_unavailable"
                ) from None

    async def close(self):
        self._closed = True
        task = asyncio.create_task(self._close_owned(), name="homekit-recording-worker-stop")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await settle_owned(task)
            raise

    async def _close_owned(self):
        global _WORKERS
        async with self._closing:
            spawn = self._spawn
            if spawn is not None:
                _, pending = await asyncio.wait({spawn}, timeout=STOP_SECONDS)
                if pending:
                    raise RecordingError("cleanup_pending")
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
                            raise RecordingError("cleanup_pending") from None
                if process.stdin is not None:
                    process.stdin.close()
                    with contextlib.suppress(ConnectionError, BrokenPipeError):
                        await asyncio.wait_for(process.stdin.wait_closed(), STOP_SECONDS)
                self._process = None
            if self._reserved:
                with _LOCK:
                    _WORKERS -= 1
                    _OWNED.discard(self)
                self._reserved = False


async def retire_recording_workers():
    failed = False
    for worker in tuple(_OWNED):
        try:
            await worker.close()
        except BaseException:
            failed = True
    if failed:
        raise RecordingError("cleanup_pending")


async def recording_capabilities():
    worker = RecordingWorker(
        RecordingConfiguration(
            VideoConfiguration(1280, 720, 15, "high", "4.0", 2000), RecordingAudioConfiguration()
        ),
        video_sps=bytes.fromhex("67640028acb402802dd080000003008000000f478c1950"),
        video_pps=bytes.fromhex("68ef3cb0"),
        capture_epoch=1,
        config_epoch=1,
    )
    try:
        packets = await worker._request(b"P", b"", 0, 0)
        if len(packets) != 2 or packets[0].kind != "init" or packets[1].kind != "fragment":
            raise RecordingError()
        return RecordingCapabilities(True)
    except asyncio.CancelledError:
        raise
    except Exception:
        return RecordingCapabilities(False, "native_recording_unavailable")
    finally:
        await worker.close()
