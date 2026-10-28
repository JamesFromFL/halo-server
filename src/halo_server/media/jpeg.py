"""Bounded admission for one supported JPEG image, before native allocation."""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_JPEG_BYTES = 8 * 1024 * 1024
MAX_SOURCE_PIXELS = 8 * 1024 * 1024
MAX_OUTPUT_PIXELS = 8_000_000
MAX_SOURCE_SIDE = 8192
MAX_SEGMENTS = 1024
MAX_SCANS = 64
MAX_RESTARTS = 131072
_BOUNDARY = re.compile(b"\xff(?=[^\x00])")


class JpegError(ValueError):
    """Content-free malformed/unsupported image result."""

    def __init__(self, code="invalid_image"):
        self.code = code
        super().__init__("JPEG is invalid or unsupported")


@dataclass(frozen=True)
class SnapshotConfiguration:
    width: int
    height: int

    def __post_init__(self):
        if (
            type(self.width) is not int
            or type(self.height) is not int
            or not 1 <= self.width <= 4096
            or not 1 <= self.height <= 4096
            or self.width * self.height > MAX_OUTPUT_PIXELS
        ):
            raise JpegError("invalid_dimensions")


@dataclass(frozen=True)
class JpegInfo:
    width: int
    height: int
    components: int
    progressive: bool
    orientation: int
    scans: int
    baseline: bool


def _orientation(body):
    if not body.startswith(b"Exif\0\0"):
        return None
    tiff = body[6:]
    if len(tiff) < 8 or tiff[:2] not in {b"II", b"MM"}:
        raise JpegError()
    endian = "little" if tiff[:2] == b"II" else "big"
    if int.from_bytes(tiff[2:4], endian) != 42:
        raise JpegError()
    offset = int.from_bytes(tiff[4:8], endian)
    if not 8 <= offset <= len(tiff) - 2:
        raise JpegError()
    count = int.from_bytes(tiff[offset : offset + 2], endian)
    if count > 128 or offset + 2 + count * 12 + 4 > len(tiff):
        raise JpegError()
    found = None
    for index in range(count):
        entry = tiff[offset + 2 + index * 12 : offset + 14 + index * 12]
        if int.from_bytes(entry[:2], endian) != 274:
            continue
        if (
            found is not None
            or int.from_bytes(entry[2:4], endian) != 3
            or int.from_bytes(entry[4:8], endian) != 1
        ):
            raise JpegError()
        found = int.from_bytes(entry[8:10], endian)
        if found != 1:
            raise JpegError("unsupported_orientation")
    return 1


def _quantization(body, tables):
    offset = 0
    while offset < len(body):
        value = body[offset]
        offset += 1
        precision, identifier = value >> 4, value & 15
        # Every admitted SOF uses eight-bit samples (T.81 B.2.4.1).
        if precision != 0 or identifier > 3:
            raise JpegError()
        stride = precision + 1
        size = 64 * stride
        if offset + size > len(body):
            raise JpegError()
        if any(
            int.from_bytes(body[pos : pos + stride], "big") == 0
            for pos in range(offset, offset + size, stride)
        ):
            raise JpegError()
        tables.add(identifier)
        offset += size
    if offset == 0:
        raise JpegError()


def _huffman(body, tables):
    offset = 0
    while offset < len(body):
        if offset + 17 > len(body):
            raise JpegError()
        value = body[offset]
        kind, identifier = value >> 4, value & 15
        counts = body[offset + 1 : offset + 17]
        size = sum(counts)
        offset += 17
        if kind > 1 or identifier > 3 or not 1 <= size <= 256 or offset + size > len(body):
            raise JpegError()
        slots = 1
        for count in counts:
            slots = slots * 2 - count
            if slots < 0:
                raise JpegError()
        # JPEG reserves the all-ones code for entropy padding.
        if slots == 0:
            raise JpegError()
        symbols = body[offset : offset + size]
        if len(set(symbols)) != size or (kind == 0 and any(symbol > 11 for symbol in symbols)):
            raise JpegError()
        if kind == 1 and any(symbol & 15 > 10 for symbol in symbols):
            raise JpegError()
        tables.add((kind, identifier))
        offset += size
    if offset == 0:
        raise JpegError()


