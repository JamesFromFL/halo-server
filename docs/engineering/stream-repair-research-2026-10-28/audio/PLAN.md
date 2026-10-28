# Audio stream repair: research and proposed implementation

2026-10-28. **Planning and isolated experiments only. No application changes,
deployment, service restart, account access, or commit.**

## Findings and recommendation

The remaining audio failure is a delivery problem, not a reason to add noise
suppression. The observed session received 3,522 single-frame mono WB/SILK
60 ms packets, representing 211.32 seconds. Halo fed only 1,573 to conversion
and sent 4,310 negotiated 20 ms packets, representing 86.20 seconds:
40.79% of observed source duration, or 39.90% of the 216.038-second session.
There were 161 converter recreations and 179 output timestamp gaps. The
first 30 seconds had continuous source RTP timestamps; three later source
timestamp gaps were also observed, with maximum missing duration 2.58 seconds.
The session ended with an ordinary Home STOP, which does not make its media
delivery healthy.

Recommend a bounded producer/egress pipeline with explicit sample provenance.
For ordinary idle/arrival gaps in the **same authorized source epoch**, keep
the reference decoder and encoder alive and discard only complete output
packets whose contributing input is expired. Do not discard an entire fresh
batch just because old native pending samples exist. If provenance or source
continuity cannot be established, reset native conversion state once and
reconsider the current fresh batch after retirement; do not blindly wait for
another read. Privacy, source identity, and capture-epoch changes retain a
hard synchronous fence and owned native retirement.

Keeping a converter requires proving the contribution boundary; it is not
permission to erase an old receipt from the ledger. Until that proof passes,
reset-and-reuse is the conservative fallback. Neither option should require
a new backend, codec dependency, ctypes binding, noise filter, or changes to
recording pre-roll.

## What is proved, and what is still unknown

The current source format is measured: 60 ms, one SILK frame per packet,
mono, 16 kHz audio bandwidth. Output is 24 kHz mono, 20 ms, 24 kbps VBR.
Normal classic-HAP output RTP increments are 480 ticks. The internal source
and output-codec timeline remains 48 kHz, where each 20 ms packet is 960
samples. Preserve that distinction rather than changing clocks to disguise
missing media.

The earlier 30 ms native grouping failure remains a separate qualification
item: three 10 ms encoder units cannot be merged across a TOC/configuration
change. It was reproduced with a different synthetic stereo source and is
not the current measured 20 ms failure. Do not silently force narrower
bandwidth or mark that open case fixed by these scheduling changes.

Contiguous RTP timestamps do not establish regular wall-clock arrivals.
Existing metadata lacks adjacent arrival-time deltas, batch sizes, feed-time
totals, retirement time, and synchronous source/admission lock wait. The
216-second trace cannot uniquely separate bursty upstream delivery from
service scheduling/lock stalls. Root independently demonstrated a possible
session-lock stall using an injected slow legacy normalizer; that is a
counterexample, not measured production normalization cost.

The actual native control with the measured source format, VBR, and concurrent
video conversion was clean for 30 seconds: 1,478 sent and received, one audio
worker, no stale drops or converter recreations. A separate native 600 ms
burst counterexample using the current application delivered only 37 packets
and recreated conversion 19 times in 12 seconds, despite continuous source
RTP timestamps and about 1 ms measured event-loop lag. This demonstrates a
real recovery defect under a plausible arrival pattern. It does not establish
that Ring actually used that pattern in the user's session.

## Why the current loop fails

Local product references, inspected read-only:

- `src/halo_server/media/homekit_audio.py:380`: `_expired_prefix()` treats any
  old retained source span as a reason to retire all native conversion.
- `homekit_audio.py:451`: after retiring that prefix, the already-read batch
  is discarded with `continue`, even if its packets have just arrived.
- `homekit_audio.py:396`: output receipt attribution and input-span retirement
  are coupled to iterating output packets. A stale-output `break` at line 511
  also discards later outputs and the unprocessed remainder of the batch.
- `homekit_audio.py:497` and `:521`: conversion and 20 ms pacing share one
  loop. Three outputs from a 60 ms input consume about 40 ms of pacing time,
  leaving only about 20 ms for sustainable serial feed work.
- `src/halo_server/media/homekit_audio_codec.py:296`: source epoch or RTP
  discontinuity already resets the adapter; this must be coordinated with
  an outer recovery owner to avoid duplicate resets.
- `src/halo_server/media/audio_worker.py:81`: resampling, FIFO quantization,
  and encoder delay retain more than codec lookahead alone.
