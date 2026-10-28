"""Finite initialization/fragment admission for the fixed native H.264/AAC mux."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from fractions import Fraction

from halo_server.media.h264 import H264Error, parse_sps, pps_sequence_id

MAX_INIT_BYTES = 1024 * 1024
MAX_FRAGMENT_BYTES = 8 * 1024 * 1024
MAX_SAMPLES = 4096


class Mp4Error(ValueError):
    def __init__(self):
        super().__init__("Invalid recording container")


@dataclass(frozen=True)
class Box:
    kind: bytes
    offset: int
    size: int
    header: int

    @property
    def end(self):
        return self.offset + self.size


def boxes(data: bytes | memoryview, *, limit=MAX_FRAGMENT_BYTES) -> tuple[Box, ...]:
    if not isinstance(data, (bytes, memoryview)) or len(data) > limit:
        raise Mp4Error()
    result = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8 or len(result) >= 1024:
            raise Mp4Error()
        size, kind = struct.unpack_from("!I4s", data, offset)
        header = 8
        if size == 1:
            if len(data) - offset < 16:
                raise Mp4Error()
            size, header = struct.unpack_from("!Q", data, offset + 8)[0], 16
        if size < header or size > len(data) - offset:
            raise Mp4Error()
        result.append(Box(kind, offset, size, header))
        offset += size
    return tuple(result)


def _children(data):
    result = {}
    for box in boxes(data):
        if box.kind in result:
            raise Mp4Error()
        result[box.kind] = memoryview(data)[box.offset + box.header : box.end]
    return result


def _u32(data, offset=0):
    if offset < 0 or offset + 4 > len(data):
        raise Mp4Error()
    return struct.unpack_from("!I", data, offset)[0]


def _avcc(data):
    if len(data) < 7 or data[0] != 1 or data[4] != 255 or data[5] & 31 != 1:
        raise Mp4Error()
    offset = 6
    length = int.from_bytes(data[offset : offset + 2], "big")
    offset += 2
    if not 4 <= length <= 65536 or offset + length + 3 > len(data):
        raise Mp4Error()
    sps = bytes(data[offset : offset + length])
    offset += length
    if data[offset] != 1:
        raise Mp4Error()
    offset += 1
    length = int.from_bytes(data[offset : offset + 2], "big")
    offset += 2
    if not 2 <= length <= 65536 or offset + length > len(data):
        raise Mp4Error()
    pps = bytes(data[offset : offset + length])
    offset += length
    if sps[0] & 31 != 7 or pps[0] & 31 != 8:
        raise Mp4Error()
    info = parse_sps(sps)
    if pps_sequence_id(pps) != info.sequence_id:
        raise Mp4Error()
    if bytes(data[1:4]) != sps[1:4]:
        raise Mp4Error()
    tail = bytes(data[offset:])
    # Fixed native eight-bit 4:2:0 output may include AVC high-profile extension.
    if tail not in {b"", b"\xfd\xf8\xf8\0"}:
        raise Mp4Error()
    if not info.progressive or info.chroma_format != 1 or info.bit_depth != 8:
        raise Mp4Error()
    return sps, pps


@dataclass(frozen=True)
class Track:
    identifier: int
    kind: str
    timescale: int
    default_duration: int
    default_size: int
    default_flags: int
    width: int = 0
    height: int = 0
    sps: bytes = b""
    pps: bytes = b""
    sample_rate: int = 0
    channels: int = 0


def _descriptor(data, offset):
    if offset >= len(data):
        raise Mp4Error()
    tag, offset, length = data[offset], offset + 1, 0
    for _ in range(4):
        if offset >= len(data):
            raise Mp4Error()
        value = data[offset]
        offset += 1
        length = (length << 7) | (value & 127)
        if not value & 128:
            break
    else:
        raise Mp4Error()
    if length > len(data) - offset:
        raise Mp4Error()
    return tag, data[offset : offset + length], offset + length


def _aac_config(esds):
    if len(esds) > 4096 or bytes(esds[:4]) != b"\0" * 4:
        raise Mp4Error()
    tag, es, end = _descriptor(esds, 4)
    if tag != 3 or end != len(esds) or len(es) < 3 or es[2] != 0:
        raise Mp4Error()
    tag, decoder, _ = _descriptor(es, 3)
    if tag != 4 or len(decoder) < 13 or decoder[0] != 0x40 or decoder[1] >> 2 != 5:
        raise Mp4Error()
    tag, asc, end = _descriptor(decoder, 13)
    if tag != 5 or end != len(decoder) or len(asc) < 2:
        raise Mp4Error()
    value = int.from_bytes(asc[:2], "big")
    rate_index = (value >> 7) & 15
    channels = (value >> 3) & 15
    rates = (
        96000,
        88200,
        64000,
        48000,
        44100,
        32000,
        24000,
        22050,
        16000,
        12000,
        11025,
        8000,
        7350,
    )
    if (
        value >> 11 != 2
        or rate_index >= len(rates)
        or channels != 1
        or value & 7
        or bytes(asc[2:]) not in {b"", b"\x56\xe5\0"}
    ):
        raise Mp4Error()
    return rates[rate_index], channels


@dataclass(frozen=True)
class Movie:
    tracks: tuple[Track, ...]


def _inspect_init(data: bytes, *, audio: bool) -> Movie:
    top = boxes(data, limit=MAX_INIT_BYTES)
    if tuple(box.kind for box in top) != (b"ftyp", b"moov"):
        raise Mp4Error()
    moov = memoryview(data)[top[1].offset + top[1].header : top[1].end]
    tracks, defaults = [], {}
    children = boxes(moov)
    for atom in children:
        if atom.kind != b"mvex":
            continue
        for box in boxes(moov[atom.offset + atom.header : atom.end]):
            if box.kind != b"trex":
                continue
            raw = moov[
                atom.offset + atom.header + box.offset + box.header : atom.offset
                + atom.header
                + box.end
            ]
            if len(raw) != 24 or bytes(raw[:4]) != b"\0" * 4:
                raise Mp4Error()
            identifier = _u32(raw, 4)
            if identifier in defaults:
                raise Mp4Error()
            defaults[identifier] = (_u32(raw, 12), _u32(raw, 16), _u32(raw, 20))
    for atom in children:
        if atom.kind != b"trak":
            continue
        values = _children(moov[atom.offset + atom.header : atom.end])
        if b"tkhd" not in values or b"mdia" not in values:
            raise Mp4Error()
        tkhd, mdia = values[b"tkhd"], _children(values[b"mdia"])
        if (
            len(tkhd) < 20
            or tkhd[0] not in {0, 1}
            or b"mdhd" not in mdia
            or b"hdlr" not in mdia
            or b"minf" not in mdia
        ):
            raise Mp4Error()
        identifier = _u32(tkhd, 12 if tkhd[0] == 0 else 20)
        mdhd, hdlr = mdia[b"mdhd"], mdia[b"hdlr"]
        if len(mdhd) < 20 or mdhd[0] not in {0, 1} or len(hdlr) < 12:
            raise Mp4Error()
        timescale = _u32(mdhd, 12 if mdhd[0] == 0 else 20)
        if not 1 <= timescale <= 1_000_000 or identifier < 1 or identifier not in defaults:
            raise Mp4Error()
        minf = _children(mdia[b"minf"])
        stbl = _children(minf.get(b"stbl", b""))
        stsd = stbl.get(b"stsd", b"")
        if len(stsd) < 8 or _u32(stsd, 4) != 1:
            raise Mp4Error()
        entries = boxes(stsd[8:])
        if len(entries) != 1:
            raise Mp4Error()
        entry = entries[0]
        body = stsd[8 + entry.offset + entry.header : 8 + entry.end]
        if bytes(hdlr[8:12]) == b"vide" and entry.kind == b"avc1":
            if len(body) < 78:
                raise Mp4Error()
            width, height = struct.unpack_from("!HH", body, 24)
            extra = _children(body[78:]).get(b"avcC")
            if extra is None:
                raise Mp4Error()
            sps, pps = _avcc(extra)
            info = parse_sps(sps)
            if (width, height) != (info.width, info.height):
                raise Mp4Error()
            track = Track(
                identifier, "video", timescale, *defaults[identifier], width, height, sps, pps
            )
        elif bytes(hdlr[8:12]) == b"soun" and entry.kind == b"mp4a" and audio:
            if (
                len(body) < 28
                or struct.unpack_from("!H", body, 16)[0] != 1
                or b"esds" not in _children(body[28:])
            ):
                raise Mp4Error()
            rate, channels = _aac_config(_children(body[28:])[b"esds"])
            if _u32(body, 24) >> 16 != rate or timescale != rate:
                raise Mp4Error()
            track = Track(
                identifier,
                "audio",
                timescale,
                *defaults[identifier],
                sample_rate=rate,
                channels=channels,
            )
        else:
            raise Mp4Error()
        tracks.append(track)
    if (
        len(tracks) != 1 + int(audio)
        or len({track.identifier for track in tracks}) != len(tracks)
        or set(defaults) != {track.identifier for track in tracks}
        or sorted(track.kind for track in tracks) != (["audio", "video"] if audio else ["video"])
    ):
        raise Mp4Error()
    return Movie(tuple(tracks))


@dataclass(frozen=True)
class TrackFragment:
    identifier: int
    start: int
    duration: int
    timescale: int
    samples: int

    @property
    def duration_seconds(self):
        return Fraction(self.duration, self.timescale)


@dataclass(frozen=True)
class Fragment:
    sequence: int
    tracks: tuple[TrackFragment, ...]

    @property
    def duration_seconds(self):
        return max(track.duration_seconds for track in self.tracks)


def _inspect_fragment(data: bytes, movie: Movie, *, maximum_seconds=Fraction(4)) -> Fragment:
    top = boxes(data)
    if tuple(box.kind for box in top) != (b"moof", b"mdat"):
        raise Mp4Error()
    moof, mdat = top
    known = {track.identifier: track for track in movie.tracks}
    body = memoryview(data)[moof.offset + moof.header : moof.end]
    sequence = None
    tracks, ranges = [], []
    for box in boxes(body):
        raw = body[box.offset + box.header : box.end]
        if box.kind == b"mfhd":
            if sequence is not None or len(raw) != 8 or bytes(raw[:4]) != b"\0" * 4:
                raise Mp4Error()
            sequence = _u32(raw, 4)
            continue
        if box.kind != b"traf":
            raise Mp4Error()
        atoms = boxes(raw)
        heads = [atom for atom in atoms if atom.kind == b"tfhd"]
        times = [atom for atom in atoms if atom.kind == b"tfdt"]
        if len(heads) != 1 or len(times) != 1:
            raise Mp4Error()
        header = raw[heads[0].offset + heads[0].header : heads[0].end]
        flags = _u32(header) & 0xFFFFFF
        identifier = _u32(header, 4)
        if (
            identifier not in known
            or any(track.identifier == identifier for track in tracks)
            or not flags & 0x020000
            or flags & ~0x02003A
        ):
            raise Mp4Error()
        track = known[identifier]
        cursor = 8 + (4 if flags & 2 else 0)
        duration, size, sample_flags = (
            track.default_duration,
            track.default_size,
            track.default_flags,
        )
        if flags & 8:
            duration, cursor = _u32(header, cursor), cursor + 4
        if flags & 16:
            size, cursor = _u32(header, cursor), cursor + 4
        if flags & 32:
            sample_flags, cursor = _u32(header, cursor), cursor + 4
        if cursor != len(header):
            raise Mp4Error()
        time = raw[times[0].offset + times[0].header : times[0].end]
        if (
            len(time) < 8
            or len(time) != (12 if time[0] == 1 else 8)
            or time[0] not in {0, 1}
            or bytes(time[1:4]) != b"\0" * 3
        ):
            raise Mp4Error()
        start = int.from_bytes(time[4:], "big")
        if start > 192 * track.timescale:
            raise Mp4Error()
        total, count, previous_end = 0, 0, None
        for atom in atoms:
            if atom.kind != b"trun":
                if atom.kind not in {b"tfhd", b"tfdt"}:
                    raise Mp4Error()
                continue
            run = raw[atom.offset + atom.header : atom.end]
            run_flags = _u32(run) & 0xFFFFFF
            n = _u32(run, 4)
            if (
                run[0] not in {0, 1}
                or not 1 <= n <= MAX_SAMPLES
                or count + n > MAX_SAMPLES
                or run_flags & ~0xF05
            ):
                raise Mp4Error()
            cursor = 8
            if run_flags & 1:
                if cursor + 4 > len(run):
                    raise Mp4Error()
                offset = struct.unpack_from("!i", run, cursor)[0] + moof.offset
                cursor += 4
            elif previous_end is not None:
                offset = previous_end
            else:
                raise Mp4Error()
            first_flags = None
            if run_flags & 4:
                first_flags, cursor = _u32(run, cursor), cursor + 4
            for index in range(n):
                current_duration, current_size = duration, size
                current_flags = (
                    first_flags if first_flags is not None and index == 0 else sample_flags
                )
                if run_flags & 0x100:
                    current_duration, cursor = _u32(run, cursor), cursor + 4
                if run_flags & 0x200:
                    current_size, cursor = _u32(run, cursor), cursor + 4
                if run_flags & 0x400:
                    current_flags, cursor = _u32(run, cursor), cursor + 4
                if run_flags & 0x800:
                    if _u32(run, cursor):
                        raise Mp4Error()
                    cursor += 4
                if (
                    current_duration < 1
                    or current_size < 1
                    or offset < mdat.offset + mdat.header
                    or offset + current_size > mdat.end
                ):
                    raise Mp4Error()
                if count == 0 and track.kind == "video":
                    if current_flags & 0x10000:
                        raise Mp4Error()
                    payload = memoryview(data)[offset : offset + current_size]
                    pos, idr = 0, False
                    for _ in range(256):
                        if pos == len(payload):
                            break
                        length = _u32(payload, pos)
                        pos += 4
                        if not 1 <= length <= len(payload) - pos:
                            raise Mp4Error()
                        idr |= payload[pos] & 31 == 5
                        pos += length
                    if pos != len(payload) or not idr:
                        raise Mp4Error()
                ranges.append((offset, offset + current_size))
                offset += current_size
                total += current_duration
                count += 1
            previous_end = offset
            if cursor != len(run):
                raise Mp4Error()
        if not count or Fraction(total, track.timescale) > maximum_seconds:
            raise Mp4Error()
        tracks.append(TrackFragment(identifier, start, total, track.timescale, count))
    if sequence is None or sequence < 1 or len(tracks) != len(known):
        raise Mp4Error()
    cursor = mdat.offset + mdat.header
    for start, end in sorted(ranges):
        if start != cursor:
            raise Mp4Error()
        cursor = end
    if cursor != mdat.end:
        raise Mp4Error()
    return Fragment(sequence, tuple(tracks))


def inspect_init(data: bytes, *, audio: bool) -> Movie:
    try:
        return _inspect_init(data, audio=audio)
    except (H264Error, IndexError, KeyError, ValueError, TypeError, OverflowError, struct.error):
        raise Mp4Error() from None


def inspect_fragment(data: bytes, movie: Movie, *, maximum_seconds=Fraction(4)) -> Fragment:
    try:
        return _inspect_fragment(data, movie, maximum_seconds=maximum_seconds)
    except (H264Error, IndexError, KeyError, ValueError, TypeError, OverflowError, struct.error):
        raise Mp4Error() from None
