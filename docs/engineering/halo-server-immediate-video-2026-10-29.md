# R7a: immediate native video after the failed R6q pilot

The R6q paired pilot still stuttered. Audio became clear between interruptions
and previews refreshed successfully, according to the user. The live metadata
showed zero audio converter recreations, but a video expiry regression near the
recurring incoming quiet periods. See the [paired pilot record](halo-server-receiver-pilot-2026-10-29.md).

## Mechanism and correction

The prior `force-cfr=0` choice adds a mandatory pending picture in this x264
backend. The last picture before a long arrival gap waits for the next input,
then correctly fails Halo's original 500 ms freshness limit. Subsequent dependent
pictures require recovery. This is additional damage beyond the incoming gap.
[x264's initialization](https://github.com/mirror/x264/blob/master/encoder/encoder.c#L1505-L1515)
adds VFR input to its delay even with B-frames and lookahead disabled.

R7a restores immediate CFR rate control while preserving every real source PTS
and the existing maximum-frame-rate admission policy. It does not duplicate
pictures, change the source timestamp clock, relabel receipts, flush a live
encoder, increase freshness limits or add a playout buffer.

The encoder starts at the selected maximum and calibrates its nominal rate from
accepted decoded source PTS. At most 32 timestamps are retained. Initial
calibration requires eight intervals and at least half a second of source time.
The rounded estimate is clamped to the selected maximum. Later changes require
at least a 20 percent difference, eight consistent estimates over half a second,
and five seconds since the last calibration. A source-PTS gap over one second
clears the estimation history; a wall-clock arrival gap does not. The camera's
SPS declares 25 fps despite an observed rate near 15, so SPS and arrival timing
are deliberately excluded from this estimate.

Calibration replaces only the encoder context, keeping the decoder warm. It
waits until native output-origin metadata is empty, preserves pending force
tokens, and emits the current real picture as an IDR. The old and replacement
contexts exist only as a bounded pair inside the existing owned worker. The
new context preserves source color fields. Unrequested pictures clear inherited
source picture-type hints, avoiding extra IDRs from two independent schedules.
The actual output SPS and source PTS remain independently checked.

Updating only PyAV's framerate property after codec opening is not a qualified
alternative: [the pinned FFmpeg wrapper](https://github.com/FFmpeg/FFmpeg/blob/n8.1.1/libavcodec/libx264.c#L184-L230)
does not update FPS in its per-frame reconfiguration path.

## Evidence and limits

An actual-paced synthetic 15-fps experiment with 800 ms arrival gaps every two
seconds reproduced three stale outputs and a pending tail in the prior VFR
worker. The immediate nominal-source-rate counterpart emitted all 90 real
pictures, with zero pending or stale outputs and exact original timestamps.
That early dependency simulation was not the production manager; the R7a sender
tests additionally exercise the actual manager, complete-AU admission, SRTP,
original receipts, recurring input gaps and independent native decoding.

The focused native gate passed 125 tests, covering false SPS25 with actual
15/30-fps PTS, irregular timestamps, cadence transitions, decoder-delayed output,
force-token ownership, source epochs, actual-paced quiet periods, color/SAR,
startup-inclusive bitrate windows and worker retirement. The complete suite and
installed-package qualification have their own frozen receipts.

The final 82-percent target/500-ms VBV remains an empirical codec policy. All four
representative startup/covered-window fixtures fit the selected limits. It is
not a universal maximum-rate guarantee; the R6q real pilot already demonstrated
that a synthetic pass cannot establish that claim.

| Synthetic source / selection | Installed R4d → R7a luma PSNR | Installed R4d → R7a luma SSIM |
| --- | --- | --- |
| 15 fps / 360p, 132 kbps | 23.662 → 23.569 dB | 0.55568 → 0.55444 |
| 15 fps / 720p, 299 kbps | 19.959 → 20.169 dB | 0.53986 → 0.54603 |
| 30 fps / 360p, 132 kbps | 23.345 → 22.664 dB | 0.55111 → 0.52194 |
| 30 fps / 720p, 299 kbps | 18.356 → 16.612 dB | 0.50718 → 0.46882 |

The installed R4d 30-fps fixtures exceeded the negotiated bitrate. At 15 fps,
R7a has small aggregate gains at 720p and small declines at 360p against R4d.
R7a's 720p score also falls below R6q's 21.132 dB: immediate output and the
bounded calibration schedule carry a quality tradeoff. The demonstrated target
of this correction is avoiding added frame loss, not a universal quality gain.

## Current user direction

The user explicitly chose minimum delay and investigation of Ring's delivery
path before a shared 1.2-second buffer. No such reservoir is enabled or added in
this slice. The recurring upstream gaps remain unresolved; immediate native
output alone cannot smooth periods in which no media arrives. Continue tracing
the receive path, while preserving clear unfiltered audio, working previews,
pairing and HKSV-off settings. Commit locally, without pushing.
