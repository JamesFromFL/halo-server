# R7f: codec reserve and a more efficient encoder preset

This slice retains the 82% average codec target, immediate CFR output, real source PTS and the R7c cadence estimator. For selected output through 1280x720 pixels, it changes x264's preset from `veryfast` to `fast` and replaces the half-target VBV size with the remaining selected one-second capacity. Above that tested pixel count, including the advertised 1080p30 catalog, both the original `veryfast` preset and half-target VBV remain unchanged. At a 132-kbps selection the target/maxrate is 108 kbps and VBV is 24 kbit; at 299 kbps they are 245 kbps and 54 kbit. The existing separate protected-wire policy, byte/count limits, privacy, encoder refresh, force-token ownership and two-second source-clock IDR policy are unchanged.

A target plus an additional half-target buffer permits more than the selected one-second capacity in a simple buffer/refill model. Reserving the difference avoids that obvious over-allocation, while the `fast` preset recovers detail lost by shrinking VBV alone. These parameters remain an empirical operating policy, not proof of an arbitrary rolling-window bound.

## Synthetic comparison

The same six-second moving gradient/checker/rectangle fixture was decoded independently, compared on original picture PTS, and packetized/encrypted for actual protected-byte accounting. Media existed only in RAM. Each case produced and decoded every real input picture with no pending frame.

| Source fps / output / selected kbps | Current PSNR / SSIM | Reserve + fast PSNR / SSIM | Candidate peak source-second bits |
|---|---:|---:|---:|
| 15 / 640x360 / 132 | 23.57 / .5545 | 24.73 / .5566 | 109184 |
| 15 / 1280x720 / 299 | 20.19 / .5464 | 21.66 / .5846 | 245432 |
| 30 / 640x360 / 132 | 22.66 / .5219 | 24.42 / .5492 | 113744 |
| 30 / 1280x720 / 299 | 16.61 / .4686 | 20.65 / .5657 | 251568 |

PSNR uses luminance mean squared error; SSIM is a non-overlapping 8x8 luminance calculation, not MS-SSIM. These results describe these pinned native synthetic cases, not image parity with Ring or decoded Apple playback. Tests retain startup-inclusive and complete-window checks, exact PTS, independent IDR decoding and the separate SRTP-byte assertion. The quality floors cover all four cases.

A 132-kbps transition seeded at nominal 13 then supplied actual 15-fps PTS. The corrective IDR window fell from 157880 to 124152 bits; the analogous nominal-18 case peaked at 109992 bits. Both retain explicit consecutive forced-IDR requests and every real picture. A minimum `vbv-init` only on refresh also contained these particular windows but reduced source-15 quality, so that alternative is not adopted.

The local single-core no-account benchmark measured roughly 10.5/19.6/7.9/13.9 ms mean managed feed time across the four cases; cold-start-inclusive maxima were 72–80 ms. A standalone direct-native benchmark additionally reports warm maxima, p95, throughput, exact-PTS counts and pending pictures under kernel-enforced 30-CPU-second and 120-wall-second limits. The target-server synthetic benchmark subsequently completed all four cases across two bounded runs: the first hit the 30-CPU-second cap during the fourth case after three successful cases; the isolated final 720p30 case then passed. All 540 pictures were independently decoded, with no PTS errors, empty feeds or pending picture. Server feed means were 11.21/24.11/8.55/17.15 ms; warm maxima were 23.21/49.04/16.31/22.89 ms. The initial capped run is retained as evidence, not called a four-case success. These are single-worker direct-native synthetic measurements through 720p, not multi-camera capacity, managed-process startup, the maximum advertised 1080p30 CPU case, or end-to-end latency. A separate true-1080p30 benchmark rejected global `fast`: local current `veryfast` encoded 52.38 fps (19.09-ms mean), while `fast` encoded only 21.52 fps (46.46-ms mean). Both preserved all 90 pictures, but fast did not keep pace with 30 fps. No server global-fast test or adoption followed that failure. A native compatibility regression requires byte-identical 1080p output against the frozen original encoder setup; it does not claim the old 1080p rate envelope is repaired.

## Remaining source-time envelope limitation

An actual source-rate rise from 15 to 30 fps can precede the sustained estimator's nominal-rate correction. In a 340-picture synthetic transition, reserve + fast preserved all pictures immediately but still peaked at 205856 bits against a 132000-bit source-second selection before correction. Corrected steady windows peaked at 107304 bits. The monitor records these real exceedances; this slice neither hides them nor drops pictures to force its counters to comply. The regression requires exact source-window accounting through this case rather than claiming universal compliance.

Native VFR is not enabled here: its one-picture pending tail remains incompatible with burst-arrival freshness. A work-only public-option probe with zero sync/RC lookahead still returned 119 outputs from 120 inputs. Explicit source-frame duration, with and without PyAV frame-duration flags, retained that same tail. The flag made output packet durations available, but did not change the pending-frame count; an 800-ms arrival quiet still aged the released old picture by about 800 ms. The x264 encoder adds a VFR delay, and the pinned FFmpeg wrapper carries frame duration as output metadata rather than as a native x264 per-picture duration override. No supported zero-tail VFR option was found for this wrapper. No backend, dependency, fake frame, timestamp rewrite, noise suppression or added playout reservoir is introduced.

## Primary implementation references

- [x264 public parameters](https://github.com/mirror/x264/blob/master/x264.h): separate VBV rate, buffer size and initial fill; reconfiguration supports only a subset of parameters.
- [x264 rate control](https://github.com/mirror/x264/blob/master/encoder/ratecontrol.c): finite buffer accounting/refill and possible underflow diagnostics; a parameter setting alone is not an arbitrary-window proof.
- [x264 encoder delay](https://github.com/mirror/x264/blob/master/encoder/encoder.c#L1505-L1514): enabling VFR adds one picture even with other lookahead disabled.
- [FFmpeg 8.1.1 libx264 wrapper](https://github.com/FFmpeg/FFmpeg/blob/n8.1.1/libavcodec/libx264.c): codec/packet data and runtime reconfiguration boundary.

Work evidence: `diagnosis/R7f-codec-window/{results.json,followup-results.json,cadence-transition.json}`. The independent CPU script is `benchmark.py` in that work directory; it accesses no Ring or HomeKit state and saves no media.
