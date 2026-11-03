"""Finite Annex-B/SPS parsing and actual selected H.264 output constraints."""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

MAX_ACCESS_UNIT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_ACCESS_UNIT_BYTES = 2 * 1024 * 1024
MAX_NALS = 256
MAX_SPS_BYTES = 64 * 1024
MAX_SOURCE_PIXELS = 8 * 1024 * 1024
MAX_OUTPUT_PIXELS = 1920 * 1080
_PROFILES = {66: "baseline", 77: "main", 100: "high"}
_LEVELS = {"3.1": (31, 3600, 108000), "3.2": (32, 5120, 216000), "4.0": (40, 8192, 245760)}


class H264Error(RuntimeError):
    """The source/configuration cannot safely satisfy this video contract."""


@dataclass(frozen=True)
class VideoConfiguration:
    width: int
    height: int
    fps: int
    profile: str
    level: str
    max_bit_rate: int
    payload_type: int = 99
    rtcp_interval: float = 0.5
    mtu: int = 1200

    def __post_init__(self):
        if (
            type(self.width) is not int
            or type(self.height) is not int
            or not 2 <= self.width <= 1920
            or not 2 <= self.height <= 1080
            or self.width % 2
            or self.height % 2
            or self.width * self.height > MAX_OUTPUT_PIXELS
            or type(self.fps) is not int
            or not 1 <= self.fps <= 30
            or self.profile not in _PROFILES.values()
            or self.level not in _LEVELS
            or type(self.max_bit_rate) is not int
            or not 1 <= self.max_bit_rate <= 20000
            or type(self.payload_type) is not int
            or not 96 <= self.payload_type <= 127
            or type(self.rtcp_interval) not in {int, float}
            or not math.isfinite(self.rtcp_interval)
            or not 0.1 <= self.rtcp_interval <= 10
            or type(self.mtu) is not int
            or not 256 <= self.mtu <= 1378
        ):
            raise H264Error("Invalid selected video configuration")
        _, maximum_frame, maximum_rate = _LEVELS[self.level]
        level_bitrate = 17500 if self.profile == "high" else 14000
        if self.level == "3.1" and self.max_bit_rate > level_bitrate:
            raise H264Error("Selected bitrate exceeds H.264 level")
        macroblocks = math.ceil(self.width / 16) * math.ceil(self.height / 16)
        if macroblocks > maximum_frame or macroblocks * self.fps > maximum_rate:
            raise H264Error("Selected geometry/rate exceeds H.264 level")


@dataclass(frozen=True)
class SpsInfo:
    profile_idc: int
    constraints: int
    level_idc: int
    width: int
    height: int
    chroma_format: int
    bit_depth: int
    progressive: bool
    frame_rate: Fraction | None
    max_reorder_frames: int | None = None
    sequence_id: int = 0

    @property
    def profile(self) -> str:
        return _PROFILES.get(self.profile_idc, "unsupported")

    @property
    def level(self) -> str:
        return f"{self.level_idc // 10}.{self.level_idc % 10}"


