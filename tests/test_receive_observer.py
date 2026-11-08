"""No privileged sockets: exercise the exact BPF program with a small cBPF VM."""

import hashlib
import json
import socket
import stat
import struct
from dataclasses import replace

import pytest

from halo_server.media import receive_observer as observer

FLOW = observer.Flow("eth0", "192.0.2.10", 40000, "198.51.100.20", 50000, (105, 111))


def ethernet(
    *,
    tags=0,
    ihl=5,
    seq=42,
    timestamp=123456,
    ssrc=789,
    payload_type=105,
    source="198.51.100.20",
    destination="192.0.2.10",
    source_port=50000,
    destination_port=40000,
    fragment=0,
    protocol=17,
    payload=b"PRIVATE_MEDIA_BYTES",
):
    rtp = struct.pack("!BBHII", 0x80, 0x80 | payload_type, seq, timestamp, ssrc) + payload
    udp = struct.pack("!HHHH", source_port, destination_port, len(rtp) + 8, 0) + rtp
    ip = (
        struct.pack(
            "!BBHHHBBH4s4s",
            0x40 | ihl,
            0,
            ihl * 4 + len(udp),
            1,
            fragment,
            64,
            protocol,
            0,
            socket.inet_aton(source),
            socket.inet_aton(destination),
        )
        + b"\0" * ((ihl - 5) * 4)
        + udp
    )
    link = b"\0" * 12
    for _ in range(tags):
        link += struct.pack("!HH", 0x8100, 123)
    return link + struct.pack("!H", 0x0800) + ip


def execute_bpf(code, data):
    """Independent subset interpreter, including Linux out-of-bounds rejection."""
    a = x = pc = 0
    while pc < len(code):
        op, yes, no, k = code[pc]
        pc += 1
        if op in (0x20, 0x28, 0x30, 0x48, 0x50):
            width = {0x20: 4, 0x28: 2, 0x30: 1, 0x48: 2, 0x50: 1}[op]
            offset = k + (x if op in (0x48, 0x50) else 0)
            if offset + width > len(data):
                return 0
            a = int.from_bytes(data[offset : offset + width], "big")
        elif op == 0x15:
            pc += yes if a == k else no
        elif op == 0x35:
            pc += yes if a >= k else no
        elif op == 0x45:
            pc += yes if a & k else no
        elif op == 0x54:
            a &= k
        elif op == 0x64:
            a <<= k
        elif op == 0x07:
            x = a
        elif op == 0x87:
            a = x
        elif op == 0x04:
            a += k
        elif op == 0x05:
            pc += k
        elif op == 0x06:
            return k
        elif op == 0x16:
            return a
        else:
            raise AssertionError(op)
    raise AssertionError("no terminal instruction")


@pytest.mark.parametrize("tags", [0, 1, 2])
@pytest.mark.parametrize("ihl", [5, 6, 15])
@pytest.mark.parametrize("payload_type", [105, 111])
def test_filter_exact_header_snapshot_with_options_and_vlan(tags, ihl, payload_type):
    wire = ethernet(tags=tags, ihl=ihl, payload_type=payload_type)
    size = execute_bpf(observer.packet_filter(FLOW), wire)
    assert size == 14 + 4 * tags + 4 * ihl + 8 + 12
    assert wire[size:] == b"PRIVATE_MEDIA_BYTES"
    ssrc, fields = observer.parse_header(wire[:size], FLOW)
    assert ssrc == 789
    assert fields["seq"] == 42 and fields["rtp_timestamp"] == 123456
    assert fields["vlan_depth"] == tags and fields["payload_type"] == payload_type


@pytest.mark.parametrize(
    "changes",
    [
        {"source": "198.51.100.21"},
        {"destination": "192.0.2.11"},
        {"source_port": 50001},
        {"destination_port": 40001},
        {"protocol": 6},
        {"fragment": 0x2000},
        {"fragment": 1},
        {"payload_type": 112},
        {"tags": 3},
    ],
)
def test_kernel_rejects_other_flows_types_fragments_and_extra_vlan(changes):
    assert execute_bpf(observer.packet_filter(FLOW), ethernet(**changes)) == 0


