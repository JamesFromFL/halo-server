# Receiver-first implementation and qualification

## Scope and status

The receiver-contract research is now implemented in locally committed slices.
This document records implementation and synthetic evidence; it does not assert
that Apple Home playback matches Ring. The installed baseline for comparison is
R4d (`9c51a33`), not the later, undeployed R5/R6 encoder experiments. Deployment
and physical Home acceptance are separate gates.

| Slice | Change | Full Python / Node checks |
| --- | --- | --- |
| R6a | Original frame receipt, source epoch and configuration provenance; worker lifecycle fencing | 2,294 / 265 |
| R6p | Bounded SPS/PPS STAP-A aggregation and independent reconstruction/decode | 2,348 / 265 |
| R6t | Shared stable A/V timeline, same-source reconfiguration continuity and correlated sender reports | 2,381 / 265 |
| R6u | Bounded 1.2-second audio retention without an intentional playout delay | 2,388 / 265 |
| R6w | Separate codec-rate measurements and finite protected-packet transmission budget | 2,413 / 265 |
| R6e | Source-time video conversion, actual output ownership and bounded native pending frames | 2,426 / 265 |
| R6r | Asynchronous, bounded exact-ciphertext video repairs | 2,444 / 265 |
| R6k | Recovery requests through the current authorized Ring lease | 2,460 / 265 |

Each Python run also had one host IPv6 skip. Each slice passed lint, formatting,
adapter compilation, dependency-lock verification and package construction before
its local commit. R6q adds the final encoder policy below and stricter regression
checks; its verification receipt is recorded separately when the gate completes.

## Final encoder policy, R6q

Use source timestamps with `force-cfr=0`, an 82 percent codec target and a VBV
buffer of half that rate in kilobits. Home's selected frame rate remains a
maximum. No pictures are duplicated to fill it. The existing two-second source
GOP and bounded recovery requests remain unchanged.

The earlier R6e 90-percent/250-ms proposal improved on an intermediate encoder
but scored worse than the actually installed R4d on these 15-fps scenes. That
comparison did not justify describing it as a deployed-baseline quality gain.
The final policy follows an additional native matrix, including startup bytes
and every covered one-second source-time window.

All fixtures contain six seconds of synthetic 720p gradient/checker motion and a
moving colored rectangle. Selected frame rate is 30 fps. Every output is
independently decoded, with quality compared at common source timestamps. The
table reports aggregate luma PSNR and non-overlapping 8-by-8 luma SSIM. It is not
a perceptual rating or an Apple playback result.

| Source fps / selected output | Installed R4d to R6q PSNR, dB | Installed R4d to R6q SSIM | R6q peak complete window, kbps / startup, kbits |
| --- | --- | --- | --- |
| 15 / 640×360, 132 kbps | 23.662 → 23.726 | 0.55568 → 0.55191 | 118.520 / 103.592 |
| 15 / 1280×720, 299 kbps | 19.959 → 21.132 | 0.53986 → 0.56053 | 279.776 / 260.784 |
| 30 / 640×360, 132 kbps | 23.345 → 21.934 | 0.55111 → 0.50673 | 123.800 / 129.728 |
| 30 / 1280×720, 299 kbps | 18.356 → 16.612 | 0.50718 → 0.46882 | 277.648 / 276.848 |

R4d exceeds the selected bitrate in both 30-fps fixtures: complete-window peaks
are 160.536 and 358.928 kbps. Its higher quality there is not quality at the same
compliant budget. At the observed camera cadence of approximately 15 fps, the
new policy improves aggregate 720p detail. The 640p SSIM decreases slightly.
Some individual-frame and keyframe scores also decrease. This is a measured
compromise, not a claim of universal superiority or an optimal encoder.

Six other new target/buffer variants were considered. None passed both startup
and covered-window ceilings in all four fixtures. The earlier 90-percent/250-ms
policy also passed the ceilings but had lower aggregate 15-fps quality. A
startup partial window already exceeding the one-second bit allowance proves
an overshoot; excluding startup from qualification would conceal that failure.
The final candidate's protected transmission is additionally qualified through
the actual manager with original receipt times, one native pending frame,
complete-AU reservation, encryption and independent decoding.

