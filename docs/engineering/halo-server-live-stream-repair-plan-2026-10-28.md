# Halo Server live-stream repair plan

Research completed October 28, 2026. **Proposed implementation only.**
Application baseline: `66770868639d88210519f1c14e971e0df06b0068`, clean `main`,
18 commits ahead of `origin/main`. Last verified installed release: `ab62415a…`.
Research used source review, primary protocol/implementation sources and
isolated synthetic experiments. No application edits, commits, pushes,
deployment, service restart, account access or real camera media capture occurred.

The recommendation is **seven implementation slices within the current stack**:
Python, aiortc, PyAV/libx264/reference libopus and the existing HAP adapter.
The main design changes are a bounded audio conversion/send pipeline, persistent
video reference recovery and a consistent audio/video timeline. A replacement
backend, another HAP fork, hardware encoder dependency or Scrypted service is
not justified by the evidence.

## Intended capabilities and limits

The repair should preserve fresh ambient sound, recover from short source
bursts without converter-reset storms, maintain useful video cadence and IDR
recovery, and show where delay or loss originates. Video should use encoded
copy when the actual source satisfies Home's selected contract, and convert
only when necessary. All choices should be automatic; user FFmpeg arguments
are not part of the design.

Preserve pairing, source ownership, microphone/privacy fences, native process
limits and recording history. HKSV remains off during live qualification.
Keep the existing 500 ms local media delivery-age policy initially. No default
noise suppression, gate, AGC, AEC, speech classifier, silence filling, sample
repetition, time stretching or hidden bandwidth reduction is proposed.

No implementation can restore source packets that never arrive, manufacture
higher-resolution detail, force Apple to choose a particular mode, or guarantee
perfect playback over every network. Physical Apple playback remains a required
qualification step, distinct from localhost tests and capability readiness.

## Evidence and decisions

The actual 216-second session observed 211.32 seconds of original audio,
submitted 94.38 seconds to conversion and emitted 86.20 seconds. It recreated
conversion 161 times and had 179 transmitted timestamp discontinuities. The
owner also reported video freezing or reloading despite an ordinary Home STOP
and no sender exception. These failures remain unresolved in the installed
release. See the [actual live audit](halo-server-live-stream-audit-2026-10-28.md).

| Finding | Research result | Decision |
| --- | --- | --- |
| Audio recovery discards fresh batches | Real native 600 ms burst counterexample: 19 recreations and 37 packets in 12 s, with fast conversion and a responsive loop | Separate expired output disposal from source reset; preserve valid fresh input |
| Conversion and output pacing are serialized | Three 20 ms outputs consume about 40 ms of pacing inside each 60 ms feed cycle | Use separate, bounded conversion and egress owners |
| Pending native audio is larger than lookahead alone | Native representative boundary retained 1,080 timeline samples at 48 kHz, or 22.5 ms | Qualify conservative logical sample attribution; do not hard-code a 120-sample tail |
| Shared lock can block source-clock/authority work | Injected 120 ms legacy encode wait blocked the actual source-clock method about 120 ms | Measure real waits; isolate native encoding from camera state locking |
| Video cadence filter is too strict | Native 30 fps input with alternating 2,999/3,001 ticks produced 30 outputs from 60 frames; exact 3,000 ticks produced 60 | Replace greedy interval filtering with phase-based source-time selection |
| Keyframe timing can stretch | Long-GOP native test extended output IDR spacing from 2 s to 4 s when output rate halved | Base the IDR deadline on source time and verify actual IDR NALs |
| Recovery can resume dependent video too early | Actual sender-state probe sent a second P frame before any recovery IDR | Keep an IDR-required fence until a complete decodable IDR is delivered |
| Video can cross its freshness bound during pacing | Actual sender/pacer admitted an IDR at age 490 ms and finished at 573 ms | Pre-admit whole access units and enforce their completion deadline |
| Timing correlations can change inconsistently | Same-epoch reconfigure probe compressed 1.967 s of source time; no-SR conversion reused one receipt for three output times | Preserve one stable source-to-output mapping and correct per-output timing |
| Diagnostics cannot locate video freezes | Ongoing frame cadence, detailed receiver reports and rendered A/V timing are missing | Add bounded measurements before assigning the real freeze to a particular stage |

