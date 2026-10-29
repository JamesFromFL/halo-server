# Live-stream repair implementation

Owner authorization: October 28, 2026. Commit each tested slice locally; no push.
Preserve pairing and HKSV-off preferences. Keep real-device qualification
separate from synthetic and installed-artifact checks.

| Slice | Scope | Status |
| --- | --- | --- |
| 1 | Bounded stream diagnostics | Complete: `47ffd76`; 2,025 Python / 265 Node tests passed |
| 2 | Audio recovery and bounded sending | Complete: `78e0e81`; 2,040 Python / 265 Node tests passed |
| 3 | Camera-state lock isolation | Complete: `4adb0c2`; 2,097 Python / 265 Node tests passed |
| 4 | Video cadence and IDR timing | Implementation complete; verification recorded in the slice receipt |
| 5 | Complete-frame delivery and loss recovery | Pending |
| 6 | Quality, A/V timeline and reconfigure | Pending |
| 7 | Installed and real-device qualification | Pending |

## Slice 1

Added fixed-schema source-ingress, admission, normalization, audio-duration,
video-frame, pacing and event-loop measurements. Incoming Ring receiver stats
and authenticated Home receiver reports distinguish missing/stale evidence from
zero loss; RTT requires correlation to a sender report actually sent. No media,
keys, addresses or raw synchronization identifiers are exported. Active/recent
summaries are bounded and finalized after owned cleanup. This slice deliberately
preserves the observed playback behavior so later repairs have a useful baseline.

Focused native and composed audio checks, source/session diagnostics, video
lifecycle/receiver reports and telemetry concurrency checks precede the full
Python/Node/format/lock/package gate. Physical playback remains unqualified.

A review found a diagnostics-only retirement race in receiver statistics. The
final slice includes a revision fence and a deterministic regression so a late
receiver observation cannot look current after STOP. This does not change media
authority. Any gate run before that repair is preliminary and cannot authorize
the commit.

## Slice 2 design decision

The approved plan made keeping the native converter across expired buffered
input conditional on conservative contribution proof. Output PTS and one
observed 1,080-sample tail do not establish resampler filter support for every
qualified format. The first repair therefore uses the approved safe fallback:
retire once, recheck authority/epoch/deadlines, and reuse valid input from the
current batch. Separate conversion and egress owners with inclusive finite
retention prevent pacing from stopping source consumption. No noise suppression,
gain change, repeated samples or invented silence is part of this repair.

A separate 30 ms CBR grouping/TOC transition remains an explicit qualification
case. Fixing the measured 20 ms path does not qualify every negotiation. Bounded
catch-up sending remains a measured candidate until its transport and physical
Apple behavior are qualified; timestamps continue to represent media time.

The upstream [FFmpeg n8.0 libopus wrapper](https://github.com/FFmpeg/FFmpeg/blob/n8.0/libavcodec/libopusenc.c#L147-L153)
uses `OPUS_SET_MAX_BANDWIDTH` for its cutoff option. The [Opus API](https://opus-codec.org/docs/opus_api-1.5/group__opus__encoderctls.html)
distinguishes that automatic ceiling from a fixed bandpass. A cutoff option
therefore cannot establish stable grouping TOCs. No narrower audio bandpass or
new native binding is approved as a shortcut. This is upstream supporting
research; the installed native runtime remains the qualification authority.

The committed R1 diagnostic release was qualified from two identical archives
and installed on the paired server as selection revision 9. Its metadata-only
[real baseline](stream-repair-research-2026-10-28/r1-live-baseline.md) confirms
bursty encoded arrivals and large local audio recovery loss. No source admission
rejects occurred; measured lock waits were short. Physical owner feedback is
kept separate from the numeric observation.

Slice 2 focused qualification passed 195 audio/native/composed checks before the
whole-suite gate. Conversion and egress are separate owned tasks; the shared
32-packet/40-KiB bound includes native response staging and the sending head.
Every complete output is classified, rather than abandoning a fresh tail after
one expired output. Ordinary sending remains at the negotiated media cadence;
a faster catch-up sender is not silently enabled. Native regular, 600-ms and
over-budget 1,200-ms burst fixtures independently decode output and reconcile
all terminal outcomes. Expiry and bounded overload loss remain explicit.

A brief same-epoch microphone permission pause also fences queued delivery,
retires old native conversion once, and enforces a new admission boundary before
resume. Both sender-observed and read-observed denial paths are covered. Native
converter reuse without proven contribution support and the separate 30-ms TOC
case remain unqualified; no codec/backend/filter change is part of this slice.

The owner's joint two-seconds-playing/one-second-pausing report expands R3
measurements: bounded per-instance raw RTP handler entry/exit and shared short
cadence bins will distinguish packet handling from assembled encoded frames.
Existing one-second receiver samples and aggregate frame intervals cannot alone
separate network arrivals, jitter assembly and sender/report timing.

## Slice 3

Legacy audio normalization now has a separate bounded native lane. Decoded
frames retain their source timing, and constructor, conversion, encoding and
close operations run outside the Ring event loop and camera-state lock. The
original encoded audio/video path does not wait for this compatibility lane.
Source generation, capture attempt, stop event and native owner must all remain
current before any result can be delivered. Cancellation fences first and joins
the actual native work; a timeout retains cleanup ownership instead of claiming
that an executing native operation stopped.

The decoded handoff charges queued, native and in-flight frames against fixed
limits. Two native worker threads serve a finite owner set, with at most one job
per owner. Startup failure, queued cancellation, blocked close and retained
exception tracebacks have explicit fault-injection coverage. Detailed local
status has a separate getter/serialization budget so a large camera inventory
does not exceed the administrative response limit.

Per-receiver RTP handler measurements and shared 100-ms bins are an observation
aid, not a claim that a post-jitter encoded gap originated on the network. A
synthetic reordered-packet case reproduces a long encoded gap despite shorter
raw packet gaps and final zero cumulative loss. Only the next paired-device
measurement can attribute the owner's repeated joint audio/video pauses.


## Slice 4

A rational source-time phase replaces restarting the frame interval after every
accepted picture. Small clock jitter no longer discards alternating nominal
frames. Adaptation keeps each selected picture's actual90kHz PTS and never
duplicates a slower camera's pictures. Copy remains byte-exact when compatible;
a real repeated phase triggers the existing adaptation/IDR boundary.

Managed encoding forces and verifies actual NAL5 plus current SPS/PPS on a
source-time recovery interval. Quantization and a missing source picture allow
one available input interval; no exact wall-clock promise is made when Ring
provides no picture. Copy still needs a real source IDR; current-lease upstream
requests and bounded recovery belong to slice5.

Native investigation also found that x264 zerolatency uses constant-rate input
accounting. A15fps source under nominal30 can spend about half its payload
target. Enabling variable-rate accounting restores the tested moving-fixture
budget but introduces one frame of lookahead and non-fixed VUI timing. Slice4
preserves the existing immediate-output mode; slice6 must qualify that explicit
tradeoff with original-source receipt attribution before any adoption.

The R4 review also found a keyframe-request race: feedback arriving while a native
feed was awaiting completion could retroactively require that already-dispatched
P frame to be an IDR, then erase the pending request. A request revision now
separates dispatched and newer requests. Held-feed and skipped-output regressions
preserve the next actual forced IDR. Twenty-seven focused cadence/native tests pass.
