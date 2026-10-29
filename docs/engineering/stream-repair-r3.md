# R3: owned legacy normalization and receiver attribution

This slice isolates the existing compatibility audio conversion from camera
state and Ring's event loop. It does not change the original Opus/video bytes,
source freshness rules, HomeKit clock mode, leases, codec backend or dependencies.
The synchronous three-argument raw-audio callback remains supported; the server
uses an additional private awaitable decoded-frame callback.

## Native ownership and failure timing

Each capture has an opaque pending normalizer owner. Its encoder construction,
PCM conversion, encoding, discontinuity reset and close run on two dedicated
native workers. The scheduler admits at most `MAX_NATIVE_CAMERAS` (256) owners,
with one retained job/client per owner and a finite ready queue. It tracks
actually started threads; partial worker-start failure rolls back media
admission, retains any real worker and retries only missing workers.

Encoder initialization now happens on the first owned native operation, rather
than inside synchronous lease acquisition. Acquisition can therefore succeed
before an unavailable encoder reports `audio_encoder_initialization_failed`.
The stable failure code and current-source stop behavior remain; codec readiness
is not assigned after failed or retired initialization. A late result or error
cannot publish into or fail a replacement capture.

Publication checks the exact owner, runner generation, capture epoch, attempt
and stop-event identity under short camera-state locking. Native serialization
keeps an encoder fixed for its operation. External raw callbacks run on the
original Ring loop, outside internal locks, with the operation claim retained
until the callback returns. Reentrant stop fences promptly instead of joining
its own callback/native thread.

Stop first fences admission and media state. It then joins receiver/native
ownership outside camera state. Five-second stop timeout is a retained cleanup
failure, not proof of release; replacement stays blocked. Native code cannot
be cancelled by cancelling an asyncio Future. Failed close retains its exact
owner and is retried. Cancellation and exceptions clear media-bearing Future
and traceback references after actual native completion.

## Bounded decoded and native handoff

The optional legacy lane uses the pinned aiortc1.15 per-instance decoder seam;
there is no global decoder replacement. Original encoded observation happens
before bounded legacy forwarding. Queued encoded input is limited to four
packets; current decoder and old-local allowances are additionally charged.
Decoded PCM has inclusive64-frame,48,000-sample and256KiB reservation limits,
including native batch/local, plane/copy/FIFO/result reserves and the in-flight
consumer. These are retention bounds, not a claim of one-second wall latency.

The output queue reserves ownership synchronously on the decoder thread before
aiortc schedules its coroutine. At most one notification is pending; no
unbounded collection of blocked `put` tasks retains PCM. Overflow evicts only
queued legacy work, never the active consumer. Quiescence wakes the decoder and
its reserved terminal path; public peer close runs only after the actual
decoder thread has exited, avoiding a synchronous join that needs the same
event loop to progress.

An operation waiting for a native worker has a one-second admission deadline.
Queue timeout/cancellation removes that exact queued operation and drops its
media references; an already started operation remains owned until completion.
Capacity failure is explicit for the legacy lane and leaves original capture
authority responsive. A queued old operation cannot initialize an encoder later.

Dropping PCM creates a real legacy discontinuity. Before the next accepted
decoded frame, its old encoder is closed and a fresh encoder uses validated
source PTS as the output offset. Missing duration stays missing; no silence is
inserted and time is not compressed. Unknown/backward source timing reports
`legacy_audio_timing_unavailable` without inventing coverage or failing original
Opus/video. This legacy mapping is separate from the later HomeKit A/V mapping
slice.

## Packet reception versus complete frames

R1's encoded ingress starts after aiortc jitter assembly. Cumulative public
receiver counters cannot identify the physical arrival boundary, and aiortc's
same-ID report replacement means those counters are not aggregate proof across
SSRCs. Latest jitter is an EWMA, not a maximum delay. A one-hertz timer also
cannot exclude blocking between its deadlines.

This slice adds pinned per-instance RTP-handler entry/exit brackets and one
shared64-slot ring of100ms bins for audio/video routed RTP entries and encoded
frame completions. The existing statistics task wakes every100ms while public
getStats stays at one hertz. It records fixed relative gaps, handler durations,
wakeup delay and a nullable shared-DTLS boolean, without payloads, SSRCs,
addresses, SDP, absolute times or arbitrary exception messages. Snapshots are
sanitized and invalidated by the source's diagnostic retirement revision.

These are routed/decrypted pre-jitter observations, not a kernel wire trace.
A synthetic pinned-aiortc counterexample produces a665ms completed-frame gap
with120ms maximum RTP-entry gap, continuous60ms PTS and final zero loss. The new
bins distinguish those boundaries and shared-track timing without attributing
a cause from cumulative histograms alone.

## Qualification limits

Deterministic barriers cover blocked initialization/encode, source/authority
responsiveness, constructor/encoder late errors, same-runner replacement,
queue overload, worker-start failures, cancellation, retained PCM, callback
affinity/reentry, close retry and real120ms Opus normalization. Public receiver
integration uses actual aiortc decoder queues/threads with synthetic media and
checks STOP and repeated cancellation ownership.

The physical R1 baseline measured legacy encode around2.18ms average/26.9ms
maximum, short state waits and sampled loop wakeups. Those data do not establish
legacy normalization as the cause of the owner's repeated A/V pauses. R3 is
responsiveness/ownership hardening plus better attribution; physical playback
and the remaining cadence, pacing and A/V clock repairs require their separate
qualification.
