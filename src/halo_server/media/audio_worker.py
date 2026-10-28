"""Private one-thread Opus conversion process; no network, credentials or URLs."""

from __future__ import annotations

import array
import contextlib
import importlib.util
import json
import os
import resource
import struct
import sys
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

# -I deliberately excludes the checkout/import path. Only this fixed sibling is
# loaded; no caller-supplied module name, path or encoder option is accepted.
spec = importlib.util.spec_from_file_location(
    "_halo_opus_wire", Path(__file__).with_name("opus.py")
)
wire = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wire
spec.loader.exec_module(wire)
HEADER = struct.Struct("!cqI")
MAX_RESPONSE = 16 * 12000


def read_exact(size):
    result = bytearray()
    while len(result) < size:
        data = sys.stdin.buffer.read(size - len(result))
        if not data:
            raise EOFError
        result.extend(data)
    return bytes(result)


def write(body):
    if not 0 < len(body) <= MAX_RESPONSE:
        raise ValueError
    sys.stdout.buffer.write(struct.pack("!I", len(body)) + body)
    sys.stdout.buffer.flush()


def apply_gain(payload, gain):
    samples = array.array("h")
    samples.frombytes(payload)
    if sys.byteorder != "little":
        samples.byteswap()
    for index, sample in enumerate(samples):
        samples[index] = min(32767, max(-32768, round(sample * gain / 100)))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


