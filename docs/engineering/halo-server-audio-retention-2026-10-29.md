# Audio continuity across incoming pauses

The shared original-receipt audio age limit is now 1.2 seconds, previously 500 ms.
This is a retention and admission bound, not an intentional playout delay. It
applies to input contributions, converted output and the finite queue-capacity
wait. Output still carries its original contribution receipt and actual sample
timestamp. The existing 32-packet / 40 KiB retained-output limits, finite packet
and byte pacing, and immediate permission, epoch and STOP fences remain.

The preceding capture observed incoming audio quiet intervals up to about one
second. The native converter retains a short contribution tail between inputs;
expiring that tail after 500 ms repeatedly recreated the converter and lost
priming/tail samples. Simply enlarging a history-only bookkeeping window would
not establish safe output expiry, so this patch explicitly changes the shared
bound and tests both its admission and expiry behavior.

## Evidence

A synthetic 12.72-second mono wideband SILK/60 ms source replayed the recorded
100 ms arrival-count bins through the actual converter, AudioTrack, local SRTP,
and an independent libopus decoder, while a native video encoder was running.
No camera audio or video was recorded. These bins reproduce an approximation
of arrival cadence, not exact physical packet timing.

| Measurement | 500 ms limit | 1.2 s limit |
| --- | ---: | ---: |
| Converter recreations | 6 | 0 |
| Independently decoded 20 ms outputs | 629 | 635 |
| Sent source duration | 12.58 s | 12.70 s |
| Unexpected RTP sample steps | 6 | 0 |
| Maximum observed send gap | 844 ms | 779 ms |

The final 20 ms native tail is explicitly unflushed at STOP. The remaining send
gaps demonstrate the limit of this change: it removes avoidable local losses,
but does not create media while the upstream feed is quiet. No time stretching,
noise suppression, gain control, invented silence, denoising or filtering is
introduced.

Deterministic boundary tests preserve exact encrypted payload content and prove
that a 750 ms or 1.19 s old contribution can send without additional delay,
whereas output beyond 1.2 s expires without consuming a cryptographic index.
Retained output remains immediately blocked by privacy, epoch change and STOP.
Ordinary fresh 60 ms batches still depart at 0, 20 and 40 ms. Native regular,
600 ms and 900 ms burst cases require zero recreations, exact output accounting
and uninterrupted RTP sample steps. A 1.2 s burst case verifies bounded recovery
and complete accounting after the finite retention window.

## Tradeoff and qualification

Under a delayed incoming burst, this permits up to 700 ms more original-receipt
age than the old policy. It does not promise a maximum physical playback age,
continuous Apple playback, or Ring parity. Video retains its separate 500 ms
new-access-unit deadline. A new complete Apple Home lifecycle test must evaluate
joint playback and lip synchronization after the complete receiver repair is
installed. Existing pairing and recording preferences are unaffected.