def inspect_jpeg(data: bytes) -> JpegInfo:
    """Validate finite JPEG structure and supported pixel/profile constraints.

    This cannot establish entropy completeness or decoder safety. Native work
    still runs in an isolated process and must reject corruption and wrong
    frame count/geometry. This function never allocates decoded pixel buffers.
    """
    if (
        not isinstance(data, bytes)
        or not 4 <= len(data) <= MAX_JPEG_BYTES
        or data[:2] != b"\xff\xd8"
    ):
        raise JpegError()
    offset, segments, scans, restarts = 2, 0, 0, 0
    frame = None
    components, quantization, huffman = {}, set(), set()
    approximation = {}
    baseline_seen = set()
    orientation = None
    in_scan = False
    restart_interval, next_restart = 0, 0
    while offset < len(data):
        if in_scan:
            match = _BOUNDARY.search(data, offset)
            if match is None:
                raise JpegError()
            offset = match.start()
        if data[offset] != 255:
            raise JpegError()
        offset += 1
        fills = 0
        while offset < len(data) and data[offset] == 255:
            fills += 1
            offset += 1
            if fills > 64:
                raise JpegError()
        if offset >= len(data):
            raise JpegError()
        marker = data[offset]
        offset += 1
        if in_scan and 0xD0 <= marker <= 0xD7:
            restarts += 1
            if not restart_interval or marker != 0xD0 + next_restart or restarts > MAX_RESTARTS:
                raise JpegError()
            next_restart = (next_restart + 1) % 8
            continue
        in_scan = False
        if marker == 0xD9:
            if (
                frame is None
                or not scans
                or offset != len(data)
                or any(approximation[identifier][0] is None for identifier in components)
                or (frame[3] and any(identifier > 1 for _, identifier in huffman))
            ):
                raise JpegError()
            return JpegInfo(
                frame[0], frame[1], len(components), frame[2], orientation or 1, scans, frame[3]
            )
        if marker in {0, 1, 0xD8} or 0xD0 <= marker <= 0xD7:
            raise JpegError()
        segments += 1
        if segments > MAX_SEGMENTS or offset + 2 > len(data):
            raise JpegError()
        size = int.from_bytes(data[offset : offset + 2], "big")
        if size < 2 or offset + size > len(data):
            raise JpegError()
        body = data[offset + 2 : offset + size]
        offset += size
        if marker in set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}:
            if marker not in {0xC0, 0xC1, 0xC2} or frame is not None or len(body) < 6:
                raise JpegError("unsupported_image")
            height, width, count = (
                int.from_bytes(body[1:3], "big"),
                int.from_bytes(body[3:5], "big"),
                body[5],
            )
            if (
                body[0] != 8
                or count not in {1, 3}
                or len(body) != 6 + count * 3
                or not 1 <= width <= MAX_SOURCE_SIDE
                or not 1 <= height <= MAX_SOURCE_SIDE
                or width * height > MAX_SOURCE_PIXELS
            ):
                raise JpegError("unsupported_image")
            blocks = 0
            for index in range(count):
                identifier, sampling, table = body[6 + 3 * index : 9 + 3 * index]
                horizontal, vertical = sampling >> 4, sampling & 15
                if (
                    identifier in components
                    or not 1 <= horizontal <= 4
                    or not 1 <= vertical <= 4
                    or table > 3
                ):
                    raise JpegError()
                components[identifier] = table
                approximation[identifier] = [None] * 64
                blocks += horizontal * vertical
            if blocks > 10:
                raise JpegError("unsupported_image")
            frame = width, height, marker == 0xC2, marker == 0xC0
        elif marker == 0xDB:
            _quantization(body, quantization)
        elif marker == 0xC4:
            _huffman(body, huffman)
        elif marker == 0xDD:
            if len(body) != 2:
                raise JpegError()
            restart_interval = int.from_bytes(body, "big")
        elif marker == 0xDA:
            scans += 1
            if frame is None or scans > MAX_SCANS or len(body) < 6:
                raise JpegError()
            count = body[0]
            if not 1 <= count <= len(components) or len(body) != 4 + count * 2:
                raise JpegError()
            first, last, successive = body[-3:]
            high, low = successive >> 4, successive & 15
            if (
                not 0 <= first <= last <= 63
                or high > 13
                or low > 13
                or (high != 0 and high != low + 1)
            ):
                raise JpegError()
            if frame[2]:
                if (first == 0 and last != 0) or (first != 0 and count != 1):
                    raise JpegError()
            elif (first, last, successive) != (0, 63, 0):
                raise JpegError()
            selected = set()
            for index in range(count):
                identifier, table = body[1 + index * 2 : 3 + index * 2]
                dc, ac = table >> 4, table & 15
                if (
                    identifier not in components
                    or identifier in selected
                    or dc > 3
                    or ac > 3
                    or (frame[3] and (dc > 1 or ac > 1))
                    or components[identifier] not in quantization
                    or (first == 0 and high == 0 and (0, dc) not in huffman)
                    or (last > 0 and (1, ac) not in huffman)
                ):
                    raise JpegError()
                if not frame[2] and identifier in baseline_seen:
                    raise JpegError()
                selected.add(identifier)
                baseline_seen.add(identifier)
                for coefficient in range(first, last + 1):
                    prior = approximation[identifier][coefficient]
                    if (high == 0 and prior is not None) or (high != 0 and prior != high):
                        raise JpegError()
                    approximation[identifier][coefficient] = low
            next_restart, in_scan = 0, True
        elif 0xE0 <= marker <= 0xEF:
            if marker == 0xE1:
                value = _orientation(body)
                if value is not None:
                    if orientation is not None:
                        raise JpegError()
                    orientation = value
        elif marker != 0xFE:
            raise JpegError("unsupported_image")
    raise JpegError()
