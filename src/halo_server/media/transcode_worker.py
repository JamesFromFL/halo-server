"""Isolated fixed-protocol PyAV video worker; invoked by absolute path with -I.

No Ring credentials, URLs, shell arguments or user encoder options enter this
process. The parent owns per-request wall deadlines and termination/reaping.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import resource
import struct
import sys
from collections import OrderedDict, deque
from fractions import Fraction
from pathlib import Path

if __name__ == "__main__":
    _limits_spec = importlib.util.spec_from_file_location(
        "_halo_worker_limits", Path(__file__).with_name("worker_limits.py")
    )
    _limits = importlib.util.module_from_spec(_limits_spec)
    _limits_spec.loader.exec_module(_limits)
    _limits.apply_child_cpu_limit()

import av

_cadence_spec = importlib.util.spec_from_file_location(
    "_halo_source_cadence", Path(__file__).with_name("cadence.py")
)
_cadence = importlib.util.module_from_spec(_cadence_spec)
_cadence_spec.loader.exec_module(_cadence)

MAX_AU = 8 * 1024 * 1024
MAX_OUTPUT = 2 * 1024 * 1024
MAX_PIXELS = 8 * 1024 * 1024
HEADER = struct.Struct("!cQQI")
MAX_PENDING_FRAMES = 8
MAX_INPUT_ORIGINS = 64
MAX_FORCE_REQUESTS = 8
RATE_SAMPLES = 32
RATE_INTERVALS = 8
RATE_SPAN = 45000
RATE_GAP = 90000
RATE_COOLDOWN = 5 * 90000


def read_exact(count):
    chunks = bytearray()
    while len(chunks) < count:
        part = sys.stdin.buffer.read(count - len(chunks))
        if not part:
            raise EOFError
        chunks.extend(part)
    return bytes(chunks)


def write(body):
    if not 0 < len(body) <= MAX_OUTPUT + 128:
        raise ValueError
    sys.stdout.buffer.write(struct.pack("!I", len(body)) + body)
    sys.stdout.buffer.flush()


def letterbox(frame, width, height, *, source_sar=None):
    if frame.width * frame.height > MAX_PIXELS:
        raise ValueError
    ratio = Fraction(1) if source_sar is None else Fraction(source_sar)
    if not Fraction(1, 16) <= ratio <= 16:
        raise ValueError
    display_width = frame.width * ratio
    scale = min(Fraction(width) / display_width, Fraction(height, frame.height))
    scaled_w = max(2, min(width, int(display_width * scale) // 2 * 2))
    scaled_h = max(2, min(height, int(frame.height * scale) // 2 * 2))
    scaled = frame.reformat(
        width=scaled_w,
        height=scaled_h,
        format="yuv420p",
        threads=1,
        src_color_range=frame.color_range or None,
        dst_color_range=frame.color_range or None,
    )
    for name in ("colorspace", "color_primaries", "color_trc", "color_range"):
        setattr(scaled, name, getattr(frame, name))
    if (scaled_w, scaled_h) == (width, height):
        return scaled
    output = av.VideoFrame(width, height, "yuv420p")
    left, top = (width - scaled_w) // 4 * 2, (height - scaled_h) // 4 * 2
    for index, (source, destination) in enumerate(zip(scaled.planes, output.planes, strict=True)):
        divisor = 1 if index == 0 else 2
        rows, columns = scaled_h // divisor, scaled_w // divisor
        black = 0 if frame.color_range == 2 else 16
        target = bytearray([black if index == 0 else 128]) * destination.buffer_size
        data = bytes(source)
        for row in range(rows):
            offset = (top // divisor + row) * destination.line_size + left // divisor
            target[offset : offset + columns] = data[
                row * source.line_size : row * source.line_size + columns
            ]
        destination.update(target)
    for name in ("colorspace", "color_primaries", "color_trc", "color_range"):
        setattr(output, name, getattr(frame, name))
    return output


def open_encoder(config, *, source=None, nominal_fps=None):
    """Encode immediately; source PTS and the selected admission ceiling stay intact."""
    width, height, fps = config["width"], config["height"], config["fps"]
    nominal_fps = fps if nominal_fps is None else nominal_fps
    if type(nominal_fps) is not int or not 1 <= nominal_fps <= fps:
        raise ValueError
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height = width, height
    encoder.pix_fmt, encoder.time_base, encoder.framerate = (
        "yuv420p",
        Fraction(1, 90000),
        Fraction(nominal_fps),
    )
    # The negotiated value is a codec one-second maximum, not a protected-wire
    # serialization rate. Native moving-scene probes retain a measured codec
    # reserve; full-target/larger-VBV probes exceeded startup or covered windows. This is
    # an empirical target, not a universal bound; the separate monitor observes
    # actual encoded windows and transport owns its finite byte/burst budget.
    payload_rate = max(1, config["max_bit_rate"] * 82 // 100)
    vbv_size = max(1, (payload_rate + 1) // 2)
    encoder.bit_rate, encoder.thread_count = payload_rate * 1000, 1
    encoder.gop_size, encoder.max_b_frames = fps * 2, 0
    encoder.sample_aspect_ratio = Fraction(1)
    if source is not None:
        for name in ("colorspace", "color_primaries", "color_trc", "color_range"):
            setattr(encoder, name, getattr(source, name))
    encoder.options = {
        "preset": "veryfast",
        "tune": "zerolatency",
        "profile": config["profile"],
        "level": config["level"],
        "forced-idr": "1",
        "x264-params": f"force-cfr=1:bframes=0:scenecut=0:repeat-headers=1:keyint={fps * 2}:"
        f"vbv-maxrate={payload_rate}:vbv-bufsize={vbv_size}",
    }
    encoder.open()
    if encoder.options:
        raise ValueError
    return encoder


class RateCadence:
    """Bounded media-clock estimate, never based on SPS or arrival batches."""

    def __init__(self, maximum):
        self.maximum = self.target = maximum
        self.samples = deque(maxlen=RATE_SAMPLES)
        self.calibrated = False
        self.changed_at = None
        self.vote = None
        self.votes = 0
        self.vote_since = None

    def observe(self, source):
        if self.samples and source <= self.samples[-1]:
            raise ValueError
        if self.samples and source - self.samples[-1] > RATE_GAP:
            self.samples.clear()
            self.vote = self.vote_since = None
            self.votes = 0
        self.samples.append(source)
        intervals = len(self.samples) - 1
        span = source - self.samples[0]
        if intervals < RATE_INTERVALS or span < RATE_SPAN:
            return self.target
        estimated = min(self.maximum, max(1, (intervals * 90000 + span // 2) // span))
        if not self.calibrated:
            self.calibrated = True
            self.target = estimated
            self.changed_at = source
            return self.target
        if abs(estimated - self.target) * 5 < self.target:
            self.vote = self.vote_since = None
            self.votes = 0
            return self.target
        if self.vote != estimated:
            self.vote, self.votes, self.vote_since = estimated, 1, source
        else:
            self.votes += 1
        if (
            self.votes >= RATE_INTERVALS
            and source - self.vote_since >= RATE_SPAN
            and source - self.changed_at >= RATE_COOLDOWN
        ):
            self.target = estimated
            self.changed_at = source
            self.vote = self.vote_since = None
            self.votes = 0
        return self.target


class Encoder:
    """Finite decoder/encoder provenance, including delayed native output."""

    def __init__(self, config):
        self.config = config
        self.decoder = av.CodecContext.create("h264", "r")
        self.decoder.thread_count = 1
        self.encoder = None
        self.cadence = _cadence.SourceCadence(config["fps"])
        self.rate = RateCadence(config["fps"])
        self.nominal_fps = config["fps"]
        self.refreshes = 0
        self.last_idr_source = None
        self.inputs = OrderedDict()
        self.pending = {}
        self.forces = deque()
        self.last_force = 0
        self.terminal = False

    def feed(self, payload, timestamp, force):
        if self.terminal or timestamp in self.inputs or timestamp in self.pending:
            raise ValueError
        if force:
            if force <= self.last_force or len(self.forces) >= MAX_FORCE_REQUESTS:
                raise ValueError
            self.last_force = force
            self.forces.append(force)
        if len(self.inputs) + len(self.pending) >= MAX_INPUT_ORIGINS:
            raise ValueError
        self.inputs[timestamp] = None
        packet = av.Packet(payload)
        packet.pts, packet.dts, packet.time_base = timestamp, None, Fraction(1, 90000)
        frames = self.decoder.decode(packet)
        if len(frames) > 4:
            raise ValueError
        packets = []
        for frame in frames:
            if frame.is_corrupt or frame.pts is None or frame.width * frame.height > MAX_PIXELS:
                raise ValueError
            source = Fraction(frame.pts) * (frame.time_base or Fraction(1, 90000)) * 90000
            if source.denominator != 1 or int(source) not in self.inputs:
                raise ValueError
            source = int(source)
            del self.inputs[source]
            if not self.cadence.accept(source):
                continue
            output = letterbox(
                frame,
                self.config["width"],
                self.config["height"],
                source_sar=self.decoder.sample_aspect_ratio,
            )
            packets.extend(self.encode(output, source))
        return self.resolve(packets)

    def encode(self, output, source):
        desired = self.rate.observe(source)
        if self.encoder is None:
            self.encoder = open_encoder(self.config, source=output, nominal_fps=self.nominal_fps)
        elif any(
            getattr(self.encoder, name) != getattr(output, name)
            for name in ("colorspace", "color_primaries", "color_trc", "color_range")
        ):
            raise ValueError
        if desired != self.nominal_fps and not self.pending:
            # Earlier pictures in this decoder batch can have returned packets
            # still awaiting resolve(). Never erase their origin/force metadata.
            replacement = open_encoder(self.config, source=output, nominal_fps=desired)
            previous = self.encoder
            self.encoder, self.nominal_fps = replacement, desired
            self.last_idr_source = None
            self.refreshes += 1
            del previous
        token = self.forces.popleft() if self.forces else 0
        needs_idr = (
            token
            or self.last_idr_source is None
            or source - self.last_idr_source >= _cadence.IDR_INTERVAL
        )
        output.pts, output.time_base = source, Fraction(1, 90000)
        output.pict_type = (
            av.video.frame.PictureType.I if needs_idr else av.video.frame.PictureType.NONE
        )
        if needs_idr:
            self.last_idr_source = source
        if source in self.pending or len(self.pending) >= MAX_PENDING_FRAMES:
            raise ValueError
        self.pending[source] = token
        return self.encoder.encode(output)

    def resolve(self, packets):
        if len(packets) > 4:
            raise ValueError
        groups = OrderedDict()
        total = 0
        for packet in packets:
            if packet.pts is None or packet.dts != packet.pts or packet.time_base is None:
                raise ValueError
            stamp = Fraction(packet.pts) * packet.time_base * 90000
            if stamp.denominator != 1 or int(stamp) not in self.pending:
                raise ValueError
            payload = bytes(packet)
            total += len(payload)
            if not payload or total > MAX_OUTPUT:
                raise ValueError
            groups.setdefault(int(stamp), []).append(payload)
        result = tuple(
            (stamp, self.pending[stamp], b"".join(parts)) for stamp, parts in groups.items()
        )
        for stamp in groups:
            del self.pending[stamp]
        return result

    def probe(self):
        if self.terminal or self.inputs or self.pending or self.encoder is not None:
            raise ValueError
        self.terminal = True
        frame = av.VideoFrame(self.config["width"], self.config["height"], "yuv420p")
        for index, plane in enumerate(frame.planes):
            plane.update(bytes([16 if index == 0 else 128]) * plane.buffer_size)
        self.forces.append(1)
        packets = list(self.encode(frame, 0))
        packets.extend(self.encoder.encode(None))
        result = self.resolve(packets)
        if len(result) != 1 or self.pending or self.forces:
            raise ValueError
        independent = av.CodecContext.create("h264", "r")
        independent.thread_count = 1
        decoded = independent.decode(av.Packet(result[0][2]))
        if len(decoded) != 1 or (decoded[0].width, decoded[0].height) != (
            self.config["width"],
            self.config["height"],
        ):
            raise ValueError
        return result


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    size = struct.unpack("!I", read_exact(4))[0]
    if not 0 < size <= 8192:
        raise ValueError
    config = json.loads(read_exact(size))
    worker = Encoder(config)
    write(b"S")
    while True:
        try:
            kind, timestamp, force, count = HEADER.unpack(read_exact(HEADER.size))
        except EOFError:
            return
        if (
            kind not in {b"F", b"P"}
            or not 0 <= timestamp < 2**53
            or not 0 <= force < 2**53
            or not 0 <= count <= MAX_AU
        ):
            raise ValueError
        if kind == b"P":
            if count or timestamp or force:
                raise ValueError
            outputs = worker.probe()
        else:
            if not count:
                raise ValueError
            outputs = worker.feed(read_exact(count), timestamp, force)
        body = b"V" + bytes((len(outputs), len(worker.pending)))
        body += b"".join(
            struct.pack("!QQI", source, token, len(payload)) + payload
            for source, token, payload in outputs
        )
        write(body)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        with contextlib.suppress(Exception):
            write(b"Eworker-failed")
        raise SystemExit(2) from None
