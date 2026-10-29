# Receiver-first paired pilot: acceptance failed

## Status

R6q (`e0af362493a3eef4f0d03019c600c5e13df9ed7d`) was deployed as managed
selection revision 13. Pairing and the exact HKSV-off preferences were preserved,
with R4d retained as the previous release. The owner reported:

> Stutters or audio interruptions remain

**Physical playback acceptance failed.** The implementation, package and synthetic
qualification gates passed; they did not establish continuous Apple Home playback
or near-Ring quality. No explicit answer about preview behavior was provided in
this pilot. Historical qualification counts remain in the
[implementation record](halo-server-receiver-implementation-2026-10-29.md).

The metadata-only capture ran from 2026-10-29 06:48:33 to 06:51:47 UTC. It contains
195 status samples, including 143 active samples and approximately 143 seconds of
live operation. The view ended through an explicit owner STOP. All measured
live, snapshot, bulk and upstream resource/cleanup counts settled to zero.

## Measured comparison with R4d

The earlier R4d capture contained 176.866 active seconds; R6 contained 142.996.
For video, the steady comparison below starts after 20 active seconds and uses
the common selected contract: 640×360, maximum 30 fps, high profile, level 4.0,
132 codec kbps. The measured subsets span 155.982 seconds for R4d and 122.004
seconds for R6. Scenes and motion were not controlled, and these are Halo access
units, not Apple decoded or displayed frame counts.

| Measurement | R4d | R6 |
| --- | ---: | ---: |
| Steady source access units per second | 14.97 | 14.85 |
| Steady sent access units per second | 14.76 | 13.74 |
| Steady sent/source access units | 98.63% | 92.49% |
| Steady stale output / dependency skips | 21 / 12 | 60 / 58 |
| Maximum complete-access-unit send duration | 471.2 ms | 87.9 ms |
| Audio sent/read duration, whole view | 172.46 / 175.26 s | 141.04 / 141.54 s |
| Audio sent/read samples | 98.40% | 99.65% |
| Native audio converter recreations | 84 | 0 |
| Audio stale / budget-rejected output | 0.24 / 0.38 s | 0 / 0 s |
| Guarded UDP RTP quiet runs ≥500 ms, per minute | 27.9 | 26.6 |
| Median / maximum guarded UDP RTP quiet run | 700 / 900 ms | 650 / 1,000 ms |
| Median quiet-run start spacing | 2 s | 2 s |
| Home video RR jitter, sampled median / maximum | 1,000 / 1,671 ms | 834 / 2,471 ms |
| Home audio RR jitter, sampled median / maximum | 191 / 339 ms | 182 / 901 ms |

Audio preservation improved substantially. The remaining 0.50-second
read-to-send difference consists of one 60-ms initial skip, one 60-ms unprocessed
packet, 40 ms submitted but not represented in generated output, and 340 ms of
generated output retired at STOP. Native pending samples are not directly
exposed, so the 40 ms is not assigned a more specific cause. There was no recorded
recurring converter recreation, stale output, overflow or budget rejection.

This is not continuous receiver delivery. Of 7,052 audio packets sent, 6,078
(86.2%) were classified as catch-up packets. The bounded sender can send these
20-ms packets at up to twice their nominal packet cadence, subject to its separate
byte budget. Media timestamps retain their original spacing. Maximum measured
pacing lateness was 978 ms. One source timestamp discontinuity added 10 ms; one
output/wire timestamp gap was 30 ms. That isolated gap does not explain repeated
interruptions at approximately two-second intervals.

Video egress serialization became faster, but video continuity did not improve
uniformly. Across the whole view, R6 recorded 70 stale outputs and 66 dependency
skips. After reconfiguration settled, these counters usually increased by one
at approximately two-second intervals. The original-receipt age reached 1.312
seconds. All eight upstream keyframe-request attempts occurred during startup
or reconfiguration, ending by approximately 15.36 active seconds; they do not
explain the later persistent pattern. A completed local request is not proof
that Ring supplied an IDR.

## Reproduced pending-picture mechanism

The live trace has no per-access-unit event association proving the exact cause
of each stale output. A separate native synthetic probe does reproduce a
relevant mechanism: x264's variable-rate path retains one picture, which is only
returned when the next input arrives. After an 800-ms input quiet period, that
picture's correct original receipt can already exceed Halo's 500-ms video
freshness policy. Rejecting it then requires dependency-safe recovery.