ABR/VBV settings and finite synthetic scenes do not guarantee every future
one-second window. The source-time codec monitor remains necessary. Codec bytes
are measured separately from RTP/SRTP and repair bytes. The finite wire peak is
`min(64 Mbps, max(2 Mbps, 4 × selected codec rate))`; it is a local transmission
policy, not an estimate of available network bandwidth. Original 500-ms new-AU
deadlines, whole-AU admission and bounded queues remain enforced.

## Audio, recovery and timing behavior

Audio retention increases from 500 ms to 1.2 seconds, within unchanged packet and
byte caps. This does not deliberately wait before playback. It lets bounded
native conversion survive representative incoming bursts without repeated
codec recreation and timeline breaks. Synthetic native replay reduced six
recreations and six timestamp discontinuities to zero; it did not remove quiet
periods already present at ingress. Privacy, source-epoch changes and STOP still
fence output immediately. No denoising, gain control, gating, artificial silence,
time stretching or background filtering was added.

Sender reports now correlate both tracks at one wall/monotonic instant. Source
reports are used only when a coherent initial pair is available; otherwise the
timeline is explicitly receipt-estimated and does not silently rebase later.
Correct sender reports alone do not explain or eliminate arrival jitter.

The native video encoder retains one picture in the measured live path. Actual
output timestamps and original receipts follow that picture rather than the
input that released it. At 15 fps this is approximately 67 ms of real latency.
The final pending picture is discarded at STOP; no fake output or live flush is
used to hide it.

Repair requests no longer block the RTCP reader behind packet pacing. Repairs
reuse exact ciphertext with a one-second original-send expiry, bounded request
and send budgets, and scope/epoch checks before transmission. Current-lease
keyframe requests use the existing Ring event loop with bounded ownership and
coalescing. Success means a local request was attempted; it does not establish
that Ring delivered the requested keyframe.

## Release and physical acceptance

Build the committed source twice offline and require identical artifacts. Run
installed-package native encode/decode, public HAP callback, STAP-A, stable-clock,
original-receipt and force-keyframe ownership checks against that artifact.
Deployment must preserve the paired accessory identity, exact HKSV-off settings
and the previous R4d release as a rollback option. Never substitute build-tree
success for installed-package qualification.

Capture one fresh lifecycle: closed baseline, open, at least two minutes live,
then closed and settled. Collect bounded metadata only, with no camera media or
credentials. Compare received/sent media continuity, parsed versus selected
format, codec windows, expiry/dependency drops, repairs, feedback, resource
cleanup, previews and the user's observed picture/sound together. Include local
and remote Home use, reconfiguration and reconnection in subsequent acceptance.

Remaining limitations are explicit:

- The cause of the approximately two-second pattern of incoming quiet periods
  is unresolved. These receiver-facing fixes may not remove all visible pauses.
- Home selected 299 kbps at 720p and then 132 kbps at 360p in the prior pilot.
  The reason for that reduction is unknown. Halo cannot preserve Ring's higher
  bitrate/resolution while simultaneously honoring a lower negotiated format.
- The earlier optional decode-only backlog/GOP warmup optimization is not
  implemented. Old input can still reach conversion before stale output is
  rejected. It is not silently counted as completed receiver work.
- No signaling/backend replacement or new HEVC/multitier HomeKit mode was added.
- Actual Apple playback, near-Ring parity and HKSV are not established by the
  synthetic tests. HKSV remains off for this live-stream investigation.

The private repair workspace retains the reproducible encoder matrix under
`diagnosis/receiver-encoder/policy-matrix/`, including the exact installed
baseline, candidate hashes and numeric results. Its consolidated receipt is
`2b3947b9f584f2cbce9dfe9aedddb9978c5d6f40837e16e0e69b312a077f2270`.
Only synthetic pictures were decoded for this comparison, and none were saved.