- `src/halo_server/media/opus.py:207`: original encoded buffering and its
  `Condition.wait_for` predicate are protected by the same lock as append and
  notification; review found no lost-notification defect. A live consumer
  starts with the latest packet; recording retains independent history.
- `src/halo_server/media/homekit_live.py:689`: presentation mapping is used
  for RTCP correlation, not receipt-based output pacing. Its synchronous
  getters can still wait on shared source state. Root owns that lock review.

The earlier description of a retained “2.5 ms tail” was incomplete. Actual
native experiments now measure **1,080 un-emitted timeline samples at 48 kHz,
or 22.5 ms**, at a representative boundary. That includes FIFO/output
quantization as well as encoder priming. No algorithm should hard-code either
120 or 1,080 as a universal codec contribution bound.

## Primary sources and their implications

1. [Opus encoder CTLs, GET_LOOKAHEAD](https://opus-codec.org/docs/opus_api-1.5/group__opus__encoderctls.html):
   codec delay depends on implementation and initial configuration. Query or
   observe it instead of assuming a fixed number. The installed PyAV 17.1.0
   encoder's public `delay` property is zero at qualified rates and therefore
   is not sufficient. Its public validated `OpusHead` extradata exposes
   pre-skip; actual output PTS and FIFO counters are also needed.
2. [FFmpeg libopus encoder source](https://github.com/FFmpeg/FFmpeg/blob/n8.0/libavcodec/libopusenc.c#L387-L394):
   the wrapper obtains lookahead and writes it into the Opus header. This is
   supporting upstream implementation evidence, not a claim that a generic
   FFmpeg tag identifies every byte of the installed wheel.
3. [Exact PyAV 17.1.0 FIFO source](https://github.com/PyAV-Org/PyAV/blob/v17.1.0/av/audio/fifo.py)
   and [resampler source](https://github.com/PyAV-Org/PyAV/blob/v17.1.0/av/audio/resampler.py):
   FIFO input/output samples and partial reads are observable, and conversion
   may buffer data. Use the pinned source and installed runtime; the old
   pyav.org stable alias currently identifies version 9.0.2.
4. [Opus repacketizer API](https://opus-codec.org/docs/opus_api-1.5/group__opus__repacketizer.html):
   repacketization rearranges existing encoded frames with compatible mode,
   bandwidth, frame size, and channels. It cannot turn a single 60 ms SILK
   frame into three independent 20 ms frames. Reference decoding and the
   existing native 20 ms encoding are necessary for this measured negotiation.
5. [RFC 6716 §2.1.4](https://www.rfc-editor.org/rfc/rfc6716.html#section-2.1.4):
   distinguishes frame duration from packets containing multiple frames.
   Larger packets have different latency and loss consequences. Negotiation
   cannot be satisfied by relabeling a 60 ms frame as 20 ms.
6. [RFC 7587 §4.1](https://www.rfc-editor.org/rfc/rfc7587.html#section-4.1)
   and [§6.1](https://www.rfc-editor.org/rfc/rfc7587.html#section-6.1):
   standard Opus RTP uses a 48 kHz timestamp clock, with packet time separately
   negotiated. Halo's existing classic-HAP selected-rate exception is an
   explicit compatibility path, not generic RFC Opus behavior.
7. [RFC 3550 §6.4.1](https://www.rfc-editor.org/rfc/rfc3550.html#section-6.4.1):
   interarrival jitter compares arrival-clock differences with source RTP
   differences. Timestamp continuity alone cannot measure arrival jitter.
8. [Scrypted classic sender](https://github.com/koush/scrypted/blob/main/plugins/homekit/src/types/camera/camera-streaming-srtp-sender.ts#L95-L143):
   sends the repacketized group immediately, while assigning media timestamps;
   it has no per-packet timer in that group. This is useful compatibility
   precedent, not proof of Halo behavior on an Apple receiver.
9. [Homebridge Ring sender](https://github.com/dgreif/ring/blob/main/packages/homebridge-ring/camera-source.ts#L138-L173):
   forwards transcoded audio when received and rewrites the selected-rate
   timestamps. Again, it does not impose Halo's extra per-output sleep.

## Proposed algorithm and invariants

### 1. Observe the actual boundary, without changing timestamps

Measure encoded ingress before the source observer/session lock, admission
completion, and consumer observation separately. Keep `received_at` semantics
unchanged until those measurements are qualified. Add constant-space counters
for interarrival step/jitter bins, maximum batch size, read wait, feed time
sum/max, native retirement time, pacing wakeup lateness, and authority/clock
getter time. Keep the existing bounded log schedule, no payloads or absolute
clock/identity data, and make diagnostics non-authoritative.

### 2. Separate freshness expiry from source/permission reset

Maintain a bounded ordered interval ledger for each actual input packet:
source start/end, current scope/epoch token, and monotonic local receipt.
Native output receives a complete conservative contribution range, accounting
for resampling, FIFO, codec priming, and 20 ms quantization. Prefer extending
the existing isolated worker response with fixed, validated metadata derived
from its public PyAV objects and actual timestamps, rather than guessing in
the sender. Limits apply to metadata entries as well as payload bytes.

For the same current scope, same capture epoch, and continuous source RTP:

- Feed the current fresh packet to the existing native worker.
- For every produced complete Opus packet, compute the oldest contributing
  receipt. If it is expired, discard **that packet**, advance the consumed
  ledger frontier, and continue examining later outputs. Never simply delete
  expired receipt metadata while permitting its PCM contribution to escape.
- Keep the worker alive across ordinary idle periods. A small pending tail
  cannot by itself invalidate unrelated new packets.
- If contribution provenance is absent, unbounded, or inconsistent, use the
  conservative reset fallback: own and join native reset, retain only the
  bounded current batch, then recheck current authority/epoch and each actual
  deadline. Reuse valid fresh input; discard only expired or invalid input.
- An output that remains stale at socket delivery is still rejected. A
  privacy/source/epoch change fences and retires the entire pipeline; the
  ordinary-idle optimization must never cross it.

A same-process codec-context reset command is a possible fallback optimization
after qualification, retaining existing subprocess isolation. It would create
fresh decoder/resampler/FIFO/encoder objects with the existing fixed settings,
and use the existing finite request timeout. It is not needed to prove the
first keep-worker path, and is not an invitation to flush live audio with
synthetic silence or reuse an encoder after end-of-stream.

### 3. Separate conversion from egress with finite ownership

Use one owned conversion task and one owned egress task per stream. The
producer may continue converting while the sender waits; it cannot create
unbounded tasks or native requests. The egress queue proposal is at most
32 complete output packets **and** 40 KiB, with each packet also bounded by
the existing audio payload/UDP cap. Every queued entry carries scope/epoch,
media PTS/duration, and conservative delivery deadline. No SRTP index is
allocated until final send; once allocated it is never reused.

At queue saturation, reject/drop explicitly under a documented bounded policy
and count the dropped duration. Do not block source processing indefinitely
or silently repeat the most recent sample. Queue and pending native owners
remain retained until actual cleanup completes. The parent STOP/privacy fence
clears delivery authority synchronously, wakes both tasks, and joins both
before source ownership is released.

### 4. Qualify a bounded catch-up sender

First compare two policies offline and then on HomeKit:

- Normal 50 packet/s egress for negotiated 20 ms media, burst credit three.
- Temporary catch-up capped at 100 packet/s, burst credit three, with an
  independent byte token budget and the same queue/deadline bounds. Suggested
  byte-rate derivation is twice the selected payload budget plus actual RTP,
  SRTP, and IP/UDP overhead; cap the instantaneous byte credit as well.

These are proposed qualification values, not adopted settings. At most three
packets can leave on one credit grant, and the sender yields between groups.
Media timestamps remain 480 selected-clock ticks per 20 ms packet; a faster
UDP drain does not speed up the encoded sound. Apple owns downstream playout
and jitter buffering. Match RTCP pairs to the chosen media clock model and
qualify A/V offset; do not replace source time with packet arrival time or
conceal loss by renumbering media samples.

No unconditional network flood, queue growth, change to the 500 ms local
delivery deadline, or assumption about Apple buffer capacity is proposed.
For 600 ms of media arriving at once, a strict 1× sender needs about 580 ms
between its first and last 20 ms packets even with instantaneous encoding.
It cannot deliver all of them inside a 500 ms receipt deadline. Faster bounded
UDP catch-up can change that transport constraint, but cannot guarantee Apple
plays continuously or remove an actual upstream delay. Real source gaps,
excessive bursts, sustained conversion overload, and stale contributor packets
still require honest bounded loss.

## Isolated experiment receipts

- `native-tail-probe.py` / `native-tail-result.json`: real reference Opus,
  PyAV worker, and independent output decoder, with two logical 600 ms batches.
  Reset-and-reuse retained 58 valid 20 ms packets from 20 source packets with
  one retirement. Keep-worker generated 59, discarded two outputs attributed
  to expired input, and retained 57 with no retirement. Both decoded to the
  expected sample counts. This is a provenance/validity probe, **not** a
  real-time pacing, exact waveform quality, or Apple test. Resampler support
  bounds remain a gate before adopting its interval attribution.
- `scheduling-model.py` / `scheduling-model-result.json`: discrete-event model
  only. Decoupled 40 ms warm conversion at regular 60 ms input preserved all
  599 generated packets in the modeled interval. With 600 ms bursts, 50 pps
  retained 515 versus 534 at bounded 100 pps; the latter still discarded 38
  genuinely expired-contributor outputs. At 1,200 ms bursts the 32-packet
  queue overflowed and loss remained explicit. It proves neither Apple buffer
  compatibility nor byte budgeting for variable real payload sizes (the
  prototype uses fixed 80-byte accounting).
- Existing sibling native receipts:
  `work/halo-audio-source-audit-2026-10-28/evidence/dropouts/concurrent-vbr30s.json`
  and `concurrent-burst12s.json`. These establish the clean native control
  and the current application's burst recovery failure, respectively.

## Proposed code touch map

| Location | Planned change |
|---|---|
| `media/homekit_audio.py` | Distinct conversion/egress owners, bounded queue and credits, per-output stale disposal, fresh-batch reuse, exact delivery accounting, final privacy checks. |
| `media/homekit_audio_codec.py` | Validate bounded contribution/progress metadata and preserve one native request owner; explicit source reset and failure semantics. |
| `media/audio_worker.py` | Expose measured native progress/priming/FIFO metadata; keep reference decoder and existing signal settings. Optional separate reset-context operation only if needed and qualified. |
| `media/ring_media.py`, `media/session.py` | Bounded ingress/admission measurements; root owns lock separation proposal. |
| `media/homekit_live.py` | Join the complete audio owner on STOP and qualify presentation/RTCP correlation; no clock convention change hidden in the repair. |
| New audio regression tests | Native true-SILK inputs, timing/queue/property tests, independent SRTP decode, privacy and retained cleanup tests. |

## Acceptance gates before implementation is called complete

1. Qualify complete sample-contribution bounds using actual resampler/FIFO
   behavior, measured priming and PTS; include negative initial PTS, single
   60 ms SILK frames, 3×20 ms CELT packets, mono/stereo, and source clock gaps.
2. Native 30–60 second regular source plus concurrent real video: account for
   all source, feed, generated, queued, expired, skipped, and sent durations;
   no unexplained deficit, repeated worker resets, or growing latency.
3. Native arrival patterns 60/180/300/600 ms, plus over-budget 1,200 ms bursts:
   no reset storm, no whole fresh batch discard, no unsignaled loss, and all
   packet/byte bounds respected. Do not require impossible lossless 1×/500 ms
   behavior for the 600 ms case.
4. Worker delays 1/20/40/80 ms and sustained overload: show conversion/pacing
   overlap, finite queue/drop behavior, bounded deadlines, and preserved video
   responsiveness. Measure averages and attribution rather than only maxima.
5. Privacy revocation during conversion, context reset, queued output, pacing,
   socket retry, and STOP: zero delivery after the fence; no late source
   acquisition or cleanup owner escape. Source/capture changes cannot share
   the old codec or queued output. Test failed native cleanup and retry.
6. Independent SRTP unwrap/reference decode: correct negotiated packet
   duration, selected-rate RTP increments, monotonic media gaps on actual
   drops, sequence/timestamp wrap, no index reuse, and explicit RTCP/A/V
   correlation. Retain existing sample/codec compatibility tests.
7. No default denoise, NS, AGC, echo filter, noise gate, speech classifier,
   silence filling, sample repetition, time stretching, or bandwidth reduction.
   Existing user volume and necessary mono/sample-rate conversion remain
   explicit. Synthetic signal checks must compare against a decoded source,
   with no real audio saved.
8. Real Apple local Wi-Fi and remote/Home Hub sessions: listening continuity,
   video freezes/reloads, first-frame and audio delay, A/V offset, ambient
   sound, long-session stability, Home STOP/reopen, microphone mute/privacy,
   and talkback. The bounded catch-up policy is not accepted merely because
   a synthetic receiver decodes the packets. Recording history is separately
   verified unchanged.

## What must be reported before adoption

The approved implementation should identify the chosen egress credit/rate and
why its offline and Apple gates passed; state every remaining loss source and
latency bound; show the improved live source-to-egress accounting; and preserve
rollback to the currently installed release. Do not promise perfect feeds,
recovered source packets, or unchanged subjective quality from synthetic packet
counts alone.