These counterexamples prove conditional code weaknesses. They do **not** prove
that the Ring camera used the injected arrival/cadence pattern or that any one
weakness caused its observed video freezes. Regular native VBR audio with
concurrent video conversion passed a 30-second control with 1,478 packets and
no resets or stale drops.

The previous idea of a universal 2.5 ms pending tail is superseded: codec
pre-skip, resampling, FIFO quantization and unfinished output contribute
different amounts. The installed encoder's public `delay` value is zero;
validated public Opus header metadata, actual PTS and FIFO progress are useful
inputs. No new ctypes/libopus binding is needed for that research.

## Seven implementation slices

Each slice gets a focused regression gate and a local commit after approval.
The first slice establishes actual camera measurements before behavioral
choices are finalized. That is a decision gate, not a claim that the current
freeze cause is already known. No GitHub push is included.

| Slice | Work and primary locations | Required outcome before progressing |
| --- | --- | --- |
| 1. Bounded stream diagnostics | `media/ring_media.py`, `session.py`, `homekit_audio.py`, `homekit_live.py`, `rtp.py`; small private telemetry helper; existing bridge/service status | Distinguish ingress, admission, conversion, queues, transmission and receiver reports; capture a real baseline without changing playback policy |
| 2. Audio recovery and sending | `media/homekit_audio.py`, `homekit_audio_codec.py`, `audio_worker.py`, audio ownership in `homekit_live.py` | No whole-fresh-batch loss or repeated resets on ordinary gaps; finite output queue; preserved provenance, RTP and privacy fences |
| 3. Shared-state lock isolation | `media/session.py`, narrow consumer/backend integration if required | Slow legacy normalization cannot hold the camera state lock; late work cannot publish into or fail a newer source generation |
| 4. Video cadence and IDR timing | `media/transcode_worker.py`, `transcode.py` | Preserve valid near-rate input, real source time and a bounded source-time keyframe interval without inventing frames |
| 5. Video delivery and loss recovery | `media/homekit_live.py`, `rtp.py`, `transcode.py`/worker; narrow current-source PLI seam through controller/backend/receiver | Whole-unit deadlines, persistent IDR recovery, bounded retransmission, and recovery from transient infeasible units |
| 6. Quality, synchronization and reconfigure | Private view timeline helper; `homekit_live.py`, `homekit_audio.py`, codec decision model; adapter `video.ts`/`camera.ts` only where needed | Exact requested/applied quality, stable media timing across reconfigure, correct SR correlation and explainable copy/adapt decisions |
| 7. Installed and real-device qualification | Existing build/release tools and native/public-HAP tests, runbook and engineering notes | Reproducible installed artifacts plus sustained Apple playback; separately report LAN, remote and untested combinations |

### 1. Measurements that can distinguish the causes

Use constant-space counters, fixed histograms and bounded snapshot retention
through the existing trusted local status path. Do not perform synchronous log
I/O inside encoded-frame, authorization or UDP hot paths. Diagnostic overflow
or formatting/sink failure must not change delivery or cleanup authority.

Measure encoded-frame callback entry **before** source-clock/session-lock
access, admission completion and consumer read separately. The current receipt
is assigned after locking and can hide earlier wait. Keep these observations
separate without silently changing what `received_at` or freshness means.
Encoded-frame completion after the jitter buffer also differs from raw network
arrival; obtain pinned receiver statistics as a separate bounded upstream view.

Audio measurements: source arrival/PTS deltas, batch size, read wait, feed time
sum/histogram/max, native pending duration, retirement time, queue duration and
bytes, actual send lateness, authority/clock wait, and every discarded duration
by fixed reason. Provide a duration ledger so source, submitted, generated,
queued, discarded and emitted audio reconcile, including initial/terminal tails.

Video measurements: source and output complete AUs/PTS, selected/output FPS,
rate-filter/stale/dependency drops, IDR interval, encoder/queue/send time,
first-to-last datagram duration, payload/protected bytes, and unique versus
retransmitted packets. Capture START/RECONFIGURE requests and actual applied
configuration separately. Keep source Ring metrics separate from Home feedback.

