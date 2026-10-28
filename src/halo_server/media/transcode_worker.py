"""Isolated fixed-protocol PyAV video worker; invoked by absolute path with -I.

No Ring credentials, URLs, shell arguments or user encoder options enter this
process. The parent owns per-request wall deadlines and termination/reaping.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import resource
import struct
import sys
from fractions import Fraction

import av

MAX_AU = 8 * 1024 * 1024
MAX_OUTPUT = 2 * 1024 * 1024
MAX_PIXELS = 8 * 1024 * 1024
HEADER = struct.Struct("!cQBI")


def read_exact(count):
    chunks = bytearray()
    while len(chunks) < count:
        part = sys.stdin.buffer.read(count - len(chunks))
        if not part:
            raise EOFError
        chunks.extend(part)
    return bytes(chunks)


def write(body):
    if not 0 < len(body) <= MAX_OUTPUT + 64:
        raise ValueError
    sys.stdout.buffer.write(struct.pack("!I", len(body)) + body)
    sys.stdout.buffer.flush()


def letterbox(frame, width, height):
    if frame.width * frame.height > MAX_PIXELS:
        raise ValueError
    scale = min(width / frame.width, height / frame.height)
    scaled_w = max(2, min(width, int(frame.width * scale) // 2 * 2))
    scaled_h = max(2, min(height, int(frame.height * scale) // 2 * 2))
    scaled = frame.reformat(width=scaled_w, height=scaled_h, format="yuv420p", threads=1)
    if (scaled_w, scaled_h) == (width, height):
        return scaled
    output = av.VideoFrame(width, height, "yuv420p")
    left, top = (width - scaled_w) // 4 * 2, (height - scaled_h) // 4 * 2
    for index, (source, destination) in enumerate(zip(scaled.planes, output.planes, strict=True)):
        divisor = 1 if index == 0 else 2
        rows, columns = scaled_h // divisor, scaled_w // divisor
        target = bytearray([16 if index == 0 else 128]) * destination.buffer_size
        data = bytes(source)
        for row in range(rows):
            offset = (top // divisor + row) * destination.line_size + left // divisor
            target[offset : offset + columns] = data[
                row * source.line_size : row * source.line_size + columns
            ]
        destination.update(target)
    return output


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    size = struct.unpack("!I", read_exact(4))[0]
    if not 0 < size <= 8192:
        raise ValueError
    config = json.loads(read_exact(size))
    width, height, fps = config["width"], config["height"], config["fps"]
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height = width, height
    encoder.pix_fmt, encoder.time_base, encoder.framerate = (
        "yuv420p",
        Fraction(1, fps),
        Fraction(fps),
    )
    encoder.bit_rate, encoder.thread_count = config["max_bit_rate"] * 1000, 1
    encoder.gop_size, encoder.max_b_frames = fps * 2, 0
    encoder.options = {
        "preset": "veryfast",
        "tune": "zerolatency",
        "profile": config["profile"],
        "level": config["level"],
        "x264-params": f"bframes=0:scenecut=0:repeat-headers=1:keyint={fps * 2}:"
        f"vbv-maxrate={config['max_bit_rate']}:vbv-bufsize={config['max_bit_rate']}",
    }
    encoder.open()
    decoder = av.CodecContext.create("h264", "r")
    decoder.thread_count = 1
    counter, last_source = 0, None
    write(b"S")
    while True:
        try:
            kind, timestamp, force, count = HEADER.unpack(read_exact(HEADER.size))
        except EOFError:
            return
        if kind not in {b"F", b"P"} or force not in {0, 1} or not 0 <= count <= MAX_AU:
            raise ValueError
        if kind == b"P":
            if count:
                raise ValueError
            frame = av.VideoFrame(width, height, "yuv420p")
            for index, plane in enumerate(frame.planes):
                plane.update(bytes([16 if index == 0 else 128]) * plane.buffer_size)
            frame.pts, frame.time_base = timestamp, Fraction(1, 90000)
            frames = [frame]
            force = 1
        else:
            if not count:
                raise ValueError
            packet = av.Packet(read_exact(count))
            packet.pts, packet.dts, packet.time_base = timestamp, None, Fraction(1, 90000)
            frames = decoder.decode(packet)
            if len(frames) > 4:
                raise ValueError
        outputs = []
        total = 0
        for frame in frames:
            if frame.width * frame.height > MAX_PIXELS:
                raise ValueError
            source = (
                timestamp
                if frame.pts is None
                else int(frame.pts * (frame.time_base or Fraction(1, 90000)) * 90000)
            )
            if last_source is not None and source - last_source < math.ceil(90000 / fps):
                continue
            output = letterbox(frame, width, height)
            output.pts, output.time_base = counter, Fraction(1, fps)
            if force:
                output.pict_type = av.video.frame.PictureType.I
            packets = encoder.encode(output)
            encoded = b"".join(bytes(packet) for packet in packets)
            if not encoded or len(encoded) > MAX_OUTPUT:
                raise ValueError
            total += len(encoded)
            if total > MAX_OUTPUT:
                raise ValueError
            if kind == b"P":
                independent = av.CodecContext.create("h264", "r")
                independent.thread_count = 1
                decoded = independent.decode(av.Packet(encoded))
                if len(decoded) != 1 or (decoded[0].width, decoded[0].height) != (width, height):
                    raise ValueError
            outputs.append(struct.pack("!QI", source, len(encoded)) + encoded)
            counter += 1
            last_source = source
        write(b"V" + bytes([len(outputs)]) + b"".join(outputs))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        with contextlib.suppress(Exception):
            write(b"Eworker-failed")
        raise SystemExit(2) from None
