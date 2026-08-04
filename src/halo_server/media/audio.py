"""Normalize decoded Ring PCM into stable 20 ms mono Opus packets."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

import av
import numpy as np

OPUS_RATE = 48_000
OPUS_FRAME_SAMPLES = 960
OPUS_FRAME_DURATION_MS = 20
OPUS_BIT_RATE = 24_000


class AudioNormalizationError(RuntimeError):
    """Decoded audio could not be normalized safely."""


@dataclass(frozen=True)
class NormalizedOpusPacket:
    payload: bytes
    rtp_timestamp: int


class NormalizedOpusEncoder:
    """Downmix and resample S16LE PCM before fixed-duration Opus encoding."""

    def __init__(self) -> None:
        try:
            encoder = av.CodecContext.create("libopus", "w")
            encoder.sample_rate = OPUS_RATE
            encoder.layout = "mono"
            encoder.format = "s16"
            encoder.time_base = Fraction(1, OPUS_RATE)
            encoder.bit_rate = OPUS_BIT_RATE
            encoder.options = {
                "application": "audio",
                "frame_duration": str(OPUS_FRAME_DURATION_MS),
                "vbr": "off",
                "dtx": "off",
                "fec": "off",
            }
            encoder.open()
        except Exception as exc:
            raise AudioNormalizationError("Opus encoder is unavailable") from exc
        self._encoder = encoder
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=OPUS_RATE)
        self._fifo = av.AudioFifo()
        self._input_spec: tuple[int, int] | None = None
        self._input_samples = 0
        self._output_samples = 0

    def encode(
        self,
        payload: bytes,
        channels: int,
        rate: int,
    ) -> tuple[NormalizedOpusPacket, ...]:
        if not isinstance(payload, bytes) or not payload:
            raise AudioNormalizationError("PCM payload must be non-empty bytes")
        if channels not in {1, 2}:
            raise AudioNormalizationError("PCM channel count must be one or two")
        if isinstance(rate, bool) or not isinstance(rate, int) or not 8_000 <= rate <= 192_000:
            raise AudioNormalizationError("PCM sample rate is invalid")
        sample_stride = channels * 2
        if len(payload) % sample_stride:
            raise AudioNormalizationError("PCM payload is not aligned to complete samples")
        input_spec = (channels, rate)
        if self._input_spec is None:
            self._input_spec = input_spec
        elif input_spec != self._input_spec:
            raise AudioNormalizationError("PCM format changed during a media session")

        samples_per_channel = len(payload) // sample_stride
        try:
            samples = np.frombuffer(payload, dtype="<i2").reshape(1, -1)
            frame = av.AudioFrame.from_ndarray(
                samples,
                format="s16",
                layout="mono" if channels == 1 else "stereo",
            )
            frame.sample_rate = rate
            frame.pts = self._input_samples
            frame.time_base = Fraction(1, rate)
            self._input_samples += samples_per_channel
            for normalized in self._resampler.resample(frame):
                self._fifo.write(normalized)

            packets = []
            while self._fifo.samples >= OPUS_FRAME_SAMPLES:
                normalized = self._fifo.read(OPUS_FRAME_SAMPLES)
                normalized.pts = self._output_samples
                normalized.time_base = Fraction(1, OPUS_RATE)
                encoded_packets = self._encoder.encode(normalized)
                for encoded in encoded_packets:
                    payload_bytes = bytes(encoded)
                    if payload_bytes:
                        packets.append(
                            NormalizedOpusPacket(
                                payload=payload_bytes,
                                rtp_timestamp=self._output_samples,
                            )
                        )
                self._output_samples += OPUS_FRAME_SAMPLES
        except AudioNormalizationError:
            raise
        except Exception as exc:
            raise AudioNormalizationError("PCM audio normalization failed") from exc
        return tuple(packets)
