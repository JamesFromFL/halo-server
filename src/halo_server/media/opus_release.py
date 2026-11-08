"""Bounded release of complete Opus packets after the existing audio prefetch.

This owns no timer, decoder, socket or extra payload queue. RTP ordering and
overflow remain in the pinned sixteen-slot aiortc buffer; the existing native
audio sender owns pacing after packets are admitted.
"""

from __future__ import annotations

import time

from aiortc.jitterbuffer import MAX_MISORDER, JitterBuffer, JitterFrame

from halo_server.media.opus import OpusError, parse_opus


class CompleteOpusBuffer(JitterBuffer):
    def __init__(self, receiver, *, clock=time.monotonic):
        super().__init__(capacity=16, prefetch=4)
        self._receiver = receiver
        self._clock = clock
        self._started = False
        self._opus = False
        self._payload_type = None

    def add(self, packet):
        if packet.payload_type != self._payload_type:
            # Never relabel buffered payload from another negotiated codec.
            # A codec transition starts a fresh, bounded prefetch boundary.
            self._packets = [None] * self.capacity
            self._origin = None
            self._started = False
            self._payload_type = packet.payload_type
        codec = self._receiver._RTCRtpReceiver__codecs.get(packet.payload_type)
        self._opus = codec is not None and codec.mimeType.lower() == "audio/opus"
        if self._opus:
            try:
                parse_opus(packet.payload)
            except OpusError:
                return False, None
            # A retransmitted duplicate must not refresh the original receipt.
            previous = self._packets[packet.sequence_number % self.capacity]
            if previous is not None and previous.sequence_number == packet.sequence_number:
                return False, None
            packet._halo_received_at = self._clock()
            if self._origin is not None:
                delta = (packet.sequence_number - self._origin) % 65536
                misorder = (self._origin - packet.sequence_number) % 65536
                if MAX_MISORDER <= misorder < delta:
                    self._started = False
        return super().add(packet)

    def _remove_frame(self, sequence_number):
        if not self._opus:
            return super()._remove_frame(sequence_number)
        if self._started:
            return self.take()
        first = self._packets[self._origin % self.capacity]
        frame = super()._remove_frame(sequence_number)
        if frame is not None and first is not None and frame.data == first.payload:
            self._started = True
            frame.received_at = first._halo_received_at
            frame.startup_at = self._clock()
            # This is a description of already-retained contiguous samples,
            # never permission to enlarge downstream queues or stale limits.
            frame.startup_samples = parse_opus(
                first.payload
            ).duration_samples + self._contiguous_samples(3)
        return frame

    def _contiguous_samples(self, count):
        samples = 0
        for offset in range(count):
            packet = self._packets[(self._origin + offset) % self.capacity]
            if packet is None:
                break
            samples += parse_opus(packet.payload).duration_samples
        return samples

    def take(self):
        if not self._started or not self._opus or self._origin is None:
            return None
        packet = self._packets[self._origin % self.capacity]
        if packet is None:
            return None
        frame = JitterFrame(bytes(packet.payload), packet.timestamp)
        frame.received_at = packet._halo_received_at
        self.remove(1)
        return frame

    def drain(self, codec):
        """At most three extra packets: four source packets per receiver call."""
        if codec.mimeType.lower() != "audio/opus":
            return
        mapper = self._receiver._RTCRtpReceiver__timestamp_mapper
        for _ in range(3):
            frame = self.take()
            if frame is None:
                return
            frame.timestamp = mapper.map(frame.timestamp)
            yield frame


def install_complete_opus_buffer(receiver, *, clock=time.monotonic):
    import aiortc

    original = receiver._RTCRtpReceiver__jitter_buffer
    if (
        aiortc.__version__ != "1.15.0"
        or type(original) is not JitterBuffer
        or original.capacity != 16
        or original._prefetch != 4
        or original._origin is not None
    ):
        raise RuntimeError("Complete Opus release requires the unused pinned audio receiver")
    buffer = CompleteOpusBuffer(receiver, clock=clock)
    receiver._RTCRtpReceiver__jitter_buffer = buffer
    return buffer
