# Halo private HAP adapter groundwork

This package implements **S04 dependency checks, S05 contracts, S06 supervised channels and S07 stable accessory/events and S08 public live-video delegates, S09 negotiated audio/talkback and S10 snapshots**. It publishes explicitly selected development accessories only after Python qualifies native video output. Python owns Ring, UDP/SRTP and video bytes. Owner pairing management and HKSV are later slices. A `hello_ack` and the feature marker `classic-camera-control-v1` mean only that this initial private control protocol is understood.

The Python daemon remains the Ring/session owner. The intended future adapter uses the released HAP public API; it does not embed Scrypted, require Homebridge, or import HAP private paths. Stable selected camera/doorbell graphs, pairing-store preflight and state-plus-cursor event delivery are implemented. Publication stays gated on the qualified Python video catalogue. HKSV remains a later slice. No unpublished scratchpad dependency or HAP fork is used here.

## Pinned toolchain

- Node **26.12.1**; npm **12.2.1**.
- `@homebridge/hap-nodejs` **2.2.3** (Apache-2.0), exact npm integrity recorded in `package-lock.json`.
- TypeScript **6.0.3** and `@types/node` **26.2.0**.

`package-lock.json` fixes transitive versions and integrity. `.npmrc` disables lifecycle scripts, automatic audit/network extras and floating direct saves. A normal daemon launch never runs npm or resolves dependencies. The current package is private development groundwork, not a complete release installer or a bundled Node binary.

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm run check
npm run build
npm test
```

The compiled supervised entry is `dist/src/main.js`. Generated `dist/` and `node_modules/` are ignored; CI builds before Python lifecycle tests. Release packaging must carry the compiled files, locked production dependencies/runtime, license notices and independently verified resource hashes. No developer-relative import is a production resource-selection contract.

## Initial private protocol

The canonical schema and shared fixtures live in `../src/halo_server/homekit/protocol/v1/`. A four-byte unsigned big-endian length precedes strict UTF-8 JSON. Body length is 1–65536 bytes; JSON depth is at most 32, keys cannot repeat, numbers must be finite, IDs are canonical lowercase UUIDs, and envelope/payload fields and message directions are allowlisted. No Ring credentials, arbitrary commands, images or media bytes are valid in control JSON.

The child requires distinct inherited stream socket descriptors at least 3 in `HALO_CONTROL_FD` and `HALO_BULK_FD`, plus `HALO_PARENT_PID` matching its actual parent. It creates no control listener. The separate binary descriptor now carries explicitly registered typed transfers with a 32-byte header, finite global two-chunk credit, exact byte/sequence checks, and retained ownership during cancellation/cleanup. It does not carry arbitrary unregistered data or provide HAP/HDS flow control.

The first message is Python `hello`; Node echoes boot/request/build correlation with `hello_ack`. The build string is currently a parent-supplied correlation marker, **not independent proof of the compiled artifact's identity**. Actual package provenance must be verified from frozen manifest/resource hashes or a later independently checked child-local release manifest.

The shared S05 decoder also validates inventory, privacy, lease, snapshot, talkback, control and transfer metadata with scoped reply correlation. These operations are wired into bounded synthetic camera delegates without accessory publication. Inventory must paginate by encoded bytes as well as the 32-camera page limit to remain below the control byte cap.

After hello, same-boot `ping` and `shutdown` reply `pong` and `shutdown_ack`. Inventory notices discover account state and drive scoped, byte-aware pagination. Camera delegates synchronize policy before lease/control/talkback work; snapshots use the bounded S10 service. Exact duplicate complete inventory notices preserve current leases. Repeated hello, stale boot, unknown direction or malformed frame closes the channel. Shutdown acknowledgment is flushed before descriptor closure. Child startup handshake and partial-frame budgets are 5 s; partial deadlines do not extend with each received byte. Writes have a 5 s whole queue/flush budget, at most 32 pending messages and 256 KiB retained output. The parent's specified heartbeat interval is 5 s; child idle expiry is 15 s. Parent identity is rechecked periodically. EOF, signals, framing/I/O failure and parent retirement close owned channels and timers; Python owns escalation/reap and retires that boot's work.

No cancellation can undo bytes already handed to a kernel/peer. This control-only lifecycle has no actuator retries or accepted source writes to replay.

## Verification boundaries

Node's test runner uses explicit `--test-isolation=none` and one-at-a-time tests so named test results are visible under the restricted local executor. Before HAP imports, a test-only guard blocks TCP/Unix connect/listen, UDP bind/send/connect, HTTP/HTTPS, DNS, fetch and child-process execution. It records attempted operation names and fails the unit phase if any occur. This is an in-process JavaScript API guard for this test phase, not an OS-wide network sandbox for npm/builds or a future native-media process.

Tests exercise canonical Python fixtures, split/coalesced frames, exact frame limits, malformed/duplicate/deep JSON, invalid UTF-8/BOM, Unicode bounds, backpressure, deadlines, promise retirement and public CameraController/DoorbellController construction with rejecting synthetic delegates. Tests use fake publication and isolated storage fixtures; they do not start real HAP listeners or perform a Home pairing. Python `tests/test_homekit_runtime.py` separately exercises the actual compiled child over inherited private socketpairs, including lifecycle/cancellation/crash/reap and raw JSON parser agreement. Those tests require Node and the compiled entry; CI must build them first, and a release gate must not accept a skipped integration phase.

`HALO_PROTOCOL_FIXTURES` can select the same canonical fixture file when verifying an isolated clean package copy. `HALO_TEST_NETWORK_RECORD` writes the unit-phase guard result to a requested evidence file; neither is consumed by the production entry.

## Explicit later gates

- HAP's released storage uses temporary-write/rename without fsync durability, can treat invalid JSON as missing and follows file symlinks during reads. S07 now validates private pinned paths, exact records/key consistency and exclusive inherited ownership before HAP reads. Power-loss durability and revocation recovery remain S14 gates; unsafe existing state is preserved and refused.
- Released HDS `sendEvent` has no public awaited drain/queued-byte bound. IPC credits alone cannot guarantee bounded HDS output. A maintained supported extension decision and stalled-controller measurements are mandatory before reliable HKSV claims; no private socket monkeypatch is included.
- Public API imports and synthetic controller objects do not prove live SRTP, valid clips, audio-off privacy, Apple notification timing, actual source resolution, HEVC/HKSV3, real pairing, unattended install or platform support. Keep those acceptance gates separate.
- `npm audit` must be reviewed as evidence about this exact locked graph/date; do not silently upgrade HAP or hide advisories to obtain a clean result.

Primary dependency sources: [HAP-NodeJS2.2.3 npm package](https://www.npmjs.com/package/@homebridge/hap-nodejs/v/2.2.3), [released source comparison pin](https://github.com/homebridge/HAP-NodeJS/tree/25e8bea26a64309a47184dec478479483fbdd50c). The npm tarball integrity and lockfile, not an assumption about Scrypted's missing local fork, identify the installed dependency.

## S07 storage and event contract

Python supplies both `HALO_PAIRING_FD` and `HALO_PAIRING_LOCK_FD`; descriptors must
be distinct from each other and the control/bulk channels. Linux `/proc/self/fd`
pins the validated private directory. The lock is outside the HAP record directory
and remains open in both owners through process exit. Never call accessory
destruction/cleanup APIs during ordinary retirement: they erase durable pairings.

Accessory configuration is bounded to eight pages, 32 entries per page, with a
whole-assembly deadline and atomic graph replacement. Event delivery uses scoped
state plus a cursor; doorbell edges are never reconstructed from replay. Both TTL
and absolute monotonic expiry are checked, including after delayed consumption.
Python `time.monotonic()` and Node `process.hrtime.bigint()` were bracket-probed on
the qualified Linux runtime; other operating systems require qualification.

## S08 live delegates

`HapVideoDelegate` implements public PREPARE/START/RECONFIGURE/STOP. Node sends
strict scoped metadata and ephemeral keys through private IPC; video bytes remain
in Python. Two viewers share one policy synchronization rather than revoking each
other. Public stream Active changes fence affected output before awaiting cleanup.
Prepared requests expire, late success is compensated, and exact scoped terminal
notices force-stop only the matching HAP session. Shutdown keeps only owned cleanup
results flowing while new acquisition admission is closed. Tests use a fake
publisher; actual Apple pairing and delivery remain S15 device qualification.

## S09 audio and saved settings

The same public stream delegate negotiates mono Opus with classic HAP sample-rate
RTP clocks and explicit packet time, bitrate mode and bounded bitrate. Python owns
the additional UDP/SRTP context, source conversion and authenticated talkback; the
Node process transports no live media bytes. Saved microphone/speaker birth is
independent of transient source capability observations. Public Mute/Volume async
setters fence active output and wait for Python CAS/fsync acknowledgement before
updating HAP state. Publication and fresh policy wait for pending setting writes.
Unsafe/failed settings keep the graph closed until durable configuration recovers.
There is no HAP private-path patch or implicit unmute on restart.

## S10 public snapshot callbacks and isolated transfer retirement

Public HAP snapshot reasons map exactly to event/periodic; absent or unknown
reasons remain unknown. The20second callback budget includes a17second scoped
READY request and bounded JPEG transfer. Python's existing Ring downloader and
isolated native worker supply validated images. Final callback delivery checks
current scope, policy and abort token. Cancelled callers retain only exact
cleanup authorization for a late READY/OPEN and never revive a callback.

The matched child now requires `bulk-cancel-v1` in addition to the original
control marker. Producer `transfer_fence` and exact `transfer_fence_ack` account
for already enqueued frames, including a fragmented body or cancelled drain.
Normal successful cancellation preserves unrelated transfers; failed framing or
frontier deadlines still close the shared channel. Native cleanup can remain
owned after its wire bytes are drained. Terminal history contains minimal
metadata,4096 entries/60seconds, and never retains image buffers or sink closures.
These are private IPC guarantees; released HDS backpressure remains an HKSV gate.

## S11 Python recording preparation

Python now owns separate recording capture, measured pre-event history and a
killable native H.264/AAC fragmented-MP4 path. This adapter still publishes no
HKSV recording capability. Released HDS transport bounds require an independent
public API extension decision and qualification; no dependency/fork was changed.
Warm capture is not automatically activated. Native sample readiness and actual
HomeKit recording delivery readiness are reported separately.