class Converter:
    def __init__(self, settings):
        self.mode = settings["mode"]
        if self.mode not in {"encode", "decode"}:
            raise ValueError
        self.configuration = wire.AudioConfiguration(**settings["configuration"])
        self.decoder = av.CodecContext.create("opus", "r")
        self.decoder.thread_count = 1
        # Two output channels allow arbitrary mono/stereo source TOCs; the
        # resampler explicitly downmixes. No container pre-skip trims RTP input.
        self.decoder.extradata = b"OpusHead\x01\x02\0\0" + struct.pack("<I", 48000) + b"\0\0\0"
        rate = self.configuration.sample_rate if self.mode == "encode" else 48000
        self.resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        self.fifo = av.AudioFifo()
        self.rate, self.samples = rate, 0
        self.origin = None
        self.pending = []
        self.unit = 10 if self.configuration.packet_time == 30 else self.configuration.packet_time
        self.encoder = None
        if self.mode == "encode":
            self.encoder = av.CodecContext.create("libopus", "w")
            self.encoder.thread_count = 1
            self.encoder.sample_rate = rate
            self.encoder.layout, self.encoder.format = "mono", "s16"
            self.encoder.time_base = Fraction(1, rate)
            self.encoder.bit_rate = self.configuration.max_bit_rate * 1000
            if self.configuration.bitrate_mode == "cbr":
                budget = self.configuration.max_bit_rate * self.configuration.packet_time // 8
                unit_bytes = (
                    (budget - 2) // 3 + 1 if self.configuration.packet_time == 30 else budget
                )
                self.encoder.bit_rate = unit_bytes * 8000 // self.unit
            self.encoder.options = {
                "application": "lowdelay",
                "frame_duration": str(self.unit),
                "vbr": "off" if self.configuration.bitrate_mode == "cbr" else "constrained",
                "fec": "off",
            }
            self.encoder.open()
            if self.encoder.options:
                raise ValueError("Unused fixed codec options")

    def feed(self, payload, timestamp, *, flush=False):
        if self.origin is None:
            self.origin = timestamp
        result = []
        if payload:
            wire.parse_opus(payload)
            packet = av.Packet(payload)
            packet.pts, packet.dts, packet.time_base = timestamp, None, Fraction(1, 48000)
            frames = self.decoder.decode(packet)
        else:
            frames = self.decoder.decode(None) if flush else []
        for frame in frames:
            if frame.samples > 5760 or frame.sample_rate != 48000:
                raise ValueError
            for normalized in self.resampler.resample(frame):
                normalized.pts = None
                self.fifo.write(normalized)
        if flush:
            for normalized in self.resampler.resample(None):
                normalized.pts = None
                self.fifo.write(normalized)
        unit_samples = self.rate * (self.unit if self.mode == "encode" else 20) // 1000
        if flush and self.fifo.samples % unit_samples:
            count = unit_samples - self.fifo.samples % unit_samples
            padding = av.AudioFrame(format="s16", layout="mono", samples=count)
            padding.sample_rate = self.rate
            padding.planes[0].update(bytes(padding.planes[0].buffer_size))
            self.fifo.write(padding)
        while self.fifo.samples >= unit_samples:
            frame = self.fifo.read(unit_samples)
            data = bytes(frame.planes[0])[: unit_samples * 2]
            if self.configuration.gain != 100:
                data = apply_gain(data, self.configuration.gain)
            if self.mode == "decode":
                if len(data) != 1920:
                    raise ValueError
                result.append((self.origin + self.samples, 960, data))
            else:
                frame.planes[0].update(data + bytes(frame.planes[0].buffer_size - len(data)))
                frame.pts, frame.time_base = self.samples, Fraction(1, self.rate)
                result.extend(self.encode(self.encoder.encode(frame)))
            self.samples += unit_samples
        if flush and self.encoder is not None:
            if self.configuration.packet_time == 30:
                # libopus emits its final lookahead packet when flushed. Pad
                # with newly encoded silence so that tail joins a complete
                # three-frame packet; never discard a source tail or repeat it.
                for _ in range((2 - len(self.pending)) % 3):
                    frame = av.AudioFrame(format="s16", layout="mono", samples=unit_samples)
                    frame.sample_rate = self.rate
                    frame.planes[0].update(bytes(frame.planes[0].buffer_size))
                    frame.pts, frame.time_base = self.samples, Fraction(1, self.rate)
                    result.extend(self.encode(self.encoder.encode(frame)))
                    self.samples += unit_samples
            result.extend(self.encode(self.encoder.encode(None)))
            if self.pending:
                raise ValueError("Incomplete native Opus tail")
        if len(result) > 16:
            raise ValueError
        return result

    def encode(self, packets):
        result = []
        for packet in packets:
            payload = bytes(packet)
            parsed = wire.parse_opus(payload)
            if parsed.channels != 1 or parsed.bandwidth_rate > self.rate:
                raise ValueError
            timestamp = self.origin + round(packet.pts * packet.time_base * 48000)
            if self.configuration.packet_time == 30:
                if self.pending and parsed.toc != wire.parse_opus(self.pending[0][1]).toc:
                    raise ValueError("Encoder changed Opus configuration")
                self.pending.append((timestamp, payload))
                if len(self.pending) < 3:
                    continue
                timestamp = self.pending[0][0]
                payload = wire.repacketize(item[1] for item in self.pending)
                self.pending.clear()
                parsed = wire.parse_opus(payload)
            if (
                parsed.duration_samples != self.configuration.packet_time * 48
                or len(payload) > wire.MAX_AUDIO_PAYLOAD
                or (
                    self.configuration.bitrate_mode == "cbr"
                    and len(payload) * 8
                    > self.configuration.max_bit_rate * self.configuration.packet_time
                )
            ):
                raise ValueError
            result.append((timestamp, parsed.duration_samples, payload))
        return result


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    size = struct.unpack("!I", read_exact(4))[0]
    if not 0 < size <= 4096:
        raise ValueError
    converter = Converter(json.loads(read_exact(size)))
    write(b"S")
    while True:
        kind, timestamp, size = HEADER.unpack(read_exact(HEADER.size))
        if kind not in {b"F", b"D"} or not 0 <= size <= wire.MAX_OPUS_PACKET:
            raise ValueError
        result = converter.feed(read_exact(size), timestamp, flush=kind == b"D")
        body = b"A" + bytes((len(result),))
        for pts, duration, payload in result:
            body += struct.pack("!qHI", pts, duration, len(payload)) + payload
        write(body)


if __name__ == "__main__":
    try:
        main()
    except EOFError:
        pass
    except BaseException:
        with contextlib.suppress(BaseException):
            write(b"Eaudio-unavailable")
        raise SystemExit(1) from None
