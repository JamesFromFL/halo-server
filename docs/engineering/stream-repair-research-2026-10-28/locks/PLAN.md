# Shared camera-state lock and diagnostic ownership plan

Research-only baseline6677086. No application/server change.

`CameraMediaSession._raw_audio` currently holds the session RLock while legacy
`NormalizedOpusEncoder.encode` performs normalization/encoding. Both
`is_lease_active` and `source_clock_snapshot` acquire that lock; live authority
checks and encoded-frame callbacks synchronously reach it. `_EncodedFrameQueue.put`
invokes those callbacks inline from the pinned aiortc receive path. Blocking can
therefore affect both source reception and HomeKit send/clock work.

The scratch probe invokes the actual `_raw_audio` and `source_clock_snapshot`
methods, injecting a120ms wait in a fake legacy encoder. Source-clock access
blocks120.15ms. This proves coupling, not actual Ring encoder duration. Python's
[asyncio guidance](https://docs.python.org/3.14/library/asyncio-dev.html#running-blocking-code)
explains why synchronous waits on an event-loop thread delay unrelated tasks.

First measure encoded-frame callback entry before source-clock and session-lock
access, admission completion, normalization duration, lock wait/hold time and
loop scheduling lag. Current received_at is assigned inside admission after
locking, so it cannot separately reveal earlier callback delay. Keep ingress
and admitted receipt separate; do not silently redefine freshness semantics.
Source jitter-buffer completion is also not identical to raw network arrival;
use pinned public receiver getStats as a separate upstream-loss/jitter view.

Proposed contained refactor after approval:

- One per-capture encoder owner and dedicated normalization serialization lock.
  Briefly snapshot generation, epoch, encoder owner and stop authority under the
  state lock. Never wait for the encoding lock or run native encoding under it.
- Encode outside the session-state lock, then reacquire state briefly and
  revalidate generation, capture epoch, exact encoder identity, stop/source
  authority and receiving demand before publishing any normalized packets.
  Stale results and exceptions cannot publish into or fail a newer generation.
- Existing encoded-original audio/recording routes, current-source checks and
  microphone admission fences remain independent. Do not remove authority
  checks to make the loop faster or make the legacy lane lazy without qualifying
  dynamic compatibility/intercom consumers.
- Stop/privacy first fences admission under the short state lock. Join an old
  normalization operation outside it before releasing its owner. A timed-out
  operation stays quarantined and blocks conflicting ownership; cancellation
  of an await cannot terminate native/thread work. No new source is admitted on
  a false cleanup claim.
- Native normalization failure on the still-current generation retains existing
  error/retirement semantics. A late error after generation change is discarded
  as retired work. Do not introduce a new process/backend merely for this plan.

Tests: block the old encoder while querying source clocks, validating a live
lease, stopping/muting, replacing source and admitting new media. Source-state
reads and revocation must remain responsive; old results must never publish.
Qualify concurrent RTSP/compatibility and direct HomeKit consumers, failure,
reentry, cancellation, close timeout, retry, shutdown and clear/reopen ABA cases.
Measure live lock time rather than assigning the current freeze to this seam.

Diagnostics should use fixed counters and bounded histograms, with updates only
on source/send hot paths. Store snapshots in bounded view-owned memory for the
existing trusted local status path (bridge/service); no per-packet log or new
unauthenticated interface. Distinguish active/completed/capability readiness and
real playback qualification. Format/sink failures and diagnostic backpressure
must not block source admission or retirement. Keep only relative timing,
negotiated media properties and fixed reason codes; omit packets/PCM, SDP,
keys, salts, account/device addresses and arbitrary exception text.
