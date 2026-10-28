"""Observed native media metadata and finite private-transfer limits.

These types describe evidence, not a camera's advertised maximum or a selected
HomeKit output. No codec, dimensions or rate is inferred from a product name.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MAX_CONTROL_BYTES = 64 * 1024
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_INIT_BYTES = 1024 * 1024
MAX_FRAGMENT_BYTES = 8 * 1024 * 1024
MAX_STAGED_FRAGMENTS = 2
MAX_TALKBACK_PCM_FRAME_BYTES = 1920
MAX_VIDEO_DIMENSION = 8192
MAX_NATIVE_CAMERAS = 256


def _optional_integer(value, maximum: int, name: str) -> None:
    if value is not None and (type(value) is not int or not 1 <= value <= maximum):
        raise ValueError(f"invalid {name}")


def _optional_text(value, name: str) -> None:
    if value is not None and (
        not isinstance(value, str)
        or not value
        or len(value) > 32
        or any(not char.isascii() or not char.isprintable() for char in value)
    ):
        raise ValueError(f"invalid {name}")


@dataclass(frozen=True)
class VideoFormat:
    codec: str
    width: int | None = None
    height: int | None = None
    profile: str | None = None
    level: str | None = None
    frame_rate_num: int | None = None
    frame_rate_den: int | None = None

    def __post_init__(self) -> None:
        if self.codec != "h264":
            raise ValueError("unsupported native video codec")
        _optional_integer(self.width, MAX_VIDEO_DIMENSION, "video width")
        _optional_integer(self.height, MAX_VIDEO_DIMENSION, "video height")
        _optional_integer(self.frame_rate_num, 1_000_000, "frame rate numerator")
        _optional_integer(self.frame_rate_den, 1_000_000, "frame rate denominator")
        if (self.frame_rate_num is None) != (self.frame_rate_den is None):
            raise ValueError("incomplete video frame rate")
        if self.frame_rate_num is not None and self.frame_rate_num / self.frame_rate_den > 240:
            raise ValueError("video frame rate exceeds the native contract")
        _optional_text(self.profile, "video profile")
        _optional_text(self.level, "video level")


@dataclass(frozen=True)
class AudioFormat:
    codec: str
    sample_rate: int | None = None
    channels: int | None = None
    rtp_clock_rate: int | None = None
    packet_time_ms: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.codec, str) or self.codec not in {"opus", "aac", "pcm_s16le"}:
            raise ValueError("unsupported native audio codec")
        _optional_integer(self.sample_rate, 192_000, "sample rate")
        _optional_integer(self.channels, 2, "channel count")
        _optional_integer(self.rtp_clock_rate, 192_000, "RTP clock rate")
        if self.packet_time_ms is not None and (
            type(self.packet_time_ms) not in {int, float}
            or not math.isfinite(self.packet_time_ms)
            or not 0 < self.packet_time_ms <= 120
        ):
            raise ValueError("invalid audio packet time")


@dataclass(frozen=True)
class SourceFormat:
    video: VideoFormat | None = None
    audio: AudioFormat | None = None
    capture_attempt: int | None = None
    config_generation: int | None = None

    def __post_init__(self) -> None:
        if self.video is not None and not isinstance(self.video, VideoFormat):
            raise ValueError("invalid observed video format")
        if self.audio is not None and not isinstance(self.audio, AudioFormat):
            raise ValueError("invalid observed audio format")
        _optional_integer(self.capture_attempt, 2**53 - 1, "capture attempt")
        _optional_integer(self.config_generation, 2**53 - 1, "configuration generation")
