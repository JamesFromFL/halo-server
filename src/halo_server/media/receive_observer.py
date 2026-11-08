"""Opt-in, bounded Linux packet-header observer, independent of Halo's sockets.

Run with ``python -m halo_server.media.receive_observer --help``. No socket is
opened on import or validation. This tool does not discover or open cameras.
Kernel BPF admits only one incoming IPv4/UDP tuple and selected RTP types, and
truncates each admitted packet immediately after the fixed twelve-byte RTP header.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import ipaddress
import json
import os
import re
import socket
import stat
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

MAX_SECONDS = 300
MAX_PACKETS = 100_000
MAX_BYTES = 32 * 1024 * 1024
SO_ATTACH_FILTER, SO_LOCK_FILTER, SO_TIMESTAMPNS_NEW = 26, 44, 64
SOL_PACKET, PACKET_STATISTICS, PACKET_IGNORE_OUTGOING = 263, 6, 23
MAX_HEADER = 22 + 60 + 8 + 12  # two VLAN tags, longest IPv4 header, UDP, fixed RTP


class ObserverError(ValueError):
    """Finite failure code; never include endpoints or packet data in messages."""


@dataclass(frozen=True)
class Flow:
    interface: str
    local_address: str
    local_port: int
    remote_address: str
    remote_port: int
    payload_types: tuple[int, ...]
    transport: str = "direct-srtp"

    def __post_init__(self):
        if self.transport != "direct-srtp":
            raise ObserverError("unsupported_transport_use_direct_srtp_only")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", self.interface):
            raise ObserverError("invalid_interface")
        for address in (self.local_address, self.remote_address):
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                raise ObserverError("literal_ipv4_addresses_required") from None
            if parsed.version != 4:
                raise ObserverError("unsupported_ipv6")
            if parsed.is_multicast or parsed.is_unspecified or int(parsed) == 0xFFFFFFFF:
                raise ObserverError("unicast_addresses_required")
        if any(
            type(p) is not int or not 1 <= p <= 65535 for p in (self.local_port, self.remote_port)
        ):
            raise ObserverError("invalid_port")
        if (
            not 1 <= len(self.payload_types) <= 8
            or len(set(self.payload_types)) != len(self.payload_types)
            or any(type(p) is not int or not 96 <= p <= 127 for p in self.payload_types)
        ):
            raise ObserverError("one_to_eight_distinct_dynamic_rtp_types_required")


def packet_filter(flow: Flow):
    """Classic BPF, Ethernet IPv4 with zero, one or two inline VLAN tags.

    Rejected fragments/non-IPv4/other transports are outside coverage. Dynamic
    IPv4 IHL determines UDP and RTP offsets. RET A truncates in the kernel;
    even encrypted media bytes never cross this observer's socket boundary.
    """
    code, labels = [], {}

    def label(name):
        labels[name] = len(code)

    def emit(op, k=0, yes=0, no=0):
        code.append((op, yes, no, k))

    emit(0x28, 12)  # LD H ABS ethertype
    emit(0x15, 0x0800, "ip14")
    emit(0x15, 0x8100, "vlan1")
    emit(0x15, 0x88A8, "vlan1", "reject")
    label("vlan1")
    emit(0x28, 16)
    emit(0x15, 0x0800, "ip18")
    emit(0x15, 0x8100, "vlan2")
    emit(0x15, 0x88A8, "vlan2", "reject")
    label("vlan2")
    emit(0x28, 20)
    emit(0x15, 0x0800, "ip22", "reject")
    for base in (14, 18, 22):
        label(f"ip{base}")
        emit(0x30, base)  # LD B ABS
        emit(0x54, 0xF0)  # AND K
        emit(0x15, 0x40, no="reject")
        emit(0x30, base + 9)
        emit(0x15, 17, no="reject")
        emit(0x28, base + 6)
        emit(0x45, 0x3FFF, "reject")  # JSET: reject MF and nonzero fragment offset
        emit(0x20, base + 12)
        emit(0x15, int(ipaddress.IPv4Address(flow.remote_address)), no="reject")
        emit(0x20, base + 16)
        emit(0x15, int(ipaddress.IPv4Address(flow.local_address)), no="reject")
        emit(0x30, base)
        emit(0x54, 15)
        emit(0x35, 5, no="reject")  # JGE: IHL at least five words
        emit(0x64, 2)  # LSH K
        emit(0x07)  # TAX: X = IHL bytes
        emit(0x48, base)  # LD H IND: UDP source
        emit(0x15, flow.remote_port, no="reject")
        emit(0x48, base + 2)
        emit(0x15, flow.local_port, no="reject")
        emit(0x50, base + 8)  # LD B IND: RTP version
        emit(0x54, 0xC0)
        emit(0x15, 0x80, no="reject")
        emit(0x50, base + 9)
        emit(0x54, 0x7F)
        for payload_type in flow.payload_types:
            emit(0x15, payload_type, f"accept{base}")
        emit(0x05, "reject")  # JA
        label(f"accept{base}")
        emit(0x87)  # TXA
        emit(0x04, base + 20)  # ADD K: L2 + IHL + UDP + fixed RTP
        emit(0x16)  # RET A
    label("reject")
    emit(0x06)  # RET K 0
    result = []
    for index, (op, yes, no, k) in enumerate(code):
        yes = labels[yes] - index - 1 if isinstance(yes, str) else yes
        no = labels[no] - index - 1 if isinstance(no, str) else no
        k = labels[k] - index - 1 if isinstance(k, str) else k
        assert 0 <= yes <= 255 and 0 <= no <= 255
        result.append((op, yes, no, k))
    return tuple(result)


def _attach(sock, instructions):
    class Filter(ctypes.Structure):
        _fields_ = [
            ("code", ctypes.c_ushort),
            ("jt", ctypes.c_ubyte),
            ("jf", ctypes.c_ubyte),
            ("k", ctypes.c_uint32),
        ]

    class Program(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]

    entries = (Filter * len(instructions))(*(Filter(*row) for row in instructions))
    program = Program(len(instructions), entries)
    sock.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, bytes(program))
    sock.setsockopt(socket.SOL_SOCKET, SO_LOCK_FILTER, 1)


def open_observer(flow):
    if sys.platform != "linux" or not hasattr(socket, "AF_PACKET"):
        raise ObserverError("linux_packet_socket_required")
    # Protocol zero: receive nothing until exact filter is attached and locked.
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)
    try:
        _attach(sock, packet_filter(flow))
        sock.setsockopt(SOL_PACKET, PACKET_IGNORE_OUTGOING, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
        sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS_NEW, 1)
        # ETH_P_ALL also admits inline VLAN ethertypes; the locked BPF still
        # rejects every packet outside the exact configured IPv4 UDP flow.
        sock.bind((flow.interface, 0x0003))
        if sock.getsockname()[3] != 1:  # ARPHRD_ETHER, not loopback/cooked/tunnel
            raise ObserverError("unsupported_link_type")
        sock.settimeout(0.2)
        return sock
    except BaseException:
        sock.close()
        raise


def parse_header(data, flow):
    """Recheck the tuple and bounds even after kernel filtering; retain scalars only."""
    if len(data) < 14:
        raise ObserverError("short_header")
    base, ether = 14, struct.unpack_from("!H", data, 12)[0]
    while ether in (0x8100, 0x88A8):
        if base >= 22 or len(data) < base + 4:
            raise ObserverError("unsupported_vlan_depth")
        ether = struct.unpack_from("!H", data, base + 2)[0]
        base += 4
    if ether != 0x0800 or len(data) < base + 20 or data[base] >> 4 != 4:
        raise ObserverError("unsupported_ip_header")
    ihl = (data[base] & 15) * 4
    if ihl < 20 or len(data) < base + ihl + 20 or data[base + 9] != 17:
        raise ObserverError("short_or_non_udp_header")
    if struct.unpack_from("!H", data, base + 6)[0] & 0x3FFF:
        raise ObserverError("unsupported_fragment")
    if data[base + 12 : base + 16] != socket.inet_aton(flow.remote_address) or data[
        base + 16 : base + 20
    ] != socket.inet_aton(flow.local_address):
        raise ObserverError("filter_mismatch")
    udp = base + ihl
    source, destination, length = struct.unpack_from("!HHH", data, udp)
    if source != flow.remote_port or destination != flow.local_port:
        raise ObserverError("filter_mismatch")
    total = struct.unpack_from("!H", data, base + 2)[0]
    if length < 20 or total != ihl + length:
        raise ObserverError("invalid_datagram_length")
    first, second, seq, timestamp, ssrc = struct.unpack_from("!BBHII", data, udp + 8)
    if first >> 6 != 2 or (second & 127) not in flow.payload_types:
        raise ObserverError("unsupported_rtp_type")
    if length < 20 + 4 * (first & 15) + (4 if first & 16 else 0):
        raise ObserverError("invalid_rtp_length")
    return ssrc, {
        "seq": seq,
        "rtp_timestamp": timestamp,
        "payload_type": second & 127,
        "marker": bool(second & 128),
        "udp_bytes": length,
        "srtp_bytes": length - 8,
        "csrc_count": first & 15,
        "extension": bool(first & 16),
        "vlan_depth": (base - 14) // 4,
    }


def kernel_timestamp(ancillary):
    values = []
    for level, kind, value in ancillary:
        if level == socket.SOL_SOCKET and kind == SO_TIMESTAMPNS_NEW:
            if len(value) != 16:
                raise ObserverError("invalid_kernel_timestamp")
            seconds, nanos = struct.unpack("=qq", value)
            if seconds < 0 or not 0 <= nanos < 10**9:
                raise ObserverError("invalid_kernel_timestamp")
            values.append(seconds * 10**9 + nanos)
    if len(values) != 1:
        raise ObserverError("missing_or_duplicate_kernel_timestamp")
    return values[0]


def clock_anchor():
    before = time.monotonic_ns()
    wall = time.time_ns()
    return {
        "kind": "clock_anchor",
        "monotonic_before_ns": before,
        "realtime_ns": wall,
        "monotonic_after_ns": time.monotonic_ns(),
    }


class Artifact:
    """New private directory, exclusive regular files, finite buffered writes."""

    def __init__(self, path):
        path = Path(path).absolute()
        if path.parent.resolve(strict=True) != path.parent:
            raise ObserverError("symlink_output_parent")
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parent = os.fstat(parent_fd)
            if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o022:
                raise ObserverError("unsafe_output_parent")
            # Pin creation/open to the same checked parent despite path renames.
            os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
            self.fd = os.open(
                path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
        finally:
            os.close(parent_fd)
        self.bytes = 0
        self.digest = hashlib.sha256()
        try:
            info = os.fstat(self.fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ObserverError("unsafe_output_directory")
            self.events = os.fdopen(self._open("headers.jsonl"), "wb", buffering=65536)
        except BaseException:
            os.close(self.fd)
            raise

    def _open(self, name):
        return os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd
        )

    def write(self, row):
        value = json.dumps(row, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if self.bytes + len(value) > MAX_BYTES:
            raise ObserverError("artifact_byte_limit")
        self.events.write(value)
        self.bytes += len(value)
        self.digest.update(value)

    def close(self, summary):
        try:
            self.events.close()
            with os.fdopen(self._open("summary.json"), "w") as file:
                json.dump(summary, file, indent=2, allow_nan=False)
                file.write("\n")
        finally:
            os.close(self.fd)


def observe(flow, directory, *, seconds=120, packets=MAX_PACKETS, opener=open_observer):
    if type(seconds) is not int or not 1 <= seconds <= MAX_SECONDS:
        raise ObserverError("invalid_duration")
    if type(packets) is not int or not 1 <= packets <= MAX_PACKETS:
        raise ObserverError("invalid_packet_limit")
    artifact = Artifact(directory)
    summary = {
        "schema_version": 1,
        "stop_reason": "error",
        "packets": 0,
        "received_headers": 0,
        "kernel_packets_minus_read": None,
        "malformed_headers": 0,
        "ancillary_failures": 0,
        "unexpected_truncations": 0,
        "kernel_packets": None,
        "kernel_drops": None,
        "statistics_available": False,
        "coverage_started_monotonic_ns": None,
        "coverage_ended_monotonic_ns": None,
        "statistics_before_monotonic_ns": None,
        "statistics_after_monotonic_ns": None,
        "final_anchor_written": False,
        "final_anchor_failure": None,
        "track_count": 0,
        "source_identity_ambiguous": False,
        "clock_step_detected": False,
        "duration_limit_seconds": seconds,
        "packet_limit": packets,
        "byte_limit": MAX_BYTES,
        "receive_buffer_bytes": None,
        "timestamp_source": "SO_TIMESTAMPNS_NEW_realtime_software",
        "headers_authenticated": False,
        "media_payload_retained": False,
        "coverage": "selected_ipv4_direct_srtp_dynamic_types_ethernet_up_to_two_vlan_tags",
        "excluded": [
            "ipv6",
            "ip_fragments",
            "turn",
            "tcp",
            "rtcp",
            "stun",
            "dtls",
            "other_flows",
            "other_payload_types",
            "more_than_two_vlan_tags",
        ],
        "flow_selection": "operator_supplied_no_live_owner_binding",
        "complete_supported_observation": False,
    }
    sock, tracks, seen_types = None, {}, set()
    start = time.monotonic_ns()
    summary["started_monotonic_ns"] = start
    last_anchor, last_offset = 0, None
    try:
        sock = opener(flow)
        summary["coverage_started_monotonic_ns"] = time.monotonic_ns()
        summary["receive_buffer_bytes"] = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        while time.monotonic_ns() - start < seconds * 10**9:
            now = time.monotonic_ns()
            if not last_anchor or now - last_anchor >= 10**9:
                anchor = clock_anchor()
                offset = (
                    anchor["realtime_ns"]
                    - (anchor["monotonic_before_ns"] + anchor["monotonic_after_ns"]) // 2
                )
                if last_offset is not None and abs(offset - last_offset) > 5_000_000:
                    summary["clock_step_detected"] = True
                artifact.write(anchor)
                last_anchor, last_offset = now, offset
            try:
                data, ancillary, flags, _address = sock.recvmsg(MAX_HEADER, socket.CMSG_SPACE(16))
            except TimeoutError:
                continue
            observed = time.monotonic_ns()
            summary["received_headers"] += 1
            if flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC):
                summary["unexpected_truncations"] += 1
                continue
            try:
                stamp = kernel_timestamp(ancillary)
            except ObserverError:
                summary["ancillary_failures"] += 1
                continue
            try:
                ssrc, fields = parse_header(data, flow)
            except ObserverError:
                summary["malformed_headers"] += 1
                continue
            identity = (ssrc, fields["payload_type"])
            if identity not in tracks:
                if len(tracks) >= 8:
                    summary["stop_reason"] = "track_limit"
                    break
                if fields["payload_type"] in seen_types:
                    summary["source_identity_ambiguous"] = True
                seen_types.add(fields["payload_type"])
                tracks[identity] = len(tracks) + 1
            artifact.write(
                {
                    "kind": "rtp_header",
                    "track_id": tracks[identity],
                    "kernel_realtime_ns": stamp,
                    "observed_monotonic_ns": observed,
                    **fields,
                }
            )
            summary["packets"] += 1
            if summary["packets"] >= packets:
                summary["stop_reason"] = "packet_limit"
                break
        else:
            summary["stop_reason"] = "duration"
    except KeyboardInterrupt:
        summary["stop_reason"] = "interrupted"
    except PermissionError:
        summary["stop_reason"] = "permission_denied_CAP_NET_RAW_required"
    except ObserverError as exc:
        summary["stop_reason"] = str(exc)
    except OSError:
        summary["stop_reason"] = "socket_or_artifact_io_error"
    finally:
        if sock is not None:
            # Coverage ends before statistics: receive remains enabled until
            # close, so any later uncounted cleanup tail is outside this claim.
            summary["coverage_ended_monotonic_ns"] = time.monotonic_ns()
            summary["statistics_before_monotonic_ns"] = time.monotonic_ns()
            try:
                value = sock.getsockopt(SOL_PACKET, PACKET_STATISTICS, 8)
                summary["kernel_packets"], summary["kernel_drops"] = struct.unpack("=II", value)
                summary["kernel_packets_minus_read"] = (
                    summary["kernel_packets"] - summary["received_headers"]
                )
                summary["statistics_available"] = True
            except (OSError, struct.error):
                pass
            finally:
                summary["statistics_after_monotonic_ns"] = time.monotonic_ns()
            sock.close()
        summary["stopped_monotonic_ns"] = time.monotonic_ns()
        summary["track_count"] = len(tracks)
        try:
            anchor = clock_anchor()
            offset = (
                anchor["realtime_ns"]
                - (anchor["monotonic_before_ns"] + anchor["monotonic_after_ns"]) // 2
            )
            if last_offset is not None and abs(offset - last_offset) > 5_000_000:
                summary["clock_step_detected"] = True
            artifact.write(anchor)
            summary["final_anchor_written"] = True
        except (OSError, ObserverError) as exc:
            summary["final_anchor_failure"] = (
                str(exc) if isinstance(exc, ObserverError) else "artifact_io_error"
            )
            if summary["stop_reason"] == "duration":
                summary["stop_reason"] = "artifact_write_failed"
        summary["complete_supported_observation"] = (
            summary["stop_reason"] == "duration"
            and summary["packets"] > 0
            and summary["statistics_available"]
            and summary["final_anchor_written"]
            and summary["kernel_drops"] == 0
            and summary["kernel_packets_minus_read"] == 0
            and not any(
                summary[k]
                for k in (
                    "malformed_headers",
                    "ancillary_failures",
                    "unexpected_truncations",
                    "source_identity_ambiguous",
                    "clock_step_detected",
                )
            )
        )
        summary["headers_bytes"] = artifact.bytes
        summary["headers_sha256"] = artifact.digest.hexdigest()
        artifact.close(summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--local-address", required=True)
    parser.add_argument("--local-port", type=int, required=True)
    parser.add_argument("--remote-address", required=True)
    parser.add_argument("--remote-port", type=int, required=True)
    parser.add_argument("--payload-type", type=int, action="append", required=True)
    parser.add_argument(
        "--transport", required=True, choices=("direct-srtp", "turn-udp", "turn-tcp", "tcp")
    )
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--packet-limit", type=int, default=MAX_PACKETS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true", help="validate only; no socket/files")
    args = parser.parse_args(argv)
    try:
        flow = Flow(
            args.interface,
            args.local_address,
            args.local_port,
            args.remote_address,
            args.remote_port,
            tuple(args.payload_type),
            args.transport,
        )
        if not 1 <= args.seconds <= MAX_SECONDS or not 1 <= args.packet_limit <= MAX_PACKETS:
            raise ObserverError("invalid_limits")
        if args.check:
            print(
                json.dumps(
                    {
                        "valid": True,
                        "capture_started": False,
                        "requires": "Linux Ethernet and CAP_NET_RAW",
                        "transport": flow.transport,
                        "filter_instructions": len(packet_filter(flow)),
                    }
                )
            )
            return 0
        if args.output is None:
            raise ObserverError("new_private_output_directory_required")
        result = observe(flow, args.output, seconds=args.seconds, packets=args.packet_limit)
        print(json.dumps(result))
        return 0 if result["complete_supported_observation"] else 2
    except (ObserverError, OSError) as exc:
        code = str(exc) if isinstance(exc, ObserverError) else "output_unavailable_no_overwrite"
        print(json.dumps({"error": code, "capture_complete": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
