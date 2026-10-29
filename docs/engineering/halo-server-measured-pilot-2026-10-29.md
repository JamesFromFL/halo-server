# R7b pilot: measured findings, excluding subjective feedback

This assessment uses capture and code evidence only. It does not use the owner's
description of picture, sound or preview appearance to decide whether playback
improved. The 2026-10-29 capture ran from 07:56:07 to 07:59:54 UTC, including
174 active one-second samples, startup and cleanup. There were 228 lifecycle
samples, 2,255 external queue samples and no lifecycle collection errors.

Installed source: `e4269317818b623a501c41aab4554e5128fb41b1`.
Release: `4776c867c1738481b386cdabffcf74ab0febe4360614235bc5502fc05ca9afd5`.
R7a and R7b remain locally committed; nothing was pushed. No additional
playback reservoir was implemented.

## Video delivery improved, but is not complete source preservation

| Measurement | R6 pilot | R7b pilot |
| --- | ---: | ---: |
| Source access units / sent access units | 2,101 / 1,891 | 2,555 / 2,534 |
| Sent as percentage of source inputs | 90.005% | 99.178% |
| Stale outputs / dependent-picture skips | 70 / 66 | 0 / 0 |
| Maximum original-source age at output admission | 1,312.1 ms | 150.1 ms |
| Largest output / source timestamp gap | 2.894 / 1.141 s | 1.210 / 1.210 s |

Every one of the 2,534 produced R7b video outputs was sent. Partial access units,
deadline rejections and non-increasing output timestamps were zero. The remaining
21 source inputs without counted outputs cannot be partitioned conclusively
between decoder startup, configuration changes and other conversion boundaries
from this telemetry. Do not call this 100% source preservation. Output admission
age is not camera-to-screen latency or Apple decoder/rendering evidence.

## Incoming periodic delivery gaps remain

After startup exclusion, guarded UDP callback quiet runs of at least 500 ms
occurred 69 times over 169.1 seconds: 24.48 per minute, median 700 ms, maximum
1 second. Their median spacing was about two seconds. R6 measured 26.82 per
minute, median 650 ms and maximum 1 second. This is substantially the same
incoming burst pattern, already present before HomeKit conversion.

All 172 source-pipeline snapshots confirmed coverage of the currently selected
transport: one shared DTLS transport, six local protocols and one nominated
pair, with current hook ownership. The Ring loop's maximum measured wake lag
was 15.315 ms. Its wake-count/time accounting leaves only about 924 ms for all
unmeasured observer work across the interval, which disfavors a repeated
650 ms local receiving-loop stall.

Among 1,716 active external UDP queue samples, 167 were nonzero, with a maximum
169,728 bytes and at most three consecutive nonzero samples. In conservatively
trimmed quiet-period interiors, 128 of 130 samples were zero and two were only
960 bytes. No sampled current drops or drop increments appeared. These are
aggregate daemon-owned sockets, not Ring-only attribution; the sampler cannot
prove packet arrival times, residence times, between-sample behavior or a
lossless path. It does not support a sustained large local queue during most
measured quiet intervals.

## Ring confirms upstream transcoding; its trigger remains unresolved

One actual Ring report said `transcoding=true`, `reason=codec_mismatch`.
The observer was installed before the signaling reader; there were no invalid
or oversized diagnostic messages. The same single report appeared in 172
status snapshots; those are not 172 independent reports.

Existing sanitized signaling logs show the answer already selected H.264 High
profile/level `640032` and Opus audio. The reason does not identify the track
or the camera-to-cloud conversion responsible. Packetization mode was not in
the saved codec summary. Codec preference alone is therefore a weak next fix:
putting the already-selected High capability first does not establish that
Ring will stop transcoding or deliver packets more regularly. An offline
preference-only preflight passed 11 tests, but was not integrated or deployed.

## A remaining local cadence/rate-control bug is visible

The output's nominal H.264 cadence changed from 18 fps to 13 fps near active
second 138. The actual source cadence did not sustain that change:

- While nominally 18 fps, measured source cadence was 14.9511 fps over about
  126 seconds; the comparable preceding 32 seconds averaged 14.7505 fps.
- After the nominal switch to 13 fps, source and output timestamps both measured
  15.0086 fps over 34.65 seconds; the last 20 seconds averaged 15.0128 fps.

The current `RateCadence` 20% change threshold explains why an inaccurate
estimate can persist. A measured return from 13 to 15 fps is below that threshold,
so votes are cleared instead of correcting the encoder's nominal rate. The
same issue can retain 18 when the measured rate is 15.

The first bitrate-window exceedances appeared near second 140, shortly after
the nominal change. The final selected limit was 132,000 bits/second, but the
largest measured one-second codec window contained 156,320 bits: 18.42% over.
141 of 2,427 complete sliding windows exceeded the selection. These windows
overlap. The timing and threshold defect make nominal cadence a concrete local
rate-control contributor to test, not proof that it alone caused every overrun
or the incoming periodic gaps.

## Audio and lifecycle findings

Halo read 171.24 seconds of audio and sent 171.16 seconds, or 99.953%. No audio
expired or was rejected by the output budget; there were no converter recreations,
timestamp/RTP-step errors or source timestamp gaps in the terminal audio ledger.
Three queued 20 ms packets retired at STOP and the 20 ms native tail account for
the 80 ms difference. Nevertheless, 7,414 of 8,558 sent audio packets used catch-up
pacing; maximum pacing lateness was about 1.02 seconds. Preserving audio samples
does not establish regular delivery or uninterrupted Apple playback.

Home receiver reports ended at three cumulative video packets lost and six
audio packets lost. The final report intervals indicated zero fractional loss.
These reports do not certify a lossless session or directly reveal rendered
frames and sound.

One snapshot request failed just before live startup. The diagnostic reported
HTTP 200 with an empty snapshot timestamp source, followed by
`snapshot-source-failed`. This is independent evidence of an intermittent
preview-source failure, regardless of what a later preview looked like.

Live view ended by normal STOP with no cleanup failure. All live/upstream/bulk
resources were zero for about 25 seconds. The generic summary's false
`clean_stop_resources` flag came from a new snapshot request in its final sample;
a subsequent read-only check confirmed zero snapshot resources as well. The
service remained active, with zero automatic restarts and unchanged HKSV-off
preferences.

## Recommended next work

Correct and regression-test the nominal cadence estimator's persistent-error
case while retaining immediate output, genuine timestamps and receipt ownership.
Separately identify Ring's transcoding trigger, including actual packetization
mode and track-specific negotiated parameters, before changing negotiation.
Do not treat codec-order changes, backend replacement or added buffering as a
proven fix. Physical Apple rendering remains outside the capture's visibility.

Detailed evidence is retained under `evidence/lifecycle-R7b` and
`diagnosis/R7b-measured-analysis` in the private repair workspace. No real camera
video or audio was recorded for this assessment.
