#!/usr/bin/env python3
"""Bounded, account-free RTP receiver comparison. See README.md for scope."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.metadata
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PINS = json.loads((HERE / "profiles.json").read_text())
CASES = ("regular", "bursty", "loss", "reorder", "large_idr", "delayed_boundary", "wrap", "cancel")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def checked_git(path, commit):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()

    actual = git("rev-parse", "HEAD")
    if actual != commit or git("diff", "HEAD", "--"):
        raise ValueError(f"reference must be clean at {commit}: {path}")
    return actual


def checked_hash(path, expected):
    actual = digest(Path(path).read_bytes())
    if actual != expected:
        raise ValueError(f"reference hash mismatch: {path}")
    return actual


def reference_identity(kind, args):
    pin = PINS[kind]
    if kind == "homebridge":
        root = Path(args.homebridge).resolve()
        checked_git(root, pin["commit"])
        checked_hash(root / "package-lock.json", pin["lock_sha256"])
        peer = root / "packages/ring-client-api/streaming/peer-connection.ts"
        checked_hash(peer, pin["peer_sha256"])
        package = json.loads((root / "node_modules/werift/package.json").read_text())
        if package["version"] != pin["werift_version"]:
            raise ValueError("released Ring requires Werift 0.22.4")
        paths = {"root": str(root), "peer": str(peer)}
    else:
        root, fork = Path(args.scrypted).resolve(), Path(args.werift).resolve()
        checked_git(root, pin["commit"])
        checked_git(fork, pin["werift_commit"])
        checked_hash(fork / "package-lock.json", pin["lock_sha256"])
        peer = root / "plugins/webrtc/src/wrtc-to-rtsp.ts"
        codecs = root / "plugins/webrtc/src/webrtc-required-codecs.ts"
        checked_hash(peer, pin["peer_sha256"])
        checked_hash(codecs, pin["codecs_sha256"])
        checked_hash(fork / "packages/webrtc/src/media/rtpReceiver.ts", pin["receiver_sha256"])
        paths = {"root": str(root), "fork": str(fork), "peer": str(peer), "codecs": str(codecs)}
    # Installed dependency bytes are separately fingerprinted. The upstream lockfile
    # records every resolved version/integrity; this catches changes between runs.
    dependency_root = Path(paths.get("fork", paths["root"])) / "node_modules"
    files = sorted(p for p in dependency_root.rglob("*") if p.is_file() and not p.is_symlink())
    tree = hashlib.sha256()
    for file in files:
        tree.update(str(file.relative_to(dependency_root)).encode() + b"\0")
        tree.update(hashlib.sha256(file.read_bytes()).digest())
    if not files:
        raise ValueError("reference dependencies unavailable; run npm ci --ignore-scripts")
    return {
        "pins": pin,
        "paths": paths,
        "dependency_tree_sha256": tree.hexdigest(),
        "dependency_files": len(files),
    }


def annexb_nalus(data):
    import re

    return [n for n in re.split(b"\x00\x00\x00?\x01", data) if n]


def source_frames(large=False):
    """Generate deterministic, independently decoded libx264 pictures in memory."""
    from fractions import Fraction

    import av
    import numpy as np

    width, height = (640, 360) if large else (160, 96)
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height, encoder.pix_fmt = width, height, "yuv420p"
    encoder.time_base, encoder.framerate = Fraction(1, 30), Fraction(30, 1)
    encoder.options = {
        "preset": "ultrafast",
        "tune": "zerolatency",
        "crf": "18",
        "x264-params": "keyint=6:min-keyint=6:scenecut=0:repeat-headers=1",
    }
    noise = np.random.default_rng(715).integers(0, 256, (height, width, 3), dtype=np.uint8)
    frames = []
    for index in range(12):
        image = noise.copy()
        image[:8, :8] = index * 16
        frame = av.VideoFrame.from_ndarray(image, format="rgb24")
        frame.pts = index
        frames.extend(bytes(packet) for packet in encoder.encode(frame))
    frames.extend(bytes(packet) for packet in encoder.encode(None))
    decoder = av.CodecContext.create("h264", "r")
    decoded = sum(len(decoder.decode(av.Packet(frame))) for frame in frames)
    decoded += len(decoder.decode(None))
    if decoded != 12 or len(frames) != 12:
        raise ValueError(f"synthetic encoder control failed: {len(frames)} AUs/{decoded} pictures")
    return frames


def make_trace(case, frames):
    sequence, base = (65500, 0xFFFFF000) if case == "wrap" else (1000, 90000)
    packets, aus = [], []
    for index, frame in enumerate(frames):
        timestamp = (base + index * 3000) & 0xFFFFFFFF
        payloads = []
        for nal in annexb_nalus(frame):
            if len(nal) <= 1000:
                payloads.append(nal)
            else:
                chunks = [nal[n : n + 998] for n in range(1, len(nal), 998)]
                for n, chunk in enumerate(chunks):
                    header = (
                        (nal[0] & 31) | (128 if n == 0 else 0) | (64 if n == len(chunks) - 1 else 0)
                    )
                    payloads.append(bytes([(nal[0] & 0xE0) | 28, header]) + chunk)
        au_packets = []
        for n, payload in enumerate(payloads):
            due = index * 33.333 + n * 0.015
            if case == "bursty":
                due = (index // 4) * 133.333 + (index % 4) * 0.3 + n * 0.001
            if case == "delayed_boundary" and index > 0:
                due += 180
            header = struct.pack(
                "!BBHII",
                0x80,
                96 | (128 if n == len(payloads) - 1 else 0),
                sequence,
                timestamp,
                0x12345678,
            )
            au_packets.append(
                {
                    "id": len(packets) + n,
                    "au": index,
                    "sequence": sequence,
                    "timestamp": timestamp,
                    "due_ms": round(due, 4),
                    "wire": base64.b64encode(header + payload).decode(),
                }
            )
            sequence = (sequence + 1) & 65535
        aus.append(
            {
                "index": index,
                "timestamp": timestamp,
                "packets": len(payloads),
                "sequences": [p["sequence"] for p in au_packets],
                "annexb_sha256": digest(b"".join(b"\0\0\0\1" + n for n in annexb_nalus(frame))),
                "keyframe": any((n[0] & 31) == 5 for n in annexb_nalus(frame)),
            }
        )
        packets.extend(au_packets)
    omitted = []
    if case == "loss":
        target = next(p for p in packets if p["au"] == 0 and p["sequence"] > 1003)
        packets.remove(target)
        omitted.append(target["id"])
    if case == "reorder":
        packets[3], packets[4] = packets[4], packets[3]
        packets[3]["due_ms"], packets[4]["due_ms"] = packets[4]["due_ms"], packets[3]["due_ms"]
    if case == "cancel":
        omitted = [p["id"] for p in packets if p["au"] >= 3]
        packets = [p for p in packets if p["au"] < 3]
    if case == "large_idr" and aus[0]["packets"] <= 128:
        raise ValueError("large IDR control did not exceed 128 RTP fragments")
    return {
        "case": case,
        "packets": packets,
        "aus": aus,
        "omitted_packet_ids": omitted,
        "source_decoded_pictures": 12,
        "source_sha256": digest(b"".join(frames)),
    }


def match_halo_access_units(result, trace):
    """Anchor source identity in content, then independently check aiortc's clock.

    TimestampMapper starts at the first *released* AU, which can follow loss.
    If that callback is damaged (the stock-buffer negative control), a later
    unique hash can still infer its clock origin without validating the damage.
    """
    by_hash = {}
    for au in trace["aus"]:
        by_hash.setdefault(au["annexb_sha256"], []).append(au)
    unique = {sha: aus[0] for sha, aus in by_hash.items() if len(aus) == 1}
    forwarded = result["forwarded"]
    anchor = next((row for row in forwarded if row["sha256"] in unique), None)
    origin = (
        (unique[anchor["sha256"]]["timestamp"] - anchor["mapped_timestamp"]) & 0xFFFFFFFF
        if anchor is not None
        else None
    )
    origin_au = next((au for au in trace["aus"] if au["timestamp"] == origin), None)
    clock_anchored = (
        origin_au is not None and bool(forwarded) and forwarded[0]["mapped_timestamp"] == 0
    )
    by_timestamp = (
        {(au["timestamp"] - origin) & 0xFFFFFFFF: au for au in trace["aus"]}
        if clock_anchored
        else {}
    )
    result["timestamp_origin_au"] = origin_au["index"] if clock_anchored else None
    result["timestamp_origin_source_rtp"] = origin if clock_anchored else None
    result["timestamp_origin_evidence_delivery"] = anchor["delivery_id"] if anchor else None
    result["assembled"] = []
    for row in forwarded:
        source = unique.get(row["sha256"])
        expected = by_timestamp.get(row["mapped_timestamp"])
        row["au"] = (source or expected or {"index": -1})["index"]
        row["payload_valid"] = source is not None or (
            expected is not None and expected["annexb_sha256"] == row["sha256"]
        )
        row["timestamp_valid"] = expected is not None and (source is None or source == expected)
        if row["payload_valid"] and row["timestamp_valid"]:
            result["assembled"].append(row)
    result["timestamp_mapping_valid"] = (
        all(row["timestamp_valid"] for row in forwarded) if clock_anchored else None
    )
    for boundary in ("egress", "sink"):
        for row in result[boundary]:
            row["au"] = forwarded[row["delivery_id"]]["au"]


def summarize(result, trace):
    arrivals = {row["id"]: row["ms"] for row in result["arrivals"]}
    forwards = result["forwarded"]
    actual_aus = {row["au"]: row for row in result["assembled"]}
    return {
        "input_packets": len(arrivals),
        "forwarded_units": len(forwards),
        "forwarded_unit": result["forwarded_unit"],
        "complete_aus": len(actual_aus),
        "not_complete_aus": [au["index"] for au in trace["aus"] if au["index"] not in actual_aus],
        "first_arrival_ms": min(arrivals.values(), default=None),
        "first_forward_ms": min((r["ms"] for r in forwards), default=None),
        "first_complete_au_ms": min((r["ms"] for r in actual_aus.values()), default=None),
        "first_udp_egress_ms": min((r["ms"] for r in result["egress"]), default=None),
        "first_udp_sink_ms": min((r["ms"] for r in result["sink"]), default=None),
        "udp_datagrams_sent": len(result["egress"]),
        "udp_datagrams_received": len(result["sink"]),
        "output_digest": result["output_digest"],
        "cleanup": result["cleanup"],
        "production_rejection_or_dependency_policy": "not executed by this receiver-only harness",
    }


async def halo_child(request):
    sys.path.insert(0, str(REPO / "src"))
    import aiortc
    from aiortc import RTCConfiguration, RTCPeerConnection
    from aiortc.jitterbuffer import JitterBuffer
    from aiortc.rtcrtpparameters import RTCRtpCodecParameters
    from aiortc.rtp import RtpPacket

    from halo_server.media.ring_media import MicrophoneTrack, install_encoded_frame_adapter

    trace = request["trace"]
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    video = peer.addTransceiver("video", direction="recvonly")
    mic = MicrophoneTrack()
    peer.addTransceiver(mic, direction="sendrecv")
    offer = await peer.createOffer()
    receiver = video.receiver
    result = {
        "arrivals": [],
        "forwarded": [],
        "assembled": [],
        "egress": [],
        "sink": [],
        "forwarded_unit": "encoded_access_unit",
        "offer": offer.sdp,
        "versions": {
            name: importlib.metadata.version(name) for name in ("aiortc", "av", "ring-doorbell")
        },
        "code": {
            "halo_ring_media": digest((REPO / "src/halo_server/media/ring_media.py").read_bytes()),
            "aiortc_receiver": digest(
                Path(sys.modules["aiortc.rtcrtpreceiver"].__file__).read_bytes()
            ),
        },
    }
    assembly_source = REPO / "src/halo_server/media/video_jitter.py"
    if assembly_source.is_file():
        result["code"]["halo_video_jitter"] = digest(assembly_source.read_bytes())
    if aiortc.__version__ != "1.15.0":
        raise ValueError("only pinned aiortc 1.15.0 is supported")
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    sink.setblocking(False)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.setblocking(False)
    start, output_hash = time.monotonic(), hashlib.sha256()

    def ms():
        return (time.monotonic() - start) * 1000 if request.get("instrument", True) else 0

    def encoded(data, timestamp):
        delivery_id = len(result["forwarded"])
        result["forwarded"].append(
            {
                "delivery_id": delivery_id,
                "mapped_timestamp": timestamp,
                "ms": ms(),
                "bytes": len(data),
                "sha256": digest(data),
            }
        )
        output_hash.update(data)
        # Diagnostic sink carries compact metadata, never household media. An AU can
        # exceed a UDP datagram, so this does not pretend to be HomeKit packetization.
        sender.sendto(struct.pack("!i", delivery_id), sink.getsockname())
        result["egress"].append({"delivery_id": delivery_id, "ms": ms()})

    install_encoded_frame_adapter(receiver, "video", encoded, forward_to_decoder=False)
    receiver._RTCRtpReceiver__decoder_thread = True  # enable queue boundary without decoder thread
    receiver._RTCRtpReceiver__codecs[96] = RTCRtpCodecParameters(
        mimeType="video/H264",
        clockRate=90000,
        payloadType=96,
        parameters={"packetization-mode": "1", "profile-level-id": "42e01f"},
    )
    if request.get("stock_buffer"):
        receiver._RTCRtpReceiver__jitter_buffer = JitterBuffer(capacity=128, is_video=True)

    async def feedback(*_args):
        result.setdefault("rtcp_feedback_ms", []).append(ms())

    receiver._send_rtcp = feedback
    reader_running = True

    async def read_sink():
        while reader_running:
            data = await asyncio.get_running_loop().sock_recv(sink, 65535)
            result["sink"].append({"delivery_id": struct.unpack("!i", data)[0], "ms": ms()})

    reader = asyncio.create_task(read_sink())
    try:
        for item in trace["packets"]:
            await asyncio.sleep(max(0, start + item["due_ms"] / 1000 - time.monotonic()))
            result["arrivals"].append({"id": item["id"], "ms": ms()})
            await receiver._handle_rtp_packet(
                RtpPacket.parse(base64.b64decode(item["wire"])),
                int((time.monotonic() - start) * 1000),
            )
        await asyncio.sleep(0.035)
    finally:
        reader_running = False
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        receiver._RTCRtpReceiver__decoder_thread = None
        mic.stop()
        await peer.close()
        sink.close()
        sender.close()
    match_halo_access_units(result, trace)
    result["output_digest"] = output_hash.hexdigest()
    assembly_snapshot = getattr(receiver._RTCRtpReceiver__jitter_buffer, "snapshot", None)
    result["video_assembly"] = assembly_snapshot() if callable(assembly_snapshot) else None
    result["cleanup"] = {
        "peer_closed": peer.connectionState == "closed",
        "sockets_closed": True,
        "pending_tasks": len(
            [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        ),
    }
    return result


def run_child(command, request, timeout):
    with tempfile.TemporaryDirectory(prefix="halo-receiver-") as tmp:
        input_path, output_path = Path(tmp) / "input.json", Path(tmp) / "output.json"
        input_path.write_text(json.dumps(request))
        process = subprocess.Popen(
            [*command, str(input_path), str(output_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise RuntimeError(f"receiver process exceeded {timeout}s and was killed") from None
        if process.returncode or not output_path.is_file():
            raise RuntimeError(
                f"receiver failed ({process.returncode}): {(stderr or stdout)[-4000:]}"
            )
        return json.loads(output_path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--homebridge", help="clean Ring release checkout with npm ci dependencies")
    parser.add_argument("--scrypted", help="clean Scrypted source checkout")
    parser.add_argument(
        "--werift", help="clean Scrypted Werift fork checkout with npm ci dependencies"
    )
    parser.add_argument("--node", default="node")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument(
        "--allow-unavailable", action="store_true", help="write partial report and exit 2"
    )
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 60:
        parser.error("timeout must be 1..60 seconds")
    report = {
        "schema": 1,
        "harness_sha256": {
            name: digest((HERE / name).read_bytes())
            for name in ("run.py", "node_receiver.mjs", "profiles.json")
        },
        "scope": (
            "synthetic post-SRTP video receiver replay; no Ring signaling, ICE connectivity, "
            "audio conversion, HomeKit or physical playback"
        ),
        "references": {},
        "cases": {},
        "controls": {},
    }
    for kind in ("homebridge", "scrypted"):
        try:
            report["references"][kind] = {"status": "available", **reference_identity(kind, args)}
        except (OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
            report["references"][kind] = {"status": "unavailable", "reason": str(exc)}
    unavailable = any(r["status"] == "unavailable" for r in report["references"].values())
    if unavailable and not args.allow_unavailable:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        return 2
    normal, large = source_frames(), source_frames(True)
    fork = Path(args.werift).resolve() if args.werift else None
    loader = fork / "node_modules/tsx/dist/loader.mjs" if fork else None
    commands = {"halo": [sys.executable, str(HERE / "run.py"), "--child-halo"]}
    for kind, reference in report["references"].items():
        if reference["status"] == "available":
            commands[kind] = [args.node, "--import", str(loader), str(HERE / "node_receiver.mjs")]
    for case in args.cases:
        trace = make_trace(case, large if case == "large_idr" else normal)
        report["cases"][case] = {
            "trace_sha256": digest(json.dumps(trace, sort_keys=True).encode()),
            "trace": {k: v for k, v in trace.items() if k != "packets"},
            "receivers": {},
        }
        for kind, command in commands.items():
            request = {
                "kind": kind,
                "trace": trace,
                "reference": report["references"].get(kind),
                "instrument": True,
            }
            try:
                result = run_child(command, request, args.timeout)
            except (OSError, RuntimeError) as exc:
                report["cases"][case]["receivers"][kind] = {"status": "failed", "reason": str(exc)}
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, indent=2) + "\n")
                return 2
            result["summary"] = summarize(result, trace)
            report["cases"][case]["receivers"][kind] = result
            if case in ("regular", "large_idr"):
                control = run_child(command, {**request, "instrument": False}, args.timeout)
                equal = control["output_digest"] == result["output_digest"]
                report["controls"][f"{case}_{kind}_instrumentation_digest_equal"] = equal
                if not equal:
                    raise RuntimeError(f"instrumentation changed {kind} output")
            if case == "large_idr" and kind == "halo":
                control = run_child(command, {**request, "stock_buffer": True}, args.timeout)
                report["controls"]["halo_stock_128_buffer"] = summarize(control, trace)
        print(f"completed {case}", flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 2 if unavailable else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child-halo":
        request = json.loads(Path(sys.argv[2]).read_text())
        Path(sys.argv[3]).write_text(json.dumps(asyncio.run(halo_child(request))))
    else:
        raise SystemExit(main())