def test_filter_rejects_short_headers_rtcp_and_dtls():
    code = observer.packet_filter(FLOW)
    wire = ethernet()
    for size in range(44):
        assert execute_bpf(code, wire[:size]) == 0
    for kind in (200, 201):
        altered = bytearray(wire)
        altered[43] = kind  # RTCP type is not a permitted dynamic RTP type
        assert execute_bpf(code, altered) == 0
    altered = bytearray(wire)
    altered[42] = 22  # DTLS handshake content type
    assert execute_bpf(code, altered) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"transport": "turn-udp"},
        {"transport": "turn-tcp"},
        {"local_address": "::1"},
        {"remote_address": "camera.example"},
        {"remote_address": "224.0.0.1"},
        {"local_port": 0},
        {"payload_types": (70,)},
        {"payload_types": (105, 105)},
        {"interface": "../eth0"},
    ],
)
def test_configuration_rejects_unsupported_without_socket(changes):
    with pytest.raises(observer.ObserverError):
        replace(FLOW, **changes)


class FakeClock:
    now = 1_000_000_000

    def monotonic_ns(self):
        return self.now

    def time_ns(self):
        return self.now + 1_700_000_000_000_000_000


class FakeSocket:
    def __init__(self, clock, rows, *, drops=0, queued=0, flags=0, stats=True):
        self.clock, self.rows = clock, list(rows)
        self.drops, self.queued, self.flags, self.stats = drops, queued, flags, stats
        self.closed, self.read = False, 0

    def recvmsg(self, size, ancillary_size):
        assert size == observer.MAX_HEADER and ancillary_size >= 16
        self.clock.now += 60_000_000
        if not self.rows:
            raise TimeoutError
        self.read += 1
        wire = self.rows.pop(0)
        length = execute_bpf(observer.packet_filter(FLOW), wire)
        stamp = struct.pack("=qq", *divmod(self.clock.time_ns(), 10**9))
        ancillary = [(socket.SOL_SOCKET, observer.SO_TIMESTAMPNS_NEW, stamp)]
        return wire[:length], ancillary, self.flags, None

    def getsockopt(self, level, option, size=None):
        if option == socket.SO_RCVBUF:
            return 524288
        assert level == observer.SOL_PACKET and option == observer.PACKET_STATISTICS
        if not self.stats:
            raise OSError
        return struct.pack("=II", self.read + self.queued, self.drops)

    def close(self):
        self.closed = True


def run(tmp_path, monkeypatch, rows, **options):
    clock = FakeClock()
    monkeypatch.setattr(observer.time, "monotonic_ns", clock.monotonic_ns)
    monkeypatch.setattr(observer.time, "time_ns", clock.time_ns)
    sock = FakeSocket(clock, rows, **options)
    path = tmp_path / "capture"
    result = observer.observe(FLOW, path, seconds=1, opener=lambda _: sock)
    assert sock.closed
    return result, path


def test_capture_private_header_only_artifact_and_accounting(tmp_path, monkeypatch):
    result, path = run(tmp_path, monkeypatch, [ethernet(), ethernet(payload_type=111, ssrc=222)])
    assert result["complete_supported_observation"] and result["packets"] == 2
    assert result["track_count"] == 2 and result["kernel_drops"] == 0
    assert result["kernel_packets_minus_read"] == 0
    rows = [json.loads(line) for line in (path / "headers.jsonl").read_text().splitlines()]
    headers = [row for row in rows if row["kind"] == "rtp_header"]
    assert [row["track_id"] for row in headers] == [1, 2]
    content = b"".join(f.read_bytes() for f in path.iterdir())
    for private in (b"PRIVATE_MEDIA_BYTES", b"198.51.100.20", b"192.0.2.10", b'"ssrc"'):
        assert private not in content
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in path.iterdir())
    assert result["headers_bytes"] == (path / "headers.jsonl").stat().st_size
    expected_hash = hashlib.sha256((path / "headers.jsonl").read_bytes()).hexdigest()
    assert result["headers_sha256"] == expected_hash


