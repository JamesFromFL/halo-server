"""Bounded RFC6184 H264 packetization and authenticated-feedback structure.

Encryption/authentication belongs to ``srtp``. This module only accepts clear
RTCP after that boundary and never changes source NAL bytes or codec labels.
"""

from __future__ import annotations

import secrets
import struct
import time
from dataclasses import dataclass

from aiortc.rtp import (
    RtcpByePacket,
    RtcpPacket,
    RtcpPsfbPacket,
    RtcpRrPacket,
    RtcpRtpfbPacket,
    RtcpSenderInfo,
    RtcpSrPacket,
)

from halo_server.media.h264 import H264Error, split_annexb

MAX_ACCESS_UNIT_BYTES = 2 * 1024 * 1024
MAX_NAL_UNITS = 256
MAX_RTCP_BYTES = 16 * 1024
MAX_RTCP_PACKETS = 32
MAX_NACK_PACKETS = 128
RTP_HEADER_BYTES = 12
SRTP_TAG_BYTES = 10


class RtpError(ValueError):
    """Malformed or oversized media, without captured contents in its message."""


def annexb_nalus(payload: bytes) -> tuple[bytes, ...]:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_ACCESS_UNIT_BYTES:
        raise RtpError("H264 access unit exceeds its byte limit")
    try:
        return split_annexb(payload)
    except H264Error as exc:
        raise RtpError("H264 access unit has an invalid Annex-B structure") from exc


@dataclass(frozen=True)
class RtpDatagram:
    sequence: int
    timestamp: int
    payload_bytes: int
    packet: bytes


class H264Packetizer:
    """One sender's sequence/SSRC state, preserved through reconfiguration."""

    def __init__(self, ssrc: int, *, sequence: int | None = None):
        if type(ssrc) is not int or not 1 <= ssrc <= 0xFFFFFFFF:
            raise RtpError("Invalid RTP synchronization source")
        self.ssrc = ssrc
        self.sequence = secrets.randbelow(65536) if sequence is None else sequence
        if type(self.sequence) is not int or not 0 <= self.sequence <= 0xFFFF:
            raise RtpError("Invalid RTP initial sequence")

    def packetize(
        self, payload: bytes, timestamp: int, *, payload_type: int, mtu: int
    ) -> tuple[RtpDatagram, ...]:
        if type(timestamp) is not int or not 0 <= timestamp <= 0xFFFFFFFF:
            raise RtpError("Invalid RTP timestamp")
        if type(payload_type) is not int or not 96 <= payload_type <= 127:
            raise RtpError("Invalid dynamic RTP payload type")
        if type(mtu) is not int or not 128 <= mtu <= 1200:
            raise RtpError("Invalid negotiated UDP packet limit")
        maximum = mtu - RTP_HEADER_BYTES - SRTP_TAG_BYTES
        chunks = []
        for nal in annexb_nalus(payload):
            if len(nal) <= maximum:
                chunks.append(nal)
            else:
                content, size = nal[1:], maximum - 2
                for offset in range(0, len(content), size):
                    end = offset + size >= len(content)
                    header = (nal[0] & 31) | (0x80 if offset == 0 else 0) | (0x40 if end else 0)
                    chunks.append(
                        bytes(((nal[0] & 0xE0) | 28, header)) + content[offset : offset + size]
                    )
        result = []
        for index, chunk in enumerate(chunks):
            marker = 0x80 if index == len(chunks) - 1 else 0
            packet = (
                struct.pack(
                    "!BBHII", 0x80, marker | payload_type, self.sequence, timestamp, self.ssrc
                )
                + chunk
            )
            result.append(RtpDatagram(self.sequence, timestamp, len(chunk), packet))
            self.sequence = (self.sequence + 1) & 0xFFFF
        return tuple(result)


def sender_report(
    ssrc: int, timestamp: int, packets: int, octets: int, *, now: float | None = None
):
    wall = time.time() if now is None else now
    seconds = int(wall) + 2_208_988_800
    fraction = int((wall % 1) * (1 << 32))
    return bytes(
        RtcpSrPacket(
            ssrc=ssrc,
            sender_info=RtcpSenderInfo(
                ntp_timestamp=((seconds & 0xFFFFFFFF) << 32) | fraction,
                rtp_timestamp=timestamp & 0xFFFFFFFF,
                packet_count=packets & 0xFFFFFFFF,
                octet_count=octets & 0xFFFFFFFF,
            ),
        )
    )


@dataclass(frozen=True)
class Feedback:
    pli: bool = False
    nack: tuple[int, ...] = ()
    bye: bool = False
    reports: int = 0


def parse_feedback(payload: bytes, media_ssrc: int) -> Feedback:
    """Parse authenticated compound RTCP; empty RR is valid liveness traffic."""
    if not isinstance(payload, bytes) or not 8 <= len(payload) <= MAX_RTCP_BYTES:
        raise RtpError("RTCP packet exceeds its bounds")
    try:
        packets = RtcpPacket.parse(payload)
    except (ValueError, struct.error, IndexError) as exc:
        raise RtpError("Malformed RTCP structure") from exc
    if not packets or len(packets) > MAX_RTCP_PACKETS:
        raise RtpError("RTCP compound packet count exceeds its limit")
    pli = bye = False
    reports = 0
    nack = []
    for packet in packets:
        if isinstance(packet, RtcpRrPacket):
            reports += sum(report.ssrc == media_ssrc for report in packet.reports)
        elif isinstance(packet, RtcpPsfbPacket) and packet.media_ssrc == media_ssrc:
            if packet.fmt == 1:
                if packet.fci:
                    raise RtpError("PLI feedback has invalid data")
                pli = True
        elif isinstance(packet, RtcpRtpfbPacket) and packet.media_ssrc == media_ssrc:
            if packet.fmt == 1:
                # aiortc's parser expands BLP offsets beyond 65535. The wire
                # identifiers are modulo-16-bit even at the rollover boundary.
                nack.extend(sequence & 0xFFFF for sequence in packet.lost)
                if len(nack) > MAX_NACK_PACKETS:
                    raise RtpError("NACK packet count exceeds its limit")
        elif isinstance(packet, RtcpByePacket):
            bye = True
    return Feedback(pli, tuple(dict.fromkeys(nack)), bye, reports)
