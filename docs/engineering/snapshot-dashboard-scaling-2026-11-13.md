# Seven-camera dashboard snapshot scheduling

## Finding

After enabling and pairing all seven cameras, Apple Home tiles intermittently
reported No Response, then recovered on a later snapshot refresh. A bounded live
journal sample contained 32 `snapshot-capacity` warnings and no other snapshot
failure codes. Warning throttling means this is an observed count, not a failure
rate. All seven accessories remained paired and published; the adapter and user
service had not restarted.

`AdapterSession.snapshot()` rejected a third concurrent request across the
entire account. Apple Home can request all seven tiles together. This refusal
occurred before the Python snapshot broker, cache, or Ring reader could help.

## Repair

The adapter now admits a bounded FIFO of up to 16 owned snapshot jobs and
dispatches at most two complete operations concurrently. A slot remains owned
through image transfer and cleanup, not merely until READY. The existing
20-second whole-call deadline includes time waiting in the queue. An absolute
monotonic deadline prevents an expired job from being dispatched before its
timer callback runs.

Each queued request retains its original scope, dimensions, and reason. The
current exact camera/privacy scope is checked again before dispatch and before
returning the image. Queued cancellation sends no request or cancellation IPC.
Dispatched cancellation retains the existing bounded late-transfer cleanup
ownership. Session shutdown retires both waiting and active jobs.

Python broker, Ring-reader, JPEG-worker, bulk-transfer, cache, battery-wake, and
recording policies retain their existing limits. Genuine slow or unavailable
Ring sources can still exhaust the deadline; this change removes premature
capacity refusals when multiple otherwise healthy tiles refresh together.

## Regression evidence

- A composed test requests seven different cameras concurrently through the
  public HAP snapshot entry point, real adapter and bulk channels, Python
  snapshot broker, and isolated native JPEG worker. Every image must match its
  source, every camera must be requested once, and peak processing must be two.
- That test fails with the deployed pre-repair adapter and passes with the fix.
- Queue tests cover FIFO order, READY versus completed transfer, queued abort,
  dispatched abort and late cleanup, whole-call deadlines, a delayed timer
  callback, privacy invalidation, the total admission bound, and shutdown.
- The current-checkout adapter suite passed 275 tests. The server-compatible
  frozen source passed 272 adapter tests; its older HAP baseline intentionally
  excludes three tests belonging to an unrelated dependency upgrade.
- All 160 focused Python snapshot tests passed, including the seven-camera
  composed regression. Existing Ring SDK BasicAuth deprecation warnings remain.

## Deployment qualification

The repair candidate starts from the last deployed source manifest and overlays
only `homekit-adapter/src/session.ts`, its new queue test, and the composed
snapshot test. The HAP dependency remains 2.2.3-halo.2. Build and installed checks
use a private, signature-verified Python 3.14.9/zlib 1.3.2 runtime matching the
server's freshly measured interpreter and external-library hashes. The updated
desktop runtime is not substituted for the server runtime.

Both independent builds produced byte-identical archives. The packaged payload
differs from the deployed release only in `adapter/dist/src/session.js`.
Installed qualification passed 137 focused Python tests, including both composed
snapshot cases against the sealed Node, HAP, and adapter. Six public HAP lifecycle
probes, native media probes, and diagnostic capture/replay checks also passed.

Qualified release:
`1516fa3d2033c4dc8d3e76b21b9818ae5c93ff366169107d69a30c7265797278`.
Qualification receipt SHA-256:
`923a76cd9a6b0f674c195a345bfbbac9743edf1b1759e6b5b9d845e227e9fccb`.

The candidate was activated on the test server at selection revision 20. The
post-install check verified all seven cameras still selected, paired, listening,
and advertised; pairing files, owner configuration, and recording settings were
byte-identical. HKSV and owner warm sources remain off for all seven cameras.
Media and cleanup resource counts were zero, the service was running without an
automatic restart, and the former active release remains the verified rollback.

Post-install observation captured 13 live samples across 88 seconds. All seven
cameras remained published throughout. Snapshot processing peaked at two jobs,
cache bytes changed as images refreshed, and all resources retired afterward.
There were no snapshot warnings or error lines in the bounded post-activation
journal, no adapter restarts, and no observed cleanup failures. The owner also
confirmed that Apple Home snapshots now refresh normally during this test.

This verifies the reported dashboard symptom for this observed run. It does not
guarantee future Ring availability or that every slow-source refresh fits the
existing whole-call deadline.

Private build, qualification, and deployment evidence is under
`work/halo-server-snapshot-scaling-2026-11-13` in the task workspace. It is not
part of this repository and contains no material intended for publication.
