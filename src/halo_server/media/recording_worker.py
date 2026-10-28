"""Fixed isolated H.264/AAC recording mux; no URLs, SDK objects or user options."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import resource
import struct
import sys
from collections import deque
from fractions import Fraction
from pathlib import Path


def load(name):
    spec = importlib.util.spec_from_file_location(
        "_halo_recording_" + name, Path(__file__).with_name(name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    load("worker_limits").apply_child_cpu_limit()

import av  # noqa: E402 - child affinity must be limited before native libraries load.

h264 = load("h264")
opus = load("opus")
video_tools = load("transcode_worker")
HEADER = struct.Struct("!cqqI")
MAX_INIT = 1024 * 1024
MAX_FRAGMENT = 8 * 1024 * 1024


def exact(count):
    output = bytearray()
    while len(output) < count:
        part = sys.stdin.buffer.read(count - len(output))
        if not part:
            raise EOFError
        output.extend(part)
    return bytes(output)


def write(payload):
    if not 0 < len(payload) <= 2 * MAX_FRAGMENT + 16:
        raise ValueError
    sys.stdout.buffer.write(struct.pack("!I", len(payload)) + payload)
    sys.stdout.buffer.flush()


def slice_kind(nal):
    bits = h264._Bits(h264._rbsp(nal[1:]))
    bits.ue(2**24)
    return bits.ue(9) % 5


class Sink:
    """Native IO may retain only one incomplete box and two complete packets."""

    def __init__(self):
        self.buffer = bytearray()
        self.init = bytearray()
        self.moof = None
        self.output = []
        self.initialized = False

    def writable(self):
        return True

    def write(self, data):
        if len(self.buffer) + len(data) > MAX_FRAGMENT + MAX_INIT:
            raise ValueError
        self.buffer.extend(data)
        while len(self.buffer) >= 8:
            size, kind = struct.unpack_from("!I4s", self.buffer)
            if size < 8 or size > (MAX_FRAGMENT if kind == b"mdat" else MAX_INIT):
                raise ValueError
            if len(self.buffer) < size:
                break
            box = bytes(self.buffer[:size])
            del self.buffer[:size]
            if not self.initialized and kind in {b"ftyp", b"moov"}:
                if len(self.init) + len(box) > MAX_INIT or (kind == b"ftyp") != (not self.init):
                    raise ValueError
                self.init.extend(box)
                if kind == b"moov":
                    self.output.append((b"I", bytes(self.init)))
                    self.init.clear()
                    self.initialized = True
            elif self.initialized and kind == b"moof" and self.moof is None:
                self.moof = box
            elif self.initialized and kind == b"mdat" and self.moof is not None:
                if len(self.moof) + len(box) > MAX_FRAGMENT:
                    raise ValueError
                self.output.append((b"M", self.moof + box))
                self.moof = None
            else:
                raise ValueError
            if (
                len(self.output) > 2
                or sum(len(value) for _, value in self.output) > 2 * MAX_FRAGMENT
            ):
                raise ValueError
        return len(data)

    def take(self):
        output, self.output = self.output, []
        return output


class Recording:
    def __init__(self, config):
        self.config = config
        self.video = h264.VideoConfiguration(**config["video"])
        self.audio_config = config["audio"]
        self.sps, self.pps = bytes.fromhex(config["video_sps"]), bytes.fromhex(config["video_pps"])
        self.source = h264.parse_sps(self.sps)
        interval = config["i_frame_interval_ms"]
        if (
            type(interval) is not int
            or not (1000 + self.video.fps - 1) // self.video.fps <= interval <= 4000
        ):
            raise ValueError
        self.key_interval = min(Fraction(interval, 1000), Fraction(19, 5))
        self.encoder_gop = max(1, self.video.fps * min(2000, interval) // 1000)
        self.source_key = None
        if h264.pps_sequence_id(self.pps) != self.source.sequence_id:
            raise ValueError
        clocks = {clock["kind"]: clock for clock in config["clocks"]}
        wanted = {"video", "audio"} if self.audio_config else {"video"}
        self.clocks = clocks if set(clocks) == wanted else {}
        for clock in self.clocks.values():
            if (
                type(clock["sender_ntp"]) is not int
                or not 0 <= clock["sender_ntp"] < 2**64
                or type(clock["rtp_timestamp"]) is not int
            ):
                raise ValueError
        self.anchors, self.origin = {}, None
        self.first_track_times = {}
        self.copy_possible = (
            h264.compatible(self.source, self.video) and self.source.frame_rate == self.video.fps
        )
        self.mode = None
        self.buffered_video = []
        self.buffered_bytes = 0
        self.pending_audio = []
        self.pending_audio_bytes = 0
        self.first_video = None
        self.last_video = None
        self.last_key = None
        self.last_audio = None
        self.copy_ledger = deque()
        self.sink = Sink()
        self.container = self.track = self.audio_track = None
        self.decoder = self.encoder = None
        self.audio_decoder = self.resampler = self.fifo = None
        self.last_frame = None
        self.last_frame_pts = None
        self.last_frame_delta = Fraction(1, self.video.fps)
        self.next_output = None
        self.finished = False

    def time(self, kind, stamp, received):
        rate = 90000 if kind == "video" else 48000
        if self.clocks:
            clock = self.clocks[kind]
            current = Fraction(clock["sender_ntp"], 2**32) + Fraction(
                stamp - clock["rtp_timestamp"], rate
            )
        else:
            anchor = self.anchors.setdefault(kind, (stamp, Fraction(received, 1_000_000_000)))
            current = anchor[1] + Fraction(stamp - anchor[0], rate)
        if self.origin is None:
            self.origin = current
        relative = current - self.origin
        # A newly admitted track may include real pre-roll and encoder priming,
        # but cannot start beyond the bounded initial recording history. Later
        # samples use the separate whole-job limit and never rebase this origin.
        if kind not in self.first_track_times:
            if not -8 <= relative <= 8:
                raise ValueError
            self.first_track_times[kind] = relative
        if not -8 <= relative <= 192:
            raise ValueError
        return relative

    def open_mux(self, initial):
        template_container = av.open(
            io.BytesIO(initial), format="h264", options={"threads": "1", "analyzeduration": "0"}
        )
        self.container = av.open(
            self.sink,
            "w",
            "mp4",
            options={
                "movflags": "frag_keyframe+empty_moov+delay_moov+default_base_moof+skip_trailer",
                "min_frag_duration": "1500000",
                "max_interleave_delta": "1000000",
            },
        )
        self.track = self.container.add_stream_from_template(template_container.streams.video[0])
        self.track.time_base = Fraction(1, 90000)
        if self.audio_config:
            self.audio_track = self.container.add_stream(
                "aac", rate=self.audio_config["sample_rate"]
            )
            self.audio_track.layout = "mono"
            self.audio_track.bit_rate = self.audio_config["bit_rate"]
            self.audio_track.codec_context.thread_count = 1
            self.audio_track.codec_context.profile = "LC"
            self.audio_track.time_base = Fraction(1, self.audio_config["sample_rate"])
            self.audio_decoder = av.CodecContext.create("opus", "r")
            self.audio_decoder.thread_count = 1
            self.audio_decoder.extradata = (
                b"OpusHead\x01\x02\0\0" + struct.pack("<I", 48000) + b"\0\0\0"
            )
            self.resampler = av.AudioResampler(
                format="fltp", layout="mono", rate=self.audio_config["sample_rate"]
            )
            self.fifo = av.AudioFifo()
        self.container.start_encoding()
        queued, self.pending_audio = self.pending_audio, []
        self.pending_audio_bytes = 0
        for payload, pts, duration in queued:
            self.encode_audio(payload, pts, duration)

    def mux_video(self, payload, pts, *, key):
        if self.container is None:
            self.open_mux(payload)
        if self.last_key is None:
            if not key:
                raise ValueError
            self.last_key = pts
        elif key:
            if pts - self.last_key > self.key_interval:
                raise ValueError
            self.last_key = pts
        elif pts - self.last_key > self.key_interval:
            raise ValueError
        packet = av.Packet(payload)
        packet.pts = packet.dts = round(pts * 90000)
        packet.duration = round(90000 / self.video.fps)
        packet.time_base = Fraction(1, 90000)
        packet.is_keyframe = key
        packet.stream = self.track
        self.container.mux(packet)

    def select(self, mode):
        if mode == "copy":
            ledger = deque()
            for payload, pts, _ in self.buffered_video:
                while ledger and ledger[0][0] < pts - 1:
                    ledger.popleft()
                ledger.append((pts, len(payload)))
                if sum(length for _, length in ledger) * 8 > self.video.max_bit_rate * 1000:
                    mode = "adapt"
                    break
        self.mode = mode
        buffered, self.buffered_video = self.buffered_video, []
        self.buffered_bytes = 0
        if mode == "adapt":
            self.decoder = av.CodecContext.create("h264", "r")
            self.decoder.thread_count = 1
            self.encoder = av.CodecContext.create("libx264", "w")
            self.encoder.width, self.encoder.height = self.video.width, self.video.height
            self.encoder.pix_fmt = "yuv420p"
            self.encoder.time_base, self.encoder.framerate = (
                Fraction(1, 90000),
                Fraction(self.video.fps),
            )
            self.encoder.thread_count, self.encoder.max_b_frames = 1, 0
            self.encoder.gop_size, self.encoder.bit_rate = (
                max(2, self.encoder_gop),
                self.video.max_bit_rate * 1000,
            )
            self.encoder.options = {
                "preset": "veryfast",
                "tune": "zerolatency",
                "profile": self.video.profile,
                "level": self.video.level,
                "x264-params": (
                    f"bframes=0:scenecut=0:repeat-headers=1:keyint={max(2, self.encoder_gop)}:"
                    f"threads=1:vbv-maxrate={self.video.max_bit_rate}:"
                    f"vbv-bufsize={self.video.max_bit_rate}"
                ),
            }
            if self.encoder_gop == 1:
                # x264's keyint=1 switches to High Intra SPS and omits the
                # explicit zero-reorder VUI. Keep the normal selected profile
                # and VUI, while forcing an actual IDR for every picture.
                self.encoder.options["forced-idr"] = "1"
            self.encoder.open()
            if self.encoder.options:
                raise ValueError
        for payload, pts, key in buffered:
            self.convert_video(payload, pts, key)

    def video_sample(self, payload, pts):
        nals = h264.split_annexb(payload)
        for nal in nals:
            if nal[0] & 31 == 7 and nal != self.sps or nal[0] & 31 == 8 and nal != self.pps:
                raise ValueError
        slices = [nal for nal in nals if nal[0] & 31 in {1, 5}]
        if not slices:
            return
        key = any(nal[0] & 31 == 5 for nal in slices)
        # Measure the source's keyframe cadence before selecting copy. A short
        # selected interval cannot inherit an older source's longer GOP.
        key_gap = pts - self.source_key if self.source_key is not None else Fraction(0)
        if key:
            self.source_key = pts
        if key_gap > self.key_interval:
            self.copy_possible = False
        reordered = any(slice_kind(nal) not in {0, 2} for nal in slices)
        if self.first_video is None:
            if not key:
                return
            self.first_video = pts
            payload = h264.annexb(
                (self.sps, self.pps, *(nal for nal in nals if nal[0] & 31 not in {7, 8}))
            )
        if (
            self.last_video is not None
            and self.mode == "copy"
            and (
                pts <= self.last_video
                or pts - self.last_video < Fraction(1, self.video.fps)
                or reordered
            )
        ):
            raise ValueError
        if self.mode is None:
            self.buffered_video.append((payload, pts, key))
            self.buffered_bytes += len(payload)
            elapsed = pts - self.first_video
            if self.buffered_bytes > 8 * 1024 * 1024:
                raise ValueError
            budget = self.video.max_bit_rate * 1000 * max(Fraction(1), elapsed) / 8
            if (
                not self.copy_possible
                or reordered
                or self.last_video is not None
                and pts - self.last_video < Fraction(1, self.video.fps)
                or self.buffered_bytes > budget
                or elapsed >= Fraction(19, 5)
            ):
                self.select("adapt")
            elif key and elapsed >= Fraction(3, 2):
                self.select("copy")
        else:
            self.convert_video(payload, pts, key)
        self.last_video = pts

    def convert_video(self, payload, pts, key):
        if self.mode == "copy":
            while self.copy_ledger and self.copy_ledger[0][0] < pts - 1:
                self.copy_ledger.popleft()
            self.copy_ledger.append((pts, len(payload)))
            if sum(length for _, length in self.copy_ledger) * 8 > self.video.max_bit_rate * 1000:
                raise ValueError
            self.mux_video(payload, pts, key=key)
        else:
            packet = av.Packet(payload)
            packet.pts, packet.dts, packet.time_base = round(pts * 90000), None, Fraction(1, 90000)
            self.frames(self.decoder.decode(packet))

    def frames(self, frames):
        if len(frames) > 16:
            raise ValueError
        for frame in frames:
            if (
                frame.is_corrupt
                or frame.width * frame.height > h264.MAX_SOURCE_PIXELS
                or frame.pts is None
            ):
                raise ValueError
            pts = Fraction(frame.pts) * (frame.time_base or Fraction(1, 90000))
            if self.last_frame_pts is not None:
                delta = pts - self.last_frame_pts
                if not 0 < delta <= Fraction(1, 4):
                    raise ValueError
                self.last_frame_delta = delta
            if self.next_output is None:
                self.next_output = pts
            while self.next_output < pts:
                self.encode_frame(self.last_frame, self.next_output)
                self.next_output += Fraction(1, self.video.fps)
            output = video_tools.letterbox(frame, self.video.width, self.video.height)
            self.last_frame, self.last_frame_pts = output, pts
            if self.next_output == pts:
                self.encode_frame(output, self.next_output)
                self.next_output += Fraction(1, self.video.fps)

    def encode_frame(self, frame, pts):
        if frame is None:
            raise ValueError
        frame.pts, frame.time_base = round(pts * 90000), Fraction(1, 90000)
        if self.encoder_gop == 1:
            frame.pict_type = av.video.frame.PictureType.I
        for packet in self.encoder.encode(frame):
            if packet.pts != packet.dts or len(bytes(packet)) > 2 * 1024 * 1024:
                raise ValueError
            self.mux_video(
                bytes(packet), Fraction(packet.pts) * packet.time_base, key=packet.is_keyframe
            )

    def audio_sample(self, payload, pts):
        parsed = opus.parse_opus(payload)
        duration = Fraction(parsed.duration_samples, 48000)
        if not self.audio_config or self.last_audio is not None and pts != self.last_audio:
            raise ValueError
        self.last_audio = pts + duration
        if self.container is None:
            self.pending_audio.append((payload, pts, duration))
            self.pending_audio_bytes += len(payload)
            if len(self.pending_audio) > 256 or self.pending_audio_bytes > 512 * 1024:
                raise ValueError
        else:
            self.encode_audio(payload, pts, duration)

    def encode_audio(self, payload, pts, duration):
        packet = av.Packet(payload)
        packet.pts, packet.dts, packet.time_base = round(pts * 48000), None, Fraction(1, 48000)
        frames = self.audio_decoder.decode(packet)
        if len(frames) > 16:
            raise ValueError
        for frame in frames:
            if frame.samples > 5760 or frame.sample_rate != 48000:
                raise ValueError
            for output in self.resampler.resample(frame):
                self.fifo.write(output)
        self.drain_audio()

    def drain_audio(self, *, partial=False):
        while self.fifo.samples >= 1024 or partial and self.fifo.samples:
            frame = self.fifo.read(min(1024, self.fifo.samples))
            for packet in self.audio_track.encode(frame):
                self.container.mux(packet)

    def finish(self):
        if self.mode is None:
            if not self.buffered_video:
                raise ValueError
            self.select("adapt" if not self.copy_possible else "copy")
        if self.mode == "adapt":
            self.frames(self.decoder.decode(None))
            end = self.last_frame_pts + self.last_frame_delta
            while self.next_output < end:
                self.encode_frame(self.last_frame, self.next_output)
                self.next_output += Fraction(1, self.video.fps)
            for packet in self.encoder.encode(None):
                if packet.pts != packet.dts:
                    raise ValueError
                self.mux_video(
                    bytes(packet), Fraction(packet.pts) * packet.time_base, key=packet.is_keyframe
                )
        if self.audio_track is not None:
            for frame in self.resampler.resample(None):
                self.fifo.write(frame)
            self.drain_audio(partial=True)
            for packet in self.audio_track.encode(None):
                self.container.mux(packet)
        self.container.close()
        if self.sink.buffer or self.sink.moof is not None:
            raise ValueError
        self.finished = True

    def feed(self, kind, stamp, received, payload):
        if self.finished:
            raise ValueError
        if kind == b"P":
            return native_probe(self.config)
        if kind == b"F":
            if payload:
                raise ValueError
            self.finish()
        else:
            pts = self.time("video" if kind == b"V" else "audio", stamp, received)
            if kind == b"V":
                self.video_sample(payload, pts)
            elif kind == b"A":
                self.audio_sample(payload, pts)
            else:
                raise ValueError
        packets = self.sink.take()
        return (
            b"O"
            + bytes((len(packets),))
            + b"".join(kind + struct.pack("!I", len(data)) + data for kind, data in packets)
        )


def native_probe(config):
    # Entire synthetic encode/mux/decode qualification remains killable.
    import array
    import math

    encoder = av.CodecContext.create("libx264", "w")
    selected = h264.VideoConfiguration(**config["video"])
    encoder.width, encoder.height = selected.width, selected.height
    encoder.pix_fmt, encoder.time_base, encoder.framerate = (
        "yuv420p",
        Fraction(1, 90000),
        Fraction(selected.fps),
    )
    encoder.thread_count, encoder.max_b_frames, encoder.gop_size = 1, 0, selected.fps * 2
    encoder.options = {
        "profile": selected.profile,
        "level": selected.level,
        "preset": "veryfast",
        "tune": "zerolatency",
        "x264-params": f"bframes=0:repeat-headers=1:scenecut=0:keyint={selected.fps * 2}:threads=1",
    }
    encoder.open()
    video = []
    for index in range(selected.fps * 2 + 4):
        frame = av.VideoFrame(selected.width, selected.height, "yuv420p")
        for plane_index, plane in enumerate(frame.planes):
            plane.update(bytes([70 if plane_index == 0 else 128]) * plane.buffer_size)
        frame.pts, frame.time_base = index * 90000 // selected.fps, Fraction(1, 90000)
        video.extend(encoder.encode(frame))
    video.extend(encoder.encode(None))
    nals = h264.split_annexb(bytes(video[0]))
    settings = dict(config)
    settings["video_sps"] = next(nal.hex() for nal in nals if nal[0] & 31 == 7)
    settings["video_pps"] = next(nal.hex() for nal in nals if nal[0] & 31 == 8)
    settings["clocks"] = []
    worker = Recording(settings)
    inputs = [
        (
            Fraction(packet.pts) * packet.time_base,
            b"V",
            round(packet.pts * packet.time_base * 90000),
            bytes(packet),
        )
        for packet in video
    ]
    audio_encoder = av.CodecContext.create("libopus", "w")
    audio_encoder.thread_count = 1
    audio_encoder.sample_rate, audio_encoder.layout, audio_encoder.format = 48000, "mono", "s16"
    audio_encoder.time_base = Fraction(1, 48000)
    audio_encoder.options = {"application": "lowdelay", "frame_duration": "20", "vbr": "off"}
    audio_encoder.open()
    for index in range(115):
        frame = av.AudioFrame(format="s16", layout="mono", samples=960)
        frame.sample_rate, frame.pts, frame.time_base = 48000, index * 960, Fraction(1, 48000)
        values = array.array(
            "h",
            (
                round(math.sin((index * 960 + i) * 2 * math.pi * 440 / 48000) * 10000)
                for i in range(960)
            ),
        )
        frame.planes[0].update(values.tobytes())
        for packet in audio_encoder.encode(frame):
            inputs.append((Fraction(index, 50), b"A", index * 960, bytes(packet)))
    outputs = []
    for moment, kind, stamp, payload in sorted(inputs, key=lambda item: (item[0], item[1])):
        response = worker.feed(kind, stamp, 10_000_000_000 + round(moment * 1_000_000_000), payload)
        offset = 2
        for _ in range(response[1]):
            length = struct.unpack_from("!I", response, offset + 1)[0]
            outputs.append(
                (response[offset : offset + 1], response[offset + 5 : offset + 5 + length])
            )
            offset += 5 + length
    if len(outputs) != 2 or [kind for kind, _ in outputs] != [b"I", b"M"]:
        raise ValueError
    movie = av.open(
        io.BytesIO(outputs[0][1] + outputs[1][1]), format="mp4", options={"threads": "1"}
    )
    counts = {"video": 0, "audio": 0}
    for stream in movie.streams:
        stream.codec_context.thread_count = 1
    for packet in movie.demux():
        for frame in packet.decode():
            if frame.is_corrupt:
                raise ValueError
            counts[packet.stream.type] += 1
    if not counts["video"] or not counts["audio"]:
        raise ValueError
    return (
        b"O"
        + bytes((len(outputs),))
        + b"".join(kind + struct.pack("!I", len(data)) + data for kind, data in outputs)
    )


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    count = struct.unpack("!I", exact(4))[0]
    if not 1 <= count <= 300000:
        raise ValueError
    recording = Recording(json.loads(exact(count)))
    write(b"S")
    while not recording.finished:
        kind, stamp, received, count = HEADER.unpack(exact(HEADER.size))
        if kind not in {b"V", b"A", b"F", b"P"} or not 0 <= count <= (
            h264.MAX_ACCESS_UNIT_BYTES
            if kind == b"V"
            else opus.MAX_OPUS_PACKET
            if kind == b"A"
            else 0
        ):
            raise ValueError
        write(recording.feed(kind, stamp, received, exact(count)))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        with contextlib.suppress(BaseException):
            write(b"E")
        raise SystemExit(1) from None
