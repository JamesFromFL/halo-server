"""Media profile selection for Ring camera sessions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TransportMode(StrEnum):
    """Halo media transport policy."""

    TRANSCODE = "transcode"
    AUTO = "auto"
    HYBRID = "hybrid"
    PASSTHROUGH = "passthrough"


@dataclass(frozen=True)
class EncodedMediaProfile:
    """Encoded Ring media details that affect consumer compatibility."""

    opus_frame_duration_ms: float


def opus_frame_duration_ms(packet: bytes) -> float | None:
    """Return one Opus frame duration from its RFC 6716 TOC byte."""
    if not packet:
        return None
    config = packet[0] >> 3
    if config < 12:
        return (10.0, 20.0, 40.0, 60.0)[config % 4]
    if config < 16:
        return (10.0, 20.0)[config % 2]
    return (2.5, 5.0, 10.0, 20.0)[config % 4]


def select_transport(
    requested: TransportMode,
    profile: EncodedMediaProfile | None,
) -> TransportMode | None:
    """Choose the proven video/audio path from an explicit probe result."""
    if requested is TransportMode.TRANSCODE:
        return TransportMode.TRANSCODE
    if profile is None:
        return TransportMode.TRANSCODE if requested is TransportMode.AUTO else None
    if requested is TransportMode.PASSTHROUGH:
        return TransportMode.PASSTHROUGH
    if requested is TransportMode.HYBRID:
        return TransportMode.HYBRID
    if profile.opus_frame_duration_ms == 20:
        return TransportMode.PASSTHROUGH
    return TransportMode.HYBRID