The private `diagnosis/receiver-immediate/results.json` probe used a known 15-fps
synthetic source, selected maximum 30 fps, 640×360 at 132 kbps, and an 800-ms
arrival gap every two seconds:

| Synthetic native path | Variable-rate selected-30 | Immediate constant-rate source-15 |
| --- | ---: | ---: |
| Source pictures | 90 | 90 |
| Outputs / maximum pending pictures | 89 / 1 | 90 / 0 |
| Modeled stale outputs / dependency skips | 3 / 32 | 0 / 0 |
| Modeled eligible pictures | 54 | 90 |
| Maximum output age | 814.7 ms | 55.3 ms |
| Startup and covered-one-second peak | 118.520 kbits | 125.464 kbits |

Every output was independently decoded. This was an actual native conversion
probe with paced synthetic arrivals; the dependency model was a counterfactual,
not the production sender or a Home receiver. Its source rate was known from the
fixture. The real Ring SPS declared 25 fps while observed source delivery was
approximately 15 access units per second, so simply trusting that SPS rate is
not a qualified production replacement. This probe supports investigating a
zero-pending path; it does not prove that such a path fixes the physical pilot.

## Ingress, clock, format and bitrate observations

Incoming bursts remain. R6 had 62 guarded UDP RTP quiet runs and 63 joint routed
audio/video quiet runs of at least 500 ms, with approximately two-second spacing.
The pattern persisted through successful decryption and encoded completion.
Measured ICE queue wait peaked at 11.699 ms, DTLS service at 19.603 ms and source
wakeup lag at 14.039 ms. Sampled daemon-owned UDP socket drop counts and observed
increments were zero. These callback measurements and intermittent socket
samples do not identify buffering before the callback or exclude unobserved
network/kernel drops. Earlier smooth Ring-app playback remains relevant
comparison evidence, not proof of an identical upstream path or latency.

The source parsed as 1280×720 high profile, level 5.0, with no reordered frames;
the final output parsed as 640×360 high profile, level 4.0, with variable-rate
timing. Home initially selected 1280×720 at 299 kbps, then reconfigured once to
640×360 at 132 kbps. The reason for the reduction is not established.

The live codec monitor recorded a **145,480-bit rolling one-second peak against
a selected 132,000-bit ceiling**, approximately 10.2% above it, with 104
over-limit observations in that configuration domain. The observations overlap;
they are not 104 distinct seconds. R6q's finite synthetic fixtures passed their
recorded ceilings, but this real result prevents a strict or universal bitrate
compliance claim. RTP/SRTP overhead and repairs are not included in that codec
measurement. The higher observed codec payload rate is also not an objective
picture-quality result.

The shared timeline remained fixed in explicitly receiver-estimated mode, with
zero source epoch changes. Late sender reports did not rebase it. Audio source
report candidates showed residuals up to 940 ms and were rejected. The 1,946
incoherent-candidate count can include repeated reads of the same rejected
snapshot; it is not 1,946 distinct bad reports. This observation does not prove
a remaining timeline-rebasing error.

Sampled Home receiver reports showed zero packet loss in R6, yet substantial
arrival jitter remained. No NACK/retransmission path was exercised. Receiver
report jitter uses RTP timestamps and arrival times, not sender-report NTP
correlation; correct sender reports alone cannot establish smooth playout.
The highest-sequence-advance metric includes an initial bootstrap bias and must
not be interpreted as a packet-loss total. Snapshot diagnostic silence does not
prove that all previews succeeded.

## Next assessment and boundaries

A zero-pending video path and an explicit shared audio/video playout reservoir
are under assessment, **not implemented**. They need controlled timing,
source-rate, quality, bitrate and ownership qualification before a new physical
pilot. A reservoir would be a deliberate bounded playout policy, unlike the
current retention limits; it must not silently weaken privacy, epoch, STOP,
count, byte or retirement guarantees. Source ingress behavior remains a
separate unresolved observation.

No audio denoising, gating, gain alteration, invented silence, repeated samples
or time stretching is introduced by this follow-up. HKSV remains off. No push,
backend replacement or new camera connection is part of this documentation.
The owner report remains the acceptance result; improved internal counters do
not override it.

Private evidence: `evidence/lifecycle-R6/lifecycle-20261029T064833Z.jsonl`, its
summary and sanitized events, `comparison-R4d-R6.json`, `comparison-R4d-R6.md`,
and `diagnosis/receiver-immediate/results.json`. The lifecycle SHA-256 is
`e260ecf4c8df531e387956b84385600b520380bdae71899ab84078597ed5113d`.
No real camera media was retained for this comparison.
