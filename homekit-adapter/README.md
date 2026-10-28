# Halo private HAP adapter groundwork

This package implements **S04 dependency/public-API checks, S05 private contracts and S06 synthetic control lifecycle**. It does not publish an accessory, open a HAP/mDNS listener, initialize pairing storage, authenticate to Ring, deliver media, or provide a working camera/HKSV integration. A `hello_ack` and the feature marker `classic-camera-control-v1` mean only that this initial private control protocol is understood.

The Python daemon remains the Ring/session owner. The intended future adapter uses the released HAP public API; it does not embed Scrypted, require Homebridge, or import HAP private paths. Stable accessories, protected pairing stores, privacy/media delegates, snapshots, live transport and HKSV are later slices. No unpublished scratchpad dependency or HAP fork is used here.

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

The child requires distinct inherited stream socket descriptors at least 3 in `HALO_CONTROL_FD` and `HALO_BULK_FD`, plus `HALO_PARENT_PID` matching its actual parent. It creates no control listener. The reserved bulk descriptor currently accepts no data. S05 defines and cross-validates its separate 32-byte typed binary header and finite credit/size limits; S06 must implement the actual sender, ownership and credit/cancel state machine before use.

The first message is Python `hello`; Node echoes boot/request/build correlation with `hello_ack`. The build string is currently a parent-supplied correlation marker, **not independent proof of the compiled artifact's identity**. Actual package provenance must be verified from frozen manifest/resource hashes or a later independently checked child-local release manifest.

The shared S05 decoder also validates inventory, privacy, lease, snapshot, talkback, control and transfer metadata with scoped reply correlation. These additional shapes are not wired into the synthetic child. Inventory must paginate by encoded bytes as well as the 32-camera page limit to remain below the control byte cap.

After hello, only same-boot `ping` and `shutdown` are accepted from the parent, replying `pong` and `shutdown_ack`. Repeated hello, stale boot, unknown direction or malformed frame closes the channel. Shutdown acknowledgment is flushed before descriptor closure. Child startup handshake and partial-frame budgets are 5 s; partial deadlines do not extend with each received byte. Writes have a 5 s whole queue/flush budget, at most 32 pending messages and 256 KiB retained output. The parent's specified heartbeat interval is 5 s; child idle expiry is 15 s. Parent identity is rechecked periodically. EOF, signals, framing/I/O failure and parent retirement close owned channels and timers; Python owns escalation/reap and retires that boot's work.

No cancellation can undo bytes already handed to a kernel/peer. This control-only lifecycle has no actuator retries or accepted source writes to replay.

## Verification boundaries

Node's test runner uses explicit `--test-isolation=none` and one-at-a-time tests so named test results are visible under the restricted local executor. Before HAP imports, a test-only guard blocks TCP/Unix connect/listen, UDP bind/send/connect, HTTP/HTTPS, DNS, fetch and child-process execution. It records attempted operation names and fails the unit phase if any occur. This is an in-process JavaScript API guard for this test phase, not an OS-wide network sandbox for npm/builds or a future native-media process.

Tests exercise canonical Python fixtures, split/coalesced frames, exact frame limits, malformed/duplicate/deep JSON, invalid UTF-8/BOM, Unicode bounds, backpressure, deadlines, promise retirement and public CameraController/DoorbellController construction with rejecting synthetic delegates. Tests do not call `publish`, start storage or perform a Home pairing. Python `tests/test_homekit_runtime.py` separately exercises the actual compiled child over inherited private socketpairs, including lifecycle/cancellation/crash/reap and raw JSON parser agreement. Those tests require Node and the compiled entry; CI must build them first, and a release gate must not accept a skipped integration phase.

`HALO_PROTOCOL_FIXTURES` can select the same canonical fixture file when verifying an isolated clean package copy. `HALO_TEST_NETWORK_RECORD` writes the unit-phase guard result to a requested evidence file; neither is consumed by the production entry.

## Explicit later gates

- HAP's released storage uses temporary-write/rename without fsync durability, can treat invalid JSON as missing and follows file symlinks during reads. A protected-path/corruption/revocation policy and supported storage hooks need qualification before S07 persistence claims. This package does not initialize that storage.
- Released HDS `sendEvent` has no public awaited drain/queued-byte bound. IPC credits alone cannot guarantee bounded HDS output. A maintained supported extension decision and stalled-controller measurements are mandatory before reliable HKSV claims; no private socket monkeypatch is included.
- Public API imports and synthetic controller objects do not prove live SRTP, valid clips, audio-off privacy, Apple notification timing, actual source resolution, HEVC/HKSV3, real pairing, unattended install or platform support. Keep those acceptance gates separate.
- `npm audit` must be reviewed as evidence about this exact locked graph/date; do not silently upgrade HAP or hide advisories to obtain a clean result.

Primary dependency sources: [HAP-NodeJS2.2.3 npm package](https://www.npmjs.com/package/@homebridge/hap-nodejs/v/2.2.3), [released source comparison pin](https://github.com/homebridge/HAP-NodeJS/tree/25e8bea26a64309a47184dec478479483fbdd50c). The npm tarball integrity and lockfile, not an assumption about Scrypted's missing local fork, identify the installed dependency.
