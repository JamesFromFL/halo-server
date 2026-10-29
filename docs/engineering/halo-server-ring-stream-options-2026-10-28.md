# R4d: post-activation Ring stream options

The owner observed smooth playback in the Ring app while Halo's R4b capture had
76 joint incoming audio/video RTP quiet periods in 157 seconds: median 700 ms,
approximately two seconds apart. R4b prevented generated audio expiry, but did
not change incoming cadence. This slice is a single-variable signaling
compatibility candidate, not a demonstrated remedy for that receive pattern.

The pinned [koush/ring activation implementation](https://github.com/koush/ring/blob/516e96a24ec279168c246795e623b6bfdf58ec45/packages/ring-client-api/streaming/streaming-connection-base.ts#L97-L104)
sends `activate_session`, then `stream_options` with audio/video enabled. The
installed [Python SDK 0.9.14](https://github.com/python-ring-doorbell/python-ring-doorbell/blob/0.9.14/ring_doorbell/webrtcstream.py#L163-L197)
includes those same booleans in its initial `live_view` request, but sends only
`activate_session` after receiving the answer. These are reverse-engineered
implementations; neither source establishes an official Ring requirement.

Halo now reasserts the original fixed stream options once per source attempt.
There is no vendor edit or global monkeypatch. The helper is version/shape
guarded and uses the existing per-device SDK stream registry. It waits for a
known Ring session and the SDK pinger-task marker, which 0.9.14 creates only after
the activation write returns. Waiting for the answer event alone would race:
the SDK invokes Halo's answer callback before awaiting activation.

The caller retains the negotiation coroutine and its cleanup. The helper creates
no task or connection. A short serial owner lock prevents duplicate writes;
timeout/cancel/failure fences the owner permanently, so an ambiguous send cannot
be replayed. Exact registry, stream, socket, session and current-attempt authority
are checked. STOP immediately fences pending admission; an already-admitted
socket write remains owned until joined. Intentional STOP is not recast as a
negotiation failure. A write returning after a swallowed timeout cannot report
success.

The existing source startup deadline bounds this negotiation. Source retirement
still cancels/joins its owned tasks and closes the SDK stream and peer. The
five-second lease-renewal path does not call the helper. Additional consumers do
not create another source command; a new actual source has its own owner.

There is one fixed diagnostic result per receive attempt: `sent`, `already_sent`,
`unsupported`, `contract_unavailable`, `retired`, `timeout`, or `send_failed`.
`sent` means the existing SDK socket send returned, not that Ring acknowledged or
applied the options. No envelope, IDs, address, SDP, ticket or raw exception is
included in that result.

Tests exercise the actual SDK answer-callback-before-activation sequence through
the Halo receiver, held activation writes, duplicate admission, stop/fence,
same-key source replacement, missing activation, unsupported SDK/changed shape,
send failure, cancellation/timeout, swallowed cancellation, lease renewal and
new-source ownership. Existing receiver/decoded/native retirement tests remain
part of the focused gate. Signaling is entirely synthetic in memory.

The controlled paired pilot should verify `result=sent`, then compare at least
120 seconds of the same receive bins and source/output ledgers against R4b, with
owner observations and clean stop. Preserve pairing and HKSV off. Do not combine
the pilot with BUNDLE, SDP, codec, endpoint or buffering changes. A repeated A/B
comparison is needed before assigning causality; unchanged bursts would not
justify a broader protocol change automatically.
