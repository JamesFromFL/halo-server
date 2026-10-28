# Floodlight live-stream audit — October 28, 2026

The actual Halo/HomeKit stream is **not qualified as continuous or reliable**.
The owner reports that video freezes or reloads. The captured audio counters
also show substantial loss inside Halo's delivery path. Passing synthetic tests
and an active connection did not establish successful playback on this camera.

The observed session ran from **6:51:06 to 6:54:42 p.m. EDT** (216.1 seconds),
using release `ab62415a…`, local commit `6677086`. This investigation made no
application changes or service restart, saved no real camera media, and left
HKSV off.

| Measured item | Actual result |
| --- | --- |
| Home's selected video | 1280 × 720, 30 fps, H264 High/4.0, 299 kbit/s ceiling |
| Video processing | Conversion (`adapt`) |
| First complete video frame sent | 2.088 s after session preparation; source age 290 ms |
| Selected audio | Mono Opus, 24 kHz, 20 ms, 24 kbit/s VBR |
| Observed original audio | Mono wideband SILK, 60 ms packets |
| Audio packets observed / passed to conversion | 3,522 / 1,573 |
| Audio duration observed / passed to conversion | 211.32 s / 94.38 s |
| Audio packets / duration emitted | 4,310 × 20 ms = 86.20 s |
| Emitted audio duration / observed input duration | Approximately 40.8% |
| Audio converter recreations | 161 |
| Output / transmitted timestamp discontinuities | 179 / 179 |
| Stale output packets counted | 105 |
| Maximum scheduled-send lateness | 2.113 s |
| Largest transmitted timestamp step | 79,200 at 24 kHz = 3.3 s |
| Source timestamp discontinuities | First 30 s: zero. Entire session: three; largest missing interval 2.58 s |
| Video packets / payload bytes emitted | 2,193 / 997,960 |
| Session termination | Home `stop_request`; no stream exception |
| Final source / feedback age | 119 ms / 2 ms |

The audio-duration comparison measures what Halo observed and emitted. It is
not a network packet-loss percentage or a measurement of Apple's decoded output.
Most missing duration lies before converter submission. The current counters do
not separately label every discarded batch and converter tail. The first
30 seconds already show 27.90 seconds observed, 13.38 seconds submitted and
12.28 seconds emitted, with 19 recreations; the defect continues beyond startup.
Three later source-timestamp gaps mean the entire source timeline cannot be
called gap-free. They do not identify whether Ring transport, buffer
resynchronization, or another upstream stage caused those gaps.

The earlier live-edge change reduced one reset cascade but did **not** solve
actual continuity. No noise suppression, gate, AGC or other filtering was added.
The faithful ambient-audio requirement remains unchanged.

The video connection stayed active until Home requested stop, but the owner
observed freezes or reloads inside that session. Its terminal packet count does
not measure frame cadence or establish continuous client decoding. Home chose
720p and a 299 kbit/s ceiling, and Halo converted the video; this session does
not establish full-resolution passthrough or maximum attainable image quality.
The adapter also advertises 1080p, so the observed 720p selection alone does not
prove an advertisement limit or justify forcing a different selection.

Three in-session health snapshots show one upstream and one live lease,
synchronized events, no cleanup failures and no automatic service restart.
A 15-second CPU/UDP-error sample occurred **after the camera session ended**;
it cannot rule out CPU or socket pressure during playback.

## Reproductions and remaining uncertainty

A 30-second native control used the actual reference decoder, true mono WB
60 ms SILK packets averaging 415 bytes (similar to the observed source), Home's
VBR 24 kHz/20 ms conversion, original buffer/threaded reads, and concurrent
1080p-to-720p video conversion. All 1,478 emitted audio packets arrived at the
local UDP receiver, with one worker, no recreations, no stale drops, continuous
timestamps and 2 ms maximum event-loop lag. The isolated conversion pipeline
can keep up with regularly spaced input under those tested conditions.

A separate **12-second injected burst test** used the same native pipeline but
supplied ten consecutive 60 ms packets every 600 ms. It produced 19 converter
recreations and only 37 emitted/received packets, despite a 68 ms maximum feed
latency and 1 ms maximum event-loop lag. A retained codec-lookahead tail exceeds
the 500 ms freshness limit before the next burst. Recovery then discards the
already-read batch, including fresh packets, and repeats. This is a demonstrated
buffering/recovery weakness. **It does not prove that actual Ring packets arrived
in those bursts.** RTP timestamps measure media progression, not arrival spacing.

A shared-lock risk also remains: the legacy PCM normalization lane performs
encoding while holding the camera session lock. HomeKit authority checks and
source-clock snapshots synchronously acquire that same lock. Its actual hold
and wait times were not measured, so this is an investigation target rather
than an established cause. Long pacing/presentation-age peaks can result from
output gaps and do not by themselves prove an event-loop stall or clock error.

## Next diagnostic and repair work

1. Measure actual source inter-arrival spacing, read-batch size and read wait;
   retain bounded counts/histograms rather than media or per-packet logs.
2. Separate conversion time, converter retirement time, authority/lock wait and
   event-loop lag; count why each input batch or output is discarded.
3. Add video input/output frame counts, stale/discard/keyframe counters, encoder
   and send time, achieved bitrate/FPS, NACK/PLI/retransmit counts and usable
   authenticated receiver-report loss/jitter/RTT values.
4. Use those observations to repair recovery around fresh batches and test
   sustained burst patterns, pauses and slow conversion. Preserve privacy/epoch
   fences and do not silently relax freshness or substitute repeated audio.
   If actual arrival buffering requires a latency tradeoff, document it before
   changing that policy.
5. Retest real video motion, audio continuity and A/V synchronization after the
   correction. Existing isolated success remains a control, not a substitute
   for the real-camera result.

The current implementation discards receiver-report loss/jitter fields and
exposes no ongoing video FPS, measured A/V offset, or Apple decoder outcome.
Those are concrete diagnostic gaps. This audit supports neither a claim of
zero network loss nor a claim of a perfect end-to-end stream. No backend swap
is justified by the current evidence.
