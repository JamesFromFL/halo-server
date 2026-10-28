"""One-image killable JPEG conversion and explicit native retirement ownership."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import struct
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from halo_server.media.jpeg import MAX_JPEG_BYTES, JpegError, SnapshotConfiguration, inspect_jpeg
from halo_server.media.manager import settle_owned

MAX_WORKERS = 2
CONVERT_SECONDS = 2.0
STOP_SECONDS = 1.0
_COUNT_LOCK = threading.Lock()
_WORKERS = 0
_RETIRING_WORKERS: set = set()
_CODES = frozenset(
    {
        "invalid_image",
        "invalid_dimensions",
        "unsupported_image",
        "unsupported_orientation",
        "busy",
        "timeout",
        "worker_failed",
        "cleanup_pending",
    }
)


class SnapshotCodecError(JpegError):
    def __init__(self, code="worker_failed"):
        super().__init__(code if code in _CODES else "worker_failed")


@dataclass(frozen=True)
class SnapshotCapabilities:
    ready: bool
    reason: str | None = None


class SnapshotWorker:
    """One admitted image, including late spawn and failed cleanup claims."""

    def __init__(self):
        self._process = None
        self._spawn = None
        self._reserved = False
        self._closed = False
        self._used = False
        self._closing = asyncio.Lock()

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    async def _start(self):
        global _WORKERS
        if self._closed:
            raise SnapshotCodecError("cleanup_pending")
        with _COUNT_LOCK:
            if _WORKERS >= MAX_WORKERS:
                raise SnapshotCodecError("busy")
            _WORKERS += 1
            self._reserved = True
        self._spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                str(Path(__file__).with_name("snapshot_worker.py")),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=65536,
                umask=0o077,
                env={
                    "LANG": os.environ.get("LANG", "C.UTF-8"),
                    "OPENBLAS_NUM_THREADS": "1",
                    "OMP_NUM_THREADS": "1",
                },
            )
        )
        self._process = await asyncio.shield(self._spawn)
        if self._closed:
            raise SnapshotCodecError("cleanup_pending")

    async def convert(self, jpeg: bytes, configuration: SnapshotConfiguration) -> bytes:
        if (
            not isinstance(jpeg, bytes)
            or not 4 <= len(jpeg) <= MAX_JPEG_BYTES
            or not isinstance(configuration, SnapshotConfiguration)
        ):
            raise SnapshotCodecError("invalid_image")
        if self._closed or self._used:
            raise SnapshotCodecError("busy")
        self._used = True
        try:
            async with asyncio.timeout(CONVERT_SECONDS):
                await self._start()
                process = self._process
                process.stdin.write(
                    struct.pack("!III", configuration.width, configuration.height, len(jpeg))
                )
                await process.stdin.drain()
                for offset in range(0, len(jpeg), 65536):
                    if self._closed:
                        raise SnapshotCodecError("cleanup_pending")
                    process.stdin.write(jpeg[offset : offset + 65536])
                    await process.stdin.drain()
                count = struct.unpack("!I", await process.stdout.readexactly(4))[0]
                if not 1 <= count <= MAX_JPEG_BYTES + 1:
                    raise SnapshotCodecError()
                result = await process.stdout.readexactly(count)
                if result[:1] == b"E":
                    code = result[1:].decode("ascii") if len(result) <= 40 else "worker_failed"
                    raise SnapshotCodecError(code)
                if result[:1] != b"J" or await process.wait() != 0 or await process.stdout.read(1):
                    raise SnapshotCodecError()
                image = result[1:]
                actual = inspect_jpeg(image)
                if (actual.width, actual.height) != (configuration.width, configuration.height):
                    raise SnapshotCodecError()
            await self._retire()
            if self._closed:
                raise SnapshotCodecError("cleanup_pending")
            return image
        except BaseException as error:
            await self.close()
            if isinstance(error, TimeoutError):
                raise SnapshotCodecError("timeout") from None
            if isinstance(error, (asyncio.CancelledError, SnapshotCodecError)):
                raise
            if isinstance(error, JpegError):
                raise SnapshotCodecError(error.code) from None
            raise SnapshotCodecError() from None

    async def close(self):
        self._closed = True
        await self._retire()

    async def _retire(self):
        task = asyncio.create_task(self._close_owned(), name="homekit-jpeg-worker-stop")
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
            spawn = self._spawn
            if spawn is not None:
                _done, pending = await asyncio.wait({spawn}, timeout=STOP_SECONDS)
                if pending:
                    raise SnapshotCodecError("cleanup_pending")
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
                            raise SnapshotCodecError("cleanup_pending") from None
                if process.stdin is not None:
                    process.stdin.close()
                    with contextlib.suppress(ConnectionError, BrokenPipeError):
                        await asyncio.wait_for(process.stdin.wait_closed(), STOP_SECONDS)
                self._process = None
            if self._reserved:
                with _COUNT_LOCK:
                    _WORKERS -= 1
                self._reserved = False


async def retire_snapshot_workers():
    failed = False
    for worker in tuple(_RETIRING_WORKERS):
        try:
            await worker.close()
        except BaseException:
            failed = True
    if failed:
        raise SnapshotCodecError("cleanup_pending")


async def snapshot_capabilities():
    try:
        await retire_snapshot_workers()
        # Fixed synthetic baseline JPEG; all native calls stay killable.
        source = base64.b64decode(
            "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIs"
            "IxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAAIAAgBAREA/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcI"
            "CQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcY"
            "GRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKj"
            "pKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/AOPr"
            "/9k="
        )
        worker = SnapshotWorker()
        try:
            output = await worker.convert(source, SnapshotConfiguration(3, 1))
            actual = inspect_jpeg(output)
            if (actual.width, actual.height) != (3, 1):
                raise SnapshotCodecError()
        finally:
            await worker.close()
        return SnapshotCapabilities(True)
    except asyncio.CancelledError:
        raise
    except Exception:
        return SnapshotCapabilities(False, "native_snapshot_unavailable")
