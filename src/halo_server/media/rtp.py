"""Bounded RFC6184 H264 packetization and authenticated-feedback structure.

Encryption/authentication belongs to ``srtp``. This module only accepts clear
RTCP after that boundary and never changes source NAL bytes or codec labels.
"""

from __future__ import annotations

import math
import secrets
import struct
import time
from collections import OrderedDict
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
MAX_RECEIVER_REPORTS = 32
MAX_RECENT_SENDER_REPORTS = 128
SENDER_REPORT_HISTORY_SECONDS = 60.0
RTP_HEADER_BYTES = 12
SRTP_TAG_BYTES = 10
MAX_UDP_PAYLOAD_BYTES = 1200


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
        if type(mtu) is not int or not 128 <= mtu <= 1378:
            raise RtpError("Invalid negotiated UDP packet limit")
        # HAP supplies a peer maximum (normally 1378 for IPv4 or 1228 for IPv6),
        # not a required packet size. Retain our conservative protected-UDP
        # payload ceiling, including the RTP header and SRTP authentication tag.
        maximum = min(mtu, MAX_UDP_PAYLOAD_BYTES) - RTP_HEADER_BYTES - SRTP_TAG_BYTES
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
class ReceiverReport:
    """Only the bounded report block for this output, without either SSRC."""

    fraction_lost: int
    packets_lost: int
    highest_sequence: int
    jitter: int
    lsr: int
    dlsr: int


@dataclass(frozen=True)
class Feedback:
    pli: bool = False
    nack: tuple[int, ...] = ()
    bye: bool = False
    reports: int = 0
    receiver_reports: tuple[ReceiverReport, ...] = ()


class ReceiverReportStats:
    """Read-side evidence, never a congestion or feedback admission decision.

    RFC3550 RTT is usable only when LSR identifies a recent report we actually
    sent. Local monotonic elapsed time avoids wall-clock adjustments; DLSR is
    the receiver's delay in units of 1/65536 second. No report means unknown.
    """

    def __init__(self, clock_rate: int = 90000):
        if type(clock_rate) is not int or not 1 <= clock_rate <= 192000:
            raise ValueError("Invalid receiver-report clock rate")
        self._clock_rate = clock_rate
        self._sent: OrderedDict[int, float] = OrderedDict()
        self._latest: ReceiverReport | None = None
        self._received_at: float | None = None
        self._rtt: float | None = None
        self._rtt_at: float | None = None
        self._samples = self._rtt_samples = self._unmatched = self._invalid = 0
        self._reordered = self._advance = 0

    def _prune(self, now):
        while self._sent:
            first = next(iter(self._sent.values()))
            if (
                now - first <= SENDER_REPORT_HISTORY_SECONDS
                and len(self._sent) <= MAX_RECENT_SENDER_REPORTS
            ):
                break
            self._sent.popitem(last=False)

    def sent(self, payload: bytes, now: float):
        # This is our own serialized SR, called only after a successful send.
        if not isinstance(payload, bytes) or len(payload) < 28 or not math.isfinite(now):
            return
        compact = (struct.unpack_from("!Q", payload, 8)[0] >> 16) & 0xFFFFFFFF
        if compact:
            self._sent.pop(compact, None)
            self._sent[compact] = now
        self._prune(now)

    def observe(self, reports: tuple[ReceiverReport, ...], now: float):
        if not math.isfinite(now):
            return
        self._prune(now)
        for report in reports[:MAX_RECEIVER_REPORTS]:
            if self._latest is not None:
                advance = (report.highest_sequence - self._latest.highest_sequence) & 0xFFFFFFFF
                if advance >= 0x80000000:
                    self._reordered = min(2**53 - 1, self._reordered + 1)
                    continue
                self._advance = min(2**53 - 1, self._advance + advance)
            self._latest, self._received_at = report, now
            self._samples = min(2**53 - 1, self._samples + 1)
            sent_at = self._sent.get(report.lsr) if report.lsr else None
            if sent_at is None:
                self._unmatched = min(2**53 - 1, self._unmatched + 1)
                continue
            elapsed = now - sent_at
            rtt = elapsed - report.dlsr / 65536
            if not 0 <= rtt <= SENDER_REPORT_HISTORY_SECONDS or elapsed < 0:
                self._invalid = min(2**53 - 1, self._invalid + 1)
                continue
            self._rtt, self._rtt_at = rtt, now
            self._rtt_samples = min(2**53 - 1, self._rtt_samples + 1)

    def snapshot(self, now: float):
        stale = (
            self._received_at is not None
            and now - self._received_at > SENDER_REPORT_HISTORY_SECONDS
        )
        report = self._latest if not stale else None
        rtt_valid = self._rtt_at is not None and now - self._rtt_at <= SENDER_REPORT_HISTORY_SECONDS

        def age(observed):
            return (
                min(3_600_000, round(max(0, now - observed) * 1000))
                if observed is not None
                else None
            )

        return {
            "reports": self._samples,
            "reordered_reports": self._reordered,
            "highest_sequence_advance": self._advance,
            "state": "unknown" if self._received_at is None else "stale" if stale else "current",
            "age_ms": age(self._received_at),
            "fraction_lost_256": report.fraction_lost if report is not None else None,
            "packets_lost": report.packets_lost if report is not None else None,
            "clock_rate": self._clock_rate,
            "jitter_ticks": report.jitter if report is not None else None,
            "jitter_us": round(report.jitter * 1_000_000 / self._clock_rate)
            if report is not None
            else None,
            "rtt_us": round(self._rtt * 1_000_000) if rtt_valid else None,
            "rtt_age_ms": age(self._rtt_at),
            "rtt_samples": self._rtt_samples,
            "rtt_unmatched": self._unmatched,
            "rtt_invalid": self._invalid,
        }


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
    receiver_reports = []
    nack = []
    for packet in packets:
        if isinstance(packet, (RtcpRrPacket, RtcpSrPacket)):
            for report in packet.reports:
                if report.ssrc == media_ssrc:
                    reports += 1
                    if len(receiver_reports) < MAX_RECEIVER_REPORTS:
                        receiver_reports.append(
                            ReceiverReport(
                                report.fraction_lost,
                                report.packets_lost,
                                report.highest_sequence,
                                report.jitter,
                                report.lsr,
                                report.dlsr,
                            )
                        )
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
    return Feedback(pli, tuple(dict.fromkeys(nack)), bye, reports, tuple(receiver_reports))
