# Complete Opus release using existing retention

The November 7 complete capture established that Ring audio arrived as complete
60 ms Opus packets without sequence loss, while aiortc's generic audio buffer
retained four packets and released one only when another packet arrived. That
imposed an initial prefetch delay without making those already complete samples
available to the downstream sender during recurring arrival holes.

The repair keeps the existing first-release boundary. After it is reached,
the pinned aiortc 1.15 receiver releases at most four complete, contiguous Opus
packets per callback. Sequence ordering, the sixteen-slot bound, and missing
packet recovery remain bounded. No packet, missing sound, noise filtering, or
loss concealment is generated. Codec changes and large sequence restarts require
a fresh prefetch boundary. The existing HomeKit output task owns pacing: there
is no new timer, independent scheduler, or added reservoir.

Only CameraMediaSession opts into this receiver behavior. Other Ring receiver
callers retain their two-argument callbacks and existing assembly behavior.
Opted-in callbacks preserve original packet receipts and source timestamps,
and carry a startup marker describing the first bounded retained batch.
HomeKit accepts it only for fresh, authorized acquisition, in the current
capture epoch and after any microphone permission boundary. Shared or prewarmed
sources without this valid marker retain the conservative live-edge policy.

The normal output pacer does not accelerate merely because this intentional
initial retention makes audio older or queued. It uses the initial output's
source clock and its existing pacing task. True lateness still allows bounded
catch-up. A discontinuous output timestamp reanchors pacing, preserving the
discontinuity instead of creating an arbitrarily distant send deadline. The
existing 1.2-second expiry, 32 retained outputs, 40 KiB retention budget, native
batch limits, permission checks, and retirement ownership remain in force.
Reordered packets retain their first actual arrival time, and age checks inspect
all retained receipts rather than assuming that sequence order is arrival order.

## Qualification

The final 9.9-second payload excerpt from the verified complete diagnostic
capture was replayed at its recorded callback intervals through real aiortc,
the actual native Opus converter, AudioTrack's existing pacer, loopback SRTP,
and an independent libopus decoder. Both runs started with empty receiver and
decoder state. The [numeric run receipt](audio-retention-qualification-2026-11-08.json)
is retained in Git; the replay and original receipt are in the workspace under
`work/halo-server-audio-retention-qualification-2026-11-08/`. No camera payload is
included in the receipt.

| Measurement | Existing path | Complete release |
|---|---:|---:|
| Original RTP packets | 166 | 166 |
| Assembled/converter inputs | 162 | 166 |
| Sent 20 ms packets | 485 | 497 |
| Independently decoded samples at 48 kHz | 465,600 | 477,120 |
| Decode errors | 0 | 0 |
| Maximum output send gap | 215.53 ms | 22.44 ms |
| Output gaps above 100 ms | 5 | 0 |
| First output after replay starts | 268.80 ms | 260.61 ms |

The additional four inputs are the original receiver's retained stop tail,
not synthesized samples. All admitted source payloads were byte-identical to
the capture. Native converter lookahead/tail accounting remains separate from
transport loss. A separate 61-packet synthetic schedule with recurring 200 ms
source holes produced all 182 expected native output packets, with no initial
skip, stale disposal, or adapter restart and without exceeding existing bounds.

Focused tests cover real RTP loss/reordering/duplicates/wrap, variable packet
durations and DTX timestamp gaps, malformed payload rejection, codec transitions,
original receipts, mixed-order age expiry, delayed authorized view startup,
privacy/resynchronization rejection, epoch fencing, discontinuity recovery,
diagnostic observer compatibility and retained output pacing. Existing unmarked
startup/catch-up and source/legacy tests remain applicable.

## A/V timing and limits

Original receipt preservation intentionally changes receiver-estimated audio
phase: the former assembly receipt included source prefetch delay; the repaired
receipt describes original RTP arrival. Source PTS and negotiated RTP sample
rates remain intact. Tests exercise actual AudioTrack output and a shared video
timeline/SR instant, verifying audio's 24 kHz RTP progression and the true arrival
offset separately from viewing delay. Paired source-clock tests preserve known
A/V phase. These tests do not prove physical Apple playout synchronization.

The local replay proves removal of the observed audio output stalls for the
retained excerpt. It cannot establish full-session behavior, Ring delivery
causes, video smoothness, or physical HomeKit playback. The excerpt begins
without its original pre-window decoder state. A new controlled on-device
lifecycle remains the acceptance gate. Offline generic aiortc replay remains a
baseline model; it does not reproduce the newly enabled complete-release policy
unless explicitly configured with that adapter.