class _Bits:
    def __init__(self, data: bytes):
        self.data, self.bit = data, 0

    def read(self, count: int) -> int:
        if not 0 <= count <= 32 or self.bit + count > len(self.data) * 8:
            raise H264Error("Truncated H.264 sequence header")
        result = 0
        for _ in range(count):
            result = (result << 1) | ((self.data[self.bit // 8] >> (7 - self.bit % 8)) & 1)
            self.bit += 1
        return result

    def ue(self, maximum=2**31 - 1) -> int:
        zeros = 0
        while self.read(1) == 0:
            zeros += 1
            if zeros > 31:
                raise H264Error("Oversized H.264 Exp-Golomb value")
        value = (1 << zeros) - 1 + self.read(zeros)
        if value > maximum:
            raise H264Error("H.264 sequence value exceeds limit")
        return value

    def se(self, maximum=2**30) -> int:
        value = self.ue(2 * maximum)
        return (value + 1) // 2 if value & 1 else -(value // 2)


def split_annexb(payload: bytes) -> tuple[bytes, ...]:
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_ACCESS_UNIT_BYTES:
        raise H264Error("H.264 access unit exceeds byte limit")
    starts = []
    search = 0
    while True:
        found = payload.find(b"\x00\x00\x01", search)
        if found < 0:
            break
        if len(starts) >= MAX_NALS:
            raise H264Error("H.264 access unit exceeds NAL count limit")
        prefix = found - 1 if found > 0 and payload[found - 1] == 0 else found
        starts.append((prefix, found + 3))
        search = found + 3
    if not starts or any(payload[: starts[0][0]]):
        raise H264Error("Invalid H.264 Annex-B access unit")
    nals = []
    for index, (_, start) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(payload)
        nal = payload[start:end].rstrip(b"\x00")
        if not nal or nal[0] & 0x80 or not 1 <= nal[0] & 31 <= 23:
            raise H264Error("Invalid H.264 NAL unit")
        nals.append(nal)
    return tuple(nals)


def annexb(nals) -> bytes:
    value = b"".join(b"\x00\x00\x00\x01" + nal for nal in nals)
    if not value or len(value) > MAX_ACCESS_UNIT_BYTES:
        raise H264Error("H.264 access unit exceeds byte limit")
    return value


def _rbsp(data):
    output = bytearray()
    zeros = 0
    for index, value in enumerate(data):
        if zeros >= 2 and value == 3:
            if index + 1 == len(data) or data[index + 1] > 3:
                raise H264Error("Invalid H.264 emulation prevention")
            zeros = 0
            continue
        output.append(value)
        zeros = zeros + 1 if value == 0 else 0
    return bytes(output)


def _scaling(bits, size):
    last = next_scale = 8
    for _ in range(size):
        if next_scale:
            next_scale = (last + bits.se(128) + 256) % 256
        last = next_scale or last


def _hrd(bits):
    count = bits.ue(31) + 1
    bits.read(8)
    for _ in range(count):
        bits.ue()
        bits.ue()
        bits.read(1)
    bits.read(20)


def _vui(bits):
    if bits.read(1) and bits.read(8) == 255 and (not bits.read(16) or not bits.read(16)):
        raise H264Error("Invalid H.264 aspect ratio")
    if bits.read(1):
        bits.read(1)
    if bits.read(1):
        bits.read(4)
        if bits.read(1):
            bits.read(24)
    if bits.read(1):
        bits.ue(5)
        bits.ue(5)
    fps = None
    if bits.read(1):
        units, scale, fixed = bits.read(32), bits.read(32), bits.read(1)
        if not units or not scale:
            raise H264Error("Invalid H.264 frame timing")
        if fixed:
            fps = Fraction(scale, units * 2)
            if fps > 240:
                raise H264Error("H.264 frame rate exceeds source limit")
    nal_hrd = bits.read(1)
    if nal_hrd:
        _hrd(bits)
    vcl_hrd = bits.read(1)
    if vcl_hrd:
        _hrd(bits)
    if nal_hrd or vcl_hrd:
        bits.read(1)
    bits.read(1)
    reorder = None
    if bits.read(1):
        bits.read(1)
        for _ in range(4):
            bits.ue(65535)
        reorder = bits.ue(16)
        bits.ue(16)
    return fps, reorder


def parse_sps(nal: bytes) -> SpsInfo:
    if not isinstance(nal, bytes) or not 4 <= len(nal) <= MAX_SPS_BYTES or nal[0] & 0x9F != 7:
        raise H264Error("Invalid H.264 SPS")
    bits = _Bits(_rbsp(nal[1:]))
    profile, constraints, level = bits.read(8), bits.read(8), bits.read(8)
    if constraints & 3:
        raise H264Error("Reserved H.264 constraint bits are set")
    sequence_id = bits.ue(31)
    chroma, depth, separate = 1, 8, 0
    if profile in {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}:
        chroma = bits.ue(3)
        if chroma == 3:
            separate = bits.read(1)
        depth = bits.ue(6) + 8
        chroma_depth = bits.ue(6) + 8
        if chroma_depth != depth:
            raise H264Error("Unsupported mixed H.264 bit depth")
        bits.read(1)
        if bits.read(1):
            for index in range(8 if chroma != 3 else 12):
                if bits.read(1):
                    _scaling(bits, 16 if index < 6 else 64)
    bits.ue(12)
    order = bits.ue(2)
    if order == 0:
        bits.ue(12)
    elif order == 1:
        bits.read(1)
        bits.se()
        bits.se()
        for _ in range(bits.ue(255)):
            bits.se()
    bits.ue(16)
    bits.read(1)
    width_mbs, height_map = bits.ue(511) + 1, bits.ue(511) + 1
    progressive = bool(bits.read(1))
    if not progressive:
        bits.read(1)
    bits.read(1)
    crop = [bits.ue(8192) for _ in range(4)] if bits.read(1) else [0, 0, 0, 0]
    fps, reorder = _vui(bits) if bits.read(1) else (None, None)
    if bits.read(1) != 1:
        raise H264Error("Invalid H.264 SPS trailing bits")
    while bits.bit % 8:
        if bits.read(1):
            raise H264Error("Invalid H.264 SPS padding")
    if bits.bit != len(bits.data) * 8:
        raise H264Error("Unexpected H.264 SPS trailing bytes")
    chroma_array = 0 if separate else chroma
    sub_width = 1 if chroma_array in {0, 3} else 2
    sub_height = 2 if chroma_array == 1 else 1
    crop_x = sub_width if chroma_array else 1
    crop_y = sub_height * (2 - int(progressive))
    width = width_mbs * 16 - (crop[0] + crop[1]) * crop_x
    height = height_map * 16 * (2 - int(progressive)) - (crop[2] + crop[3]) * crop_y
    if not 1 <= width <= 8192 or not 1 <= height <= 8192 or width * height > MAX_SOURCE_PIXELS:
        raise H264Error("H.264 decoded geometry exceeds source limits")
    return SpsInfo(
        profile,
        constraints,
        level,
        width,
        height,
        chroma,
        depth,
        progressive,
        fps,
        0 if profile == 66 else reorder,
        sequence_id,
    )


def pps_ids(nal: bytes) -> tuple[int, int]:
    """Read bounded PPS/SPS IDs and the required PPS flags."""
    if not isinstance(nal, bytes) or not 2 <= len(nal) <= MAX_SPS_BYTES or nal[0] & 0x9F != 8:
        raise H264Error("Invalid H.264 PPS or picture parameters exceed limit")
    bits = _Bits(_rbsp(nal[1:]))
    picture_id, sequence_id = bits.ue(255), bits.ue(31)
    bits.read(2)
    return picture_id, sequence_id


def pps_sequence_id(nal: bytes) -> int:
    return pps_ids(nal)[1]


def slice_pps_id(nal: bytes) -> int:
    """Read the referenced PPS ID from a bounded non-partitioned slice."""
    if (
        not isinstance(nal, bytes)
        or not 2 <= len(nal) <= MAX_ACCESS_UNIT_BYTES
        or nal[0] & 0x80
        or nal[0] & 31 not in {1, 5}
    ):
        raise H264Error("Invalid H.264 picture slice")
    bits = _Bits(_rbsp(nal[1:]))
    bits.ue(2**24)
    bits.ue(9)
    return bits.ue(255)


def compatible(source: SpsInfo, selected: VideoConfiguration) -> bool:
    return (
        source.profile == selected.profile
        and source.level_idc <= _LEVELS[selected.level][0]
        and source.width == selected.width
        and source.height == selected.height
        and source.chroma_format == 1
        and source.bit_depth == 8
        and source.progressive
        and source.max_reorder_frames == 0
        and source.frame_rate is not None
        and source.frame_rate <= selected.fps
    )


VIDEO_CATALOG = tuple(
    VideoConfiguration(width, height, fps, profile, level, 2000)
    for width, height, fps in (
        (320, 240, 15),
        (640, 360, 30),
        (640, 480, 30),
        (1280, 720, 30),
        (1920, 1080, 30),
    )
    for profile in ("baseline", "main", "high")
    for level in ("3.1", "3.2", "4.0")
    if math.ceil(width / 16) * math.ceil(height / 16) <= _LEVELS[level][1]
    and math.ceil(width / 16) * math.ceil(height / 16) * fps <= _LEVELS[level][2]
)