@pytest.mark.parametrize(
    "options", [{"drops": 1}, {"queued": 1}, {"stats": False}, {"flags": socket.MSG_CTRUNC}]
)
def test_incomplete_evidence_is_reported(tmp_path, monkeypatch, options):
    result, _ = run(tmp_path, monkeypatch, [ethernet()], **options)
    assert not result["complete_supported_observation"]


def test_new_ssrc_same_type_is_explicitly_ambiguous(tmp_path, monkeypatch):
    result, _ = run(tmp_path, monkeypatch, [ethernet(), ethernet(ssrc=900)])
    assert result["source_identity_ambiguous"]
    assert not result["complete_supported_observation"]


def test_permission_failure_no_prompt_no_leak(tmp_path):
    def denied(_):
        raise PermissionError("private socket details")

    result = observer.observe(FLOW, tmp_path / "capture", opener=denied)
    assert result["stop_reason"] == "permission_denied_CAP_NET_RAW_required"
    assert not result["complete_supported_observation"]
    assert "private socket details" not in json.dumps(result)


def test_no_overwrite_symlink_or_existing_directory(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    with pytest.raises(FileExistsError):
        observer.Artifact(target)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(observer.ObserverError, match="symlink_output_parent"):
        observer.Artifact(link / "capture")


def test_untrusted_writable_parent_rejected_before_directory_creation(tmp_path):
    parent = tmp_path / "unsafe"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    with pytest.raises(observer.ObserverError, match="unsafe_output_parent"):
        observer.Artifact(parent / "capture")
    assert not (parent / "capture").exists()


def test_parent_descriptor_pins_creation_across_rename(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    moved = tmp_path / "moved"
    parent.mkdir(mode=0o700)
    original_mkdir = observer.os.mkdir

    def rename_before_create(name, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            parent.rename(moved)
            parent.symlink_to(tmp_path, target_is_directory=True)
        return original_mkdir(name, mode=mode, dir_fd=dir_fd)

    monkeypatch.setattr(observer.os, "mkdir", rename_before_create)
    artifact = observer.Artifact(parent / "capture")
    artifact.close({})
    assert (moved / "capture/summary.json").exists()
    assert not (tmp_path / "capture").exists()


def test_byte_limit_and_packet_limit_close_socket(tmp_path, monkeypatch):
    monkeypatch.setattr(observer, "MAX_BYTES", 40)
    result, _ = run(tmp_path, monkeypatch, [ethernet()])
    assert result["stop_reason"] == "artifact_byte_limit"
    assert result["final_anchor_failure"] == "artifact_byte_limit"
    assert not result["final_anchor_written"]
    assert not result["complete_supported_observation"]
    monkeypatch.setattr(observer, "MAX_BYTES", 10000)
    sock = FakeSocket(FakeClock(), [ethernet(), ethernet()])
    result = observer.observe(FLOW, tmp_path / "limited", packets=1, opener=lambda _: sock)
    assert sock.closed and result["stop_reason"] == "packet_limit" and result["packets"] == 1


def test_statistics_and_close_tail_are_outside_declared_coverage(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(observer.time, "monotonic_ns", clock.monotonic_ns)
    monkeypatch.setattr(observer.time, "time_ns", clock.time_ns)

    class BoundarySocket(FakeSocket):
        def getsockopt(self, level, option, size=None):
            result = super().getsockopt(level, option, size)
            if option == observer.PACKET_STATISTICS:
                clock.now += 1_000_000
            return result

        def close(self):
            clock.now += 2_000_000
            super().close()

    sock = BoundarySocket(clock, [ethernet()])
    result = observer.observe(FLOW, tmp_path / "boundary", seconds=1, opener=lambda _: sock)
    assert result["complete_supported_observation"]
    assert result["coverage_started_monotonic_ns"] <= result["coverage_ended_monotonic_ns"]
    assert result["coverage_ended_monotonic_ns"] <= result["statistics_before_monotonic_ns"]
    assert result["statistics_before_monotonic_ns"] < result["statistics_after_monotonic_ns"]
    assert result["statistics_after_monotonic_ns"] < result["stopped_monotonic_ns"]


def test_timestamp_strict_abi_and_missing_values():
    good = (socket.SOL_SOCKET, observer.SO_TIMESTAMPNS_NEW, struct.pack("=qq", 7, 8))
    assert observer.kernel_timestamp([good]) == 7_000_000_008
    for bad in (
        [],
        [good, good],
        [(socket.SOL_SOCKET, 35, good[2])],
        [(socket.SOL_SOCKET, observer.SO_TIMESTAMPNS_NEW, struct.pack("=qq", 1, 10**9))],
    ):
        with pytest.raises(observer.ObserverError):
            observer.kernel_timestamp(bad)


def test_cli_check_never_opens_socket_or_artifact(monkeypatch, capsys):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry check must be inert")

    monkeypatch.setattr(observer.socket, "socket", forbidden)
    monkeypatch.setattr(observer, "Artifact", forbidden)
    args = [
        "--interface",
        "eth0",
        "--local-address",
        "192.0.2.10",
        "--local-port",
        "40000",
        "--remote-address",
        "198.51.100.20",
        "--remote-port",
        "50000",
        "--payload-type",
        "105",
        "--transport",
        "direct-srtp",
        "--check",
    ]
    assert observer.main(args) == 0
    assert not json.loads(capsys.readouterr().out)["capture_started"]
    args[args.index("direct-srtp")] = "turn-udp"
    assert observer.main(args) == 2
    assert "unsupported_transport" in capsys.readouterr().err


def test_packet_socket_filter_locked_before_receive_enabled_and_closed_on_failure(monkeypatch):
    operations = []

    class SetupSocket:
        def setsockopt(self, level, option, value):
            operations.append(("option", level, option))

        def bind(self, address):
            operations.append(("bind", address))

        def getsockname(self):
            return ("eth0", 3, 0, 1, b"\0" * 6)

        def settimeout(self, seconds):
            assert seconds <= 0.2

        def close(self):
            operations.append(("close",))

    def socket_factory(family, kind, protocol):
        assert family == socket.AF_PACKET and kind == socket.SOCK_RAW and protocol == 0
        operations.append(("create_inert",))
        return SetupSocket()

    monkeypatch.setattr(observer.socket, "socket", socket_factory)
    sock = observer.open_observer(FLOW)
    assert operations.index(("option", socket.SOL_SOCKET, observer.SO_ATTACH_FILTER)) < (
        operations.index(("option", socket.SOL_SOCKET, observer.SO_LOCK_FILTER))
    )
    assert operations.index(("option", socket.SOL_SOCKET, observer.SO_LOCK_FILTER)) < (
        operations.index(("bind", ("eth0", 3)))
    )
    assert ("option", observer.SOL_PACKET, observer.PACKET_IGNORE_OUTGOING) in operations
    sock.close()
    monkeypatch.setattr(SetupSocket, "getsockname", lambda _: ("eth0", 3, 0, 772, b""))
    with pytest.raises(observer.ObserverError, match="unsupported_link_type"):
        observer.open_observer(FLOW)
    assert operations[-1] == ("close",)


def test_clock_step_is_flagged_and_final_anchor_retained(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(observer.time, "monotonic_ns", clock.monotonic_ns)
    monkeypatch.setattr(
        observer.time,
        "time_ns",
        lambda: clock.time_ns() + (20_000_000 if clock.now > 1_500_000_000 else 0),
    )
    sock = FakeSocket(clock, [ethernet()])
    result = observer.observe(FLOW, tmp_path / "step", seconds=1, opener=lambda _: sock)
    assert result["clock_step_detected"] and not result["complete_supported_observation"]
    rows = [json.loads(line) for line in (tmp_path / "step/headers.jsonl").read_text().splitlines()]
    assert rows[-1]["kind"] == "clock_anchor"


def test_interrupt_closes_only_observer_and_reports_partial(tmp_path):
    sock = FakeSocket(FakeClock(), [])

    def interrupted(*_):
        raise KeyboardInterrupt

    sock.recvmsg = interrupted
    result = observer.observe(FLOW, tmp_path / "interrupted", opener=lambda _: sock)
    assert sock.closed and result["stop_reason"] == "interrupted"
    assert not result["complete_supported_observation"]