Retain authenticated current-media receiver-report loss/jitter fields and
bounded RTT estimates. Interpret signed cumulative loss, missing/stale reports,
clock units and sequence rollover correctly. RTT requires a matching owned
recent SR plus valid LSR/DLSR; an empty RR provides no loss or RTT measurement.
Feedback is not proof of Apple's frame rendering. RTP report field semantics
come from [RFC 3550 §6.4](https://www.rfc-editor.org/rfc/rfc3550#section-6.4).

Use fixed relative timing/media properties and reason codes. Exclude payloads,
PCM/images, SDP, keys/salts, credentials, addresses, raw exceptions and raw
synchronization identifiers from exported diagnostics. Bound active/completed
view snapshots and correlation history; preserve structural RTCP input limits.

### 2. Audio: recover content rather than restart on every old tail

One conversion owner submits native requests in order. One send owner drains
complete output packets from a queue capped by packet count, bytes and age.
The proposed experimental caps are 32 packets and 40 KiB, further constrained
by the existing per-packet limits. Each entry carries its exact scope/epoch,
media PTS/duration and conservative delivery deadline. STOP or permission/source
change fences delivery immediately and joins both owners before release.

For same-epoch continuous source time, retain the worker across ordinary idle.
Inspect every output; discard only packets with expired logical source
contributors, advance consumed attribution correctly, and examine later fresh
outputs. Never erase an expired receipt while allowing its buffered PCM to
escape. Codec predictive state and logical sample-window attribution are
separate concerns; privacy/epoch transitions always retire the old codec state.

Keep-worker recovery is accepted only after conservative resampler/FIFO/encoder
attribution is demonstrated. If it cannot be established, the safe fallback
owns one reset and rechecks/reuses the current fresh batch after retirement.
The actual source-discontinuity reset and outer recovery must have one owner.
No blind fresh-batch discard, repeated cold starts, synthetic flush padding,
repeated samples or undisclosed silence filling is permitted.

Compare ordinary 50 packet/s delivery at 20 ms with a bounded temporary
100 packet/s catch-up candidate, both with at most three packets of credit and
an independent byte budget. These are **qualification candidates**, not chosen
production values. RTP timestamps still advance 480 ticks per 20 ms for the
measured classic-HAP 24 kHz request. Sending queued packets sooner does not
speed up their encoded media timeline; Apple controls downstream playout.
The compared [Scrypted classic sender](https://github.com/koush/scrypted/blob/50b8668ea8d36f281d5ab6d152cd47f3d45e57f5/plugins/homekit/src/types/camera/camera-streaming-srtp-sender.ts)
sends groups immediately, which motivates testing but does not qualify Halo's
policy on Apple receivers.

A strict real-time sender needs about 580 ms between the first and last of
30 packets representing a 600 ms burst. It cannot deliver them all under a
500 ms receipt deadline. Bounded catch-up can reduce transport backlog; it
cannot undo upstream delay, preserve genuinely expired contributors or prove
continuous Apple playback. A scheduling model still dropped 38 old-contributor
packets at the faster candidate rate. If actual arrivals require a larger
latency allowance, document that tradeoff for the owner before changing policy.

A native two-batch probe retained 58 valid packets with reset/reuse, or 57 with
keep-worker plus stale-output disposal. Neither prototype is an implemented
repair or a real-time Apple test. The [Opus API](https://opus-codec.org/docs/opus_api-1.5/group__opus__encoderctls.html)
documents configuration-dependent codec delay; do not infer a universal tail
from one tested pre-skip value. A single 60 ms SILK frame also requires actual
conversion to satisfy the selected 20 ms duration; repacketization cannot split
that encoded frame into three independently decodable frames.

### 3. Camera state and native work must have separate ownership

Snapshot current generation, capture epoch, encoder identity and stop authority
under a short state lock. Serialize native normalization under its own owner
outside that lock. Reacquire state and revalidate every identity before
publishing output or assigning an error. A late operation cannot publish into
or fail a replacement generation.

Retirement fences first and joins outstanding native/thread work outside the
state lock. A timed-out owner remains retained/quarantined rather than being
reported released. Do not remove privacy checks, assume cancellation stops a
thread, or disable compatibility consumers as a shortcut. Tests must cover
stop/mute, close failure/retry, source replacement, reentry, concurrent legacy
and native consumers, and ABA state changes during encode.

### 4–5. Preserve cadence, references and complete-frame delivery

Use rational source-time phase selection with bounded timestamp quantization
tolerance. A source below the selected FPS remains at its real cadence; do not
duplicate pictures to claim 30 fps. Encode with PTS representing actual source
time, and force/verify an output IDR by a two-second admitted-source-time
budget. Test near-rate jitter, fractional rates, sparse input and long GOPs.

Once a reference AU is discarded or partially delivered, an IDR-required state
must persist across every dependent output. Clear it only after a complete
current-epoch IDR with current parameter sets is sent. In conversion, force an
encoder IDR. In compatible-copy mode, add a coalesced, current-lease/epoch
callback to the existing receiver's validated PLI seam; it must not acquire a
second Ring connection. Bound waiting and retries. H264 refresh and feedback
behavior is guided by [RFC 6184 §8.5](https://www.rfc-editor.org/rfc/rfc6184#section-8.5)
and [RFC 4585](https://www.rfc-editor.org/rfc/rfc4585).

Pre-admit each new complete AU using protected-byte pacing, queued repairs and
its receipt/completion deadline. A frame arriving at age 490 ms must either
finish within the 500 ms new-delivery limit or be rejected before starting.
Check deadline, authority and epoch again during socket retries. Unexpected
mid-AU expiry/backpressure enters IDR recovery; it cannot silently resume a
broken reference chain. A single temporarily infeasible AU should not kill the
whole healthy view; persistent inability to produce/send a valid IDR gets a
finite explicit failure, rather than an infinite retry loop.

Encoder payload target and protected-RTP wire pacing are different budgets.
Reserve measured fragmentation/header/recovery headroom and bound VBV/IDR
peaks within Home's request. Never raise the selected cap or merely enlarge
buffers to hide a growing queue. Preserve the existing authenticated-feedback
bounds and one-second/512-packet/1 MiB repair cache. New AU freshness and an
explicit bounded cached repair have distinct semantics and counters. Process
repair requests with finite byte/time credit so they cannot starve new media,
BYE handling, source changes or privacy retirement.

### 6. Automatic compatible quality and one stable timeline

Track `supported`, `requested`, `source` and `applied` properties separately,
plus a fixed copy/adapt reason. Copy only proven compatible encoded source;
otherwise use existing managed conversion at the selected contract. Measure
source SPS/profile/level/geometry/cadence and actual output rather than assuming
a request is what Ring provided. Qualifying VUI-absent cadence or additional
profile compatibility is a separate test gate, not a speculative bypass.

Halo already advertises 1080p. The observed 720p/30 fps/299 kbit/s contract came
from Home; advertised ordering or removing lower modes cannot guarantee a
better selection. The public [HAP selected video API](https://developers.homebridge.io/HAP-NodeJS/types/VideoInfo.html)
and [advertised options](https://developers.homebridge.io/HAP-NodeJS/types/VideoStreamingOptions.html)
are different surfaces. A 37 kbit/s average under a 299 kbit/s ceiling may be
normal VBR in a static scene; it is not a reason to add padding or change caps.
At that ceiling a 44,032-byte IDR alone needs about 1.178 seconds before framing,
so some higher-rate source frames cannot be copied within the delivery budget.

A view-owned timeline should relate source RTP, output RTP and common time.
Use valid matching-epoch source sender reports when available. Otherwise keep
an explicitly uncertain receiver estimate anchored once, advancing by actual
source PTS and conversion offsets; do not reuse one receipt as the presentation
time of all three converted outputs. Preserve a stable mapping through
same-source reconfigure; a true capture reset gets a separately fenced epoch.
Outgoing SR NTP/RTP must describe the same instant. Never add a guessed audio
delay or change classic-HAP wire clocks as part of a timing fix.

Apply [RECONFIGURE](https://developers.homebridge.io/HAP-NodeJS/types/ReconfiguredVideoInfo.html)
transactionally at a bounded AU boundary, retaining UDP endpoints, SSRC, SRTP
sequence/index and exact scoped callback ownership. Configuration generations
fence old native results after the new contract is acknowledged. Review
compound RTCP/CNAME compatibility in the lab, not as an unproven explanation
for the current freezes or an automatic protocol change.

### 7. Verification, rollout and completion criteria

The implementation is not complete merely because a view stays connected.
Required gates:

- **Audio accounting:** regular native runs account for every source/codec/
  queued/sent/discarded duration, with no unexplained deficit, reset storm or
  accumulating delay. Test 60/180/300/600 ms arrivals, over-budget 1,200 ms
  bursts, variable native delays and simultaneous real video conversion.
  Expected expiry/overflow loss stays explicit and bounded.
- **Video correctness:** native 15/24/29.97/30/60 fps sources, one-tick jitter,
  lower source FPS, source gaps/wrap, long GOP, moving/high-entropy scenes,
  low selected rates, stale/multi-output dependencies and source SPS changes.
  Independently reconstruct/decode SRTP output and verify actual dimensions,
  cadence, IDRs and byte-exact copy where selected.
- **Timing:** deterministic known-source flash/impulse fixtures, delayed/lost/
  reordered SRs, asymmetric worker delay and reconfigure. SR arithmetic within
  one applicable clock tick; decoded event alignment within one video period
  plus one audio packet (about 53.4 ms at 30 fps/20 ms, 86.7 ms at 15 fps/20 ms).
  Proposed added-drift goal: no more than 20 ms over 30 minutes. Receiver
  estimates and actual Apple rendering must retain distinct uncertainty labels.
- **Feedback and ownership:** authenticated/malformed/wrong-source reports,
  negative cumulative loss, LSR/DLSR validation, rollover, loss storms and repair
  starvation; privacy/epoch/STOP during every wait and partial operation; no
  late output, leaked capture/worker or reused crypto index. Existing renewal,
  snapshot/settings/HAP callbacks and cleanup-failure regressions remain green.
- **Packaging:** full Python/Node/lint/format/TypeScript/lock/build gate; two
  identical release archives; extracted installed daemon/native/public-HAP
  qualification and server staging verification. No new dependency or public
  plugin protocol is required for this plan.
- **Physical pilot after implementation approval:** retain the same floodlight
  pairing and exact HKSV-off preference bytes. Compare baseline and repaired
  local Wi-Fi views, at least 30 open/close cycles, 10-minute continuous motion/
  ambient-audio sessions, longer sync observation and two supported viewers.
  Test remote/Home Hub separately, recording actual selection and versions.
  No media recording is needed for ordinary owner observation; quantitative
  rendered A/V measurement needs a separately agreed measurement method.
  Do not alter live server networking for loss simulation; use isolated tests.

A successful pilot requires no repeated converter cascade, no unexplained Halo
input-to-output loss, no recurring freeze/reload under the qualified conditions,
no growing backlog, correct privacy behavior and complete resource cleanup.
Measure startup/added latency against the same-camera baseline before assigning
release thresholds; do not invent a latency promise from synthetic tests.
Missing receiver reports or untested device/network cases remain explicitly
unknown. Actual HKSV, extra cameras and remote cases are separately qualified.

After approval, build and test each slice, commit locally, then stage the
qualified package through the existing managed release selector. Preserve the
current release and paired identity for recovery. Pilot failure blocks promotion
of a reliability claim and determines the next targeted measurement/repair.
No HAP identity reset or GitHub push is included. This research does not start
any of those steps.

## Research evidence and deferred choices

The accompanying research archive contains four focused plans and synthetic
probe programs/results: audio tail/provenance and scheduling, native video
cadence/GOP, sender dependency/deadline cases, quality/clock arithmetic and the
shared-lock coupling demonstration. Exact local source provenance and a final
unchanged-product check are included. Official primary references are listed
in the focused plans; pinned sources are distinguished from current API docs.

The separate synthetic 30 ms CBR Opus TOC-transition defect remains open and
must not be represented as fixed by this measured 20 ms repair. A successful
20 ms pilot cannot qualify every advertised packet-time/bitrate combination;
that case needs its own repair or an explicitly qualified capability restriction
before claiming comprehensive negotiated-mode support. No forced
1080p, modern/H265 transport, alternate Ring backend, hardware acceleration or
new HAP extension is proposed. If measured limits later require one of those,
or a larger live latency allowance, present that concrete change to the owner
before proceeding.
