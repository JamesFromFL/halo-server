# R5: source deadlines and video dependency recovery

## Observed defects and authorized scope

The pre-repair sender could admit an access unit (AU) using a fresh one-second
send budget even when the source packet was already old. A deterministic
132 kbps case transmitted the last packet at 581.8 ms after source receipt.
The pacing helper also sent after a simulated scheduler oversleep crossed its
explicit deadline. Finally, the old stale-frame flag skipped one P frame and
then permitted another P frame without first delivering a recovery IDR.

These are local correctness defects. They do not explain the independently
observed simultaneous audio/video quiet periods at the owned UDP receive
boundary. R5 does not claim to remove those upstream gaps.

This change keeps the existing capture, permission, source clock, RTP timebase,
native worker ownership and feedback semantics. It adds no upstream PLI API,
new connection, FFmpeg arguments or backend. Source receipt and freshness
constants are unchanged.

## Admission and wire ownership

For each AU, the original deadline is `received_at + 500 ms`. The existing
one-second AU guard can only shorten that deadline. The receipt supplied by the
current sender is the triggering encoded source packet's receipt; exact
earliest-contributor attribution for decoder reordering or multi-output native
batches remains separate R6 work. This is not a claim of that stronger
provenance guarantee.

Before encrypting, the sender dry-packetizes and charges all RTP header/payload
bytes plus the ten-byte SRTP authentication tag. At the selected Home bitrate:

```
start = max(now, previous_packet_service_end)
duration = sum(protected_datagram_bytes) * 8 / selected_bits_per_second
accept only if start + duration <= original_deadline
```

The prediction includes serialization of the final packet, not merely its send
time. Predictable rejection consumes neither RTP sequence nor SRTP indexes.
After protection or partial delivery, indexes are never rolled back.

One existing per-session send lock reserves the entire AU. Retransmissions
cannot steal packet slots between its packets. Every actual send still checks
authority, current capture epoch and the unchanged absolute deadline after
sleep and on every `BlockingIOError` retry, reserving the remaining datagram's
protected-byte serialization time in that selected-bitrate model. Pacing advances from the actual
successful send time, so oversleep cannot create a catch-up burst. Sender
reports retain their existing behavior.

These are software admission checks and a conservative service model. They
cannot prove physical NIC transmission or Apple receipt deadlines, nor exclude
thread/process preemption between the final check and the socket syscall.

Repairs retain the original AU's source deadline. A queued repair must still
refer to the exact cached ciphertext entry at actual send. Expired or cleared
entries cannot be sent after waiting for the AU lock. The existing feedback
reader awaits these bounded sends; this slice does not introduce a separate
repair worker or change Home PLI handling.

## Persistent recovery

Startup, source reset, reader resynchronization, stale output and a rejected or
partially sent AU require an IDR. P frames cannot clear this state, including
multiple outputs returned by one native feed. A successful recovery requires
actual NAL type 5, current SPS/PPS in the AU, linked SPS/PPS identifiers, and
parameters matching the selected profile, level, dimensions and progressive
8-bit YUV420 format. The sender does not trust a keyframe boolean alone.

An AU missed or partly sent clears repair state and requests the existing
adapter refresh. Only complete delivery of the qualified IDR clears recovery.
If a stale actual IDR consumed a pending native force, it rearms the request
for a fresh IDR without charging a fresh recovery attempt.
Recovery fails explicitly after three fresh but infeasible IDRs or six seconds.
The budget belongs to the view until an IDR is completely delivered: neither
same-epoch configuration changes nor true capture-epoch changes restart it.
The source timestamp origin still resets on those boundaries. This prevents
repeated capture churn from extending a failing view indefinitely. The existing
owned reaper enforces elapsed recovery even if the sender is awaiting source
input. Ordinary no-source startup retains the existing separate source timeout.

Copy mode remains limited by actual source IDR availability. This narrow repair
does not guarantee a new Ring IDR within six seconds. The adapted path can
request an actual local IDR; current-lease upstream PLI remains separate work.

## Native feasibility and limits

The frozen pre-R5 encoder's representative moving-scene probes exposed a real
compatibility constraint: a 640x360/132 kbps IDR required 458.2 ms of protected
wire service plus about 80 ms of native feed time; a 1280x720/299 kbps IDR
required 506.8 ms of wire service alone. Flat-image controls fit. A strict
deadline alone would therefore create avoidable recovery failures.

The separately reviewed native worker adjustment reserves bitrate headroom
for RTP/SRTP and reduces its VBV reservoir. Its qualification must use actual
native output, moving-scene fixtures, independent IDR decoding and exact wire
cost. It is not a promise that every possible input or source age can fit.
Age, feed latency, independent pacing backlog and wire time are additive;
250 ms of existing age plus 250 ms of additional backlog leaves no send budget
and must be rejected. No freshness limit is relaxed to make a fixture pass.

## Verification

Focused regressions cover predictable whole-AU rejection before crypto,
oversleep, blocked UDP retries, partial delivery, sequence continuity, repeated
P outputs, false keyframe labels, missing or wrong parameters, capture-epoch
changes, finite attempt/time recovery, reset budgets, independent SRTP/IDR
decoding, repair reservation and reaper retirement while source read is blocked.
Existing public HAP-to-IPC-to-SRTP, codec, cadence and ownership tests remain
required. The recorded native fixture results and full gate receipt accompany
the change outside the source tree. Physical Home/Ring improvement remains a
separate installed pilot acceptance gate.
