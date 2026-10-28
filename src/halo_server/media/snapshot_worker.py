"""One fixed JPEG conversion; private process, no URLs or account credentials."""

from __future__ import annotations

import contextlib
import importlib.util
import math
import os
import resource
import struct
import sys
from fractions import Fraction
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "_halo_jpeg_wire", Path(__file__).with_name("jpeg.py")
)
wire = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wire
spec.loader.exec_module(wire)


def read_exact(size):
    result = bytearray()
    while len(result) < size:
        data = sys.stdin.buffer.read(size - len(result))
        if not data:
            raise EOFError
        result.extend(data)
    return bytes(result)


def write(body):
    if not 0 < len(body) <= wire.MAX_JPEG_BYTES + 1:
        raise ValueError
    sys.stdout.buffer.write(struct.pack("!I", len(body)) + body)
    sys.stdout.buffer.flush()


def convert(data, configuration):
    header = wire.inspect_jpeg(data)
    import av

    decoder = av.CodecContext.create("mjpeg", "r")
    decoder.thread_count = 1
    decoder.options = {"max_pixels": str(wire.MAX_SOURCE_PIXELS), "err_detect": "explode"}
    decoder.open()
    if decoder.options:
        raise ValueError
    frames = decoder.decode(av.Packet(data)) + decoder.decode(None)
    if (
        len(frames) != 1
        or frames[0].is_corrupt
        or (frames[0].width, frames[0].height) != (header.width, header.height)
    ):
        raise wire.JpegError()
    frame = frames[0]
    width, height = configuration.width, configuration.height
    # The native admission above proves the entire entropy payload before the
    # original baseline JPEG can cross this process boundary unchanged.
    if header.baseline and (header.width, header.height) == (width, height):
        return data
    scale = min(width / frame.width, height / frame.height)
    scaled_width = max(1, min(width, math.floor(frame.width * scale)))
    scaled_height = max(1, min(height, math.floor(frame.height * scale)))
    scaled = frame.reformat(width=scaled_width, height=scaled_height, format="rgb24", threads=1)
    output = av.VideoFrame(width, height, "rgb24")
    plane = output.planes[0]
    pixels = bytearray(plane.buffer_size)
    source = bytes(scaled.planes[0])
    left, top = (width - scaled_width) // 2, (height - scaled_height) // 2
    for row in range(scaled_height):
        position = (top + row) * plane.line_size + left * 3
        pixels[position : position + scaled_width * 3] = source[
            row * scaled.planes[0].line_size : row * scaled.planes[0].line_size + scaled_width * 3
        ]
    plane.update(pixels)
    del pixels, source, scaled, frame, frames, decoder, data, plane
    # Tiny axes have no useful subsampling geometry. Use full chroma there.
    pixel_format = "yuvj444p" if min(width, height) == 1 else "yuvj420p"
    output = output.reformat(format=pixel_format, threads=1)
    encoder = av.CodecContext.create("mjpeg", "w")
    encoder.thread_count = 1
    encoder.width, encoder.height = width, height
    encoder.pix_fmt, encoder.time_base = pixel_format, Fraction(1, 1)
    encoder.global_quality = 5 * 118  # Fixed Q5 in FF_QP2LAMBDA units.
    encoder.flags |= 2  # AV_CODEC_FLAG_QSCALE, not caller-supplied options.
    output.pts = 0
    packets = encoder.encode(output) + encoder.encode(None)
    if len(packets) != 1:
        raise ValueError
    image = bytes(packets[0])
    del encoder, output, packets
    actual = wire.inspect_jpeg(image)
    if (actual.width, actual.height) != (width, height):
        raise ValueError
    verifier = av.CodecContext.create("mjpeg", "r")
    verifier.thread_count = 1
    verifier.options = {"max_pixels": str(wire.MAX_SOURCE_PIXELS), "err_detect": "explode"}
    verifier.open()
    if verifier.options:
        raise ValueError
    decoded = verifier.decode(av.Packet(image)) + verifier.decode(None)
    if (
        len(decoded) != 1
        or decoded[0].is_corrupt
        or (decoded[0].width, decoded[0].height) != (width, height)
    ):
        raise ValueError
    return image


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    width, height, length = struct.unpack("!III", read_exact(12))
    configuration = wire.SnapshotConfiguration(width, height)
    if not 4 <= length <= wire.MAX_JPEG_BYTES:
        raise wire.JpegError()
    write(b"J" + convert(read_exact(length), configuration))


if __name__ == "__main__":
    try:
        main()
    except wire.JpegError as error:
        with contextlib.suppress(BaseException):
            write(b"E" + error.code.encode("ascii"))
        raise SystemExit(1) from None
    except BaseException:
        with contextlib.suppress(BaseException):
            write(b"Eworker_failed")
        raise SystemExit(1) from None
