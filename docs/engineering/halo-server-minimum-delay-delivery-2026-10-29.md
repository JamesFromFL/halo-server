# Minimum-delay Ring delivery investigation

The owner chose minimum delay and investigation of Ring's delivery path before
adding a shared audio/video reservoir. The proposed 1.2-second reservoir remains
design work only. No intentional playback delay, alternate Ring backend,
noise filtering, media duplication or unsolicited speaker activation is added.

R7a (`225b488`) separately removes the native encoder's mandatory pending
picture and measures source cadence from real decoded timestamps. Its full
gate passed 2,473 Python tests and 265 Node tests, with one host IPv6 skip.
Physical playback acceptance remains open.

## What the completed R6 pilot establishes

The owner reported clear sound between interruptions, continuing stutters, and
working previews. The capture contains one continuous source attempt. Incoming
audio/video quiet intervals recur roughly every two seconds, with about 650 ms
median duration. Those gaps already appear at the owned UDP callback boundary,
before decryption, frame assembly and HomeKit conversion. That callback is not
a kernel packet-arrival timestamp.

The dedicated Ring event loop recorded 1,390 sleeps of 100 ms over 141.735
seconds, 1.754882 seconds of cumulative wake lateness and 0.066077 seconds of
statistics collection. This leaves at most about 0.914 seconds for startup and
unmeasured observer work. A 650 ms blocking callback every two seconds would
consume roughly 45 seconds and is incompatible with that count/time evidence.
This materially disfavors a repeating Halo receive-loop stall; it does not
locate the delay within Ring, the network or the kernel.

The Home sender reads separate compressed buffers. It does not normally apply
backpressure to the socket. Measured frame admission and RTP handling are short.
No sampled UDP drops or Home receiver loss were observed, but sampling does not
prove that every packet or interval was loss-free.

## R7b: passive observations on the existing connection

The pinned upstream Ring client defines `stream_info`, including a `transcoding`
boolean and a reason such as `codec_mismatch`. The pinned Python SDK discards
that method. R7b observes the existing SDK instance without changing its offer,
activation, audio sender, media transport or original message handling.

Only finite status fields, bounded counters and observation age reach owner
diagnostics. Transcoding remains unknown until a valid report arrives. Missing
reports are not evidence that transcoding is disabled. Observation coverage
distinguishes installation before the signaling reader from a later install.
Raw messages, SDP, session identifiers, tickets, addresses and reason text are
not saved by the observer. Retirement prevents observations from an old source
attempt appearing current.

Receive coverage is also checked against the transports actually selected after
the answer. The old count of two gathered transports describes the installation
set; the live bundled audio/video receivers share one transport. The new check
verifies current object and hook ownership, including the nominated protocol,
without exporting their identities or addresses. Unsupported or replaced
objects cannot inherit a successful coverage claim.

The external lifecycle collector additionally samples daemon-owned UDP queue
and drop counts at nominal 100 ms intervals. It records actual sample times and
gaps. Owner status collection can delay samples; the counts include other UDP
sockets belonging to the daemon. It neither reads packet contents nor changes
socket options. This can reveal queue buildup inside the host, but is not a
wire capture or proof of a particular upstream cause.

## Interpretation and next experiments

1. Run one closed → open → at least two minutes live → closed lifecycle with
   R7a and R7b together. Check picture/audio observations, original-frame expiry,
   source quiet intervals, Ring stream metadata, transport coverage, queue
   counts, preview refresh and resource cleanup.
2. If Ring reports codec-mismatch transcoding, qualify one codec-preference
   change using the existing supported H.264 capability set. Keep transport,
   activation, audio policy and Home output unchanged for that comparison.
   Transcoding alone would not establish that it causes the periodic gaps.
3. If the report is absent or does not distinguish the cause, use the queue and
   source-loop evidence to choose the next measurement. Queue growth during
   callback silence supports host-side waiting; empty sampled queues with
   responsive loops favors a delay before the host, subject to sampling gaps.
4. Further candidates include idle outbound silent RTP and bundled versus
   separate transports. Each requires a native preflight and a separate,
   controlled comparison. A blind sender `replaceTrack(None)` toggle is unsafe
   as an experiment because restoring the same track can create catch-up
   bursts. No such experiment is enabled in R7b.

The reference client and the Ring application may use different source paths
and buffering. Smooth Ring-app playback does not establish equal route or
latency. No documented low-latency switch was found in the inspected WebRTC
path. The earlier activation-options parity change did not remove the gaps.

## Primary references

- [Ring stream message schema at the pinned Homebridge reference](https://github.com/dgreif/ring/blob/dde2a6d14b078145218b5c1dab305e4573bb36cd/packages/ring-client-api/streaming/streaming-messages.ts)
- [Ring reference signaling](https://github.com/dgreif/ring/blob/dde2a6d14b078145218b5c1dab305e4573bb36cd/packages/ring-client-api/streaming/webrtc-connection.ts)
- [Python SDK 0.9.14 reader and dispatch](https://github.com/python-ring-doorbell/python-ring-doorbell/blob/0.9.14/ring_doorbell/webrtcstream.py)
- [aiortc 1.15.0 sender ownership and draining](https://github.com/aiortc/aiortc/blob/1.15.0/src/aiortc/rtcrtpsender.py)

Detailed source comparisons, synthetic probes, lifecycle measurements and
qualification receipts remain in the private repair workspace. Those sources
are pinned references, not claims about the newest release or physical
acceptance of another platform.
