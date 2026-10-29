# R7c: correct a persistent nominal encoder rate

The R7b physical metadata showed an average of 15.0086 source pictures per source second after the output encoder nominal changed from 18 to 13 fps. The same source-time interval contained 520 input and 520 output pictures. The old estimator's 20% deadband prevented a subsequent stable 15-fps estimate from correcting either 13 or 18. A direct no-media execution confirmed both wrong values persisted for 450 further 15-fps inputs.

This changes the estimator, without changing picture admission, output PTS, bitrate/VBV settings, codec/backend, frame freshness, or pacing. Every input remains eligible for immediate output; calibration does not retain a frame or invent one.

Startup still calibrates after at least eight intervals and half a source second. After that, a maximum of 128 integer source timestamps covers at least four source seconds before a rate correction is considered. One boundary sample is retained during pruning. A different rounded rate needs at least eight observations and two seconds of persistence; a one-fps correction needs four seconds. Every correction retains the existing five-source-second cooldown. Actual source gaps greater than one second clear estimation history and votes, while retaining the existing calibrated rate until fresh covered evidence exists. Arrival time and SPS-declared rate do not enter the estimate.

The larger window avoids letting a short irregular PTS window control a lasting rate. Longer one-fps voting rejects a 1.54-second 13-fps excursion within otherwise 15-fps source time. Unlike a percentage deadband, it still converges for stable 13, 14, 16, or 18 fps estimates followed by actual 15 fps. The current tests require that correction within eleven source seconds, with no intermediate refresh and no worker/decoder replacement.

The native encoder still refreshes only its encoder context when no returned packet origins are pending. It preserves the warm decoder, original source PTS/receipts, and force-token FIFO. `encode()` continues to force real IDRs on explicit requests, encoder refresh, and the two-second source-clock interval; `gop_size`/`keyint` are an additional encoder ceiling and are unchanged.

## Qualification and remaining rate-envelope issue

The focused tests cover fixed and irregular cadence, short excursions, full-window/gap boundaries, sustained 15-to-30 changes, immediate outputs, original receipts, source epoch/STOP ownership, decoder warmth, exact consecutive forced-IDR acknowledgments, and independent native decoding. The regular 640x360/132 and 1280x720/299 fixtures at source 15 and 30 fps continue to measure startup-inclusive and covered codec windows.

A separate synthetic 132-kbps moving-scene transition deliberately seeds the old 13-fps nominal and then supplies actual 15-fps PTS. The old estimator stays at 13 and peaks at 140704 bits in a source second. The corrected estimator reaches 15, but its corrective real IDR overlaps the older rate context and produces a transitional 157880-bit window. Subsequent covered windows peak at 123448 bits. The old 18-fps case stays at 18; the corrected case reaches 15 and peaks at 128208 bits in the same experiment. Explicit consecutive force requests remain present in the corrected steady-window check.

The complete transition-window overshoot is retained in the work evidence; it is not presented as codec-limit compliance or hidden by dropping pictures. R7c fixes persistent rate estimation only. The subsequent rate-envelope investigation must account for encoder refresh, startup, and forced-IDR overlap separately. Correct convergence does not prove a universal bitrate bound, visual parity with Ring, smooth Apple rendering, or an end-to-end latency guarantee.
