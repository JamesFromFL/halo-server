# Halo private HAP adapter groundwork

The supervised adapter implements classic HomeKit camera graphs, live video,
negotiated audio/talkback, snapshots, motion/doorbell events and the S12 HKSV
recording path. Python owns Ring accounts, shared capture and native media;
Node owns public HAP delegates, pairing state and bounded encrypted HDS output.
Explicit selection and native qualification gate publication. HKSV also requires
owner opt-in, video capability, current HomeKit recording
settings and four seconds of actual decodable pre-event history. It defaults off.

The adapter uses the approved narrow transport extension to pinned HAP-NodeJS.
It requires neither Scrypted nor Homebridge and imports no private HAP paths.
The `classic-camera-control-v1`, `bulk-cancel-v1`, `hksv-recording-v1`,
`owner-admin-v1` and `publication-status-v1` markers
negotiate private protocol support; they do not prove successful Apple pairing or
Home timeline playback. Owner setup and immutable release packaging live in the
parent application; [actual Apple/Ring acceptance](../docs/homekit-acceptance.md)
remains a separate device qualification gate.

## Pinned toolchain

- Node **26.12.1**; npm **12.2.1**.
- `@homebridge/hap-nodejs` **2.3.0-halo.2** (Apache-2.0), immutable vendored tarball and lock integrity. [Patch, license and provenance](vendor/) accompany the dependency; `tools/build_hap_dependency.py` rebuilds the exact official source pin plus patch.
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

- The existing HAP reader can treat invalid JSON as missing and follows file symlinks. Halo validates pinned private paths, exact records and exclusive inherited ownership before HAP reads. The approved durability correction adds fsync, a durable intent and a fatal process boundary; an incomplete intent remains refused for explicit owner recovery.
- The approved maintained extension supplies bounded public HDS output and durable storage while preserving pairing formats and upstream 2.3.0 crypto. The upstream crypto implementation changed to native Node X25519/Ed25519 and is qualified against legacy pairing fixtures; see vendor provenance. IPC credits remain separate from HDS bounds. Real Ring/Home hub acceptance remains a hardware qualification gate; no private socket monkeypatch is included.
- Public API imports and synthetic controller objects do not prove live SRTP, valid clips, audio-off privacy, Apple notification timing, actual source resolution, HEVC/HKSV3, real pairing, unattended install or platform support. Keep those acceptance gates separate.
- `npm audit` must be reviewed as evidence about this exact locked graph/date; do not silently upgrade HAP or hide advisories to obtain a clean result.

Primary dependency sources: [HAP-NodeJS 2.3.0 npm package](https://www.npmjs.com/package/@homebridge/hap-nodejs/v/2.3.0), [released source comparison pin](https://github.com/homebridge/HAP-NodeJS/tree/bd7a01b889cf1c74cbfcc4a4e5efdfa53fc3532b). The npm tarball integrity and lockfile, not an assumption about Scrypted's missing local fork, identify the installed dependency.

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

`accessory_status` reports current configuration scope/revision/generation and
bounded configured/listening/advertised counts. A publish return or retained
cleanup claim cannot prove listening: only the public HAP `listening` event does.
Only the subsequent public `advertised` event proves local advertisement. Counts
clear synchronously on invalidation, retirement or a new configuration generation;
unchanged listener evidence can be reused after that same graph is revalidated.
Incomplete pages, incomplete inventory and stale full scopes never count as a
ready graph. Status output retains at most one in-flight write and one latest
snapshot. Pairing status/reveal require advertisement evidence too, while reset
may quiesce an unadvertised retained cleanup claim. These facts prove software
publication, not a working Ring source, Apple pairing, Home hub delivery or a clip
in Apple's timeline.

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
These are private IPC guarantees; the independent HDS transport bounds are described below.

## Recording and encrypted HDS

Python owns independent recording/warm leases, the bounded prebuffer and native
H.264/AAC fMP4 workers. The child requests one scoped init/fragment at a time;
exact READY metadata authorizes the bounded bulk transfer before OPEN. One
fragment of lookahead marks the real final fragment as EOS. Trusted Ring incident
time defines pre-roll; a delayed controller request cannot relabel later footage
as pre-event history. Quiet completion includes two seconds of tail; a150second
natural cutoff precedes the180second absolute job limit. Missing history/events
fail closed. Ordinary quality changes apply to the next job; privacy/audio-off
changes immediately fence affected delivery. Audio-off MP4 has no audio track.

The shared policy preserves independent live and recording permissions.
HomeKitCameraActive disables all camera media. A unique transport AbortSignal
owns recording cancellation; a reusable numeric stream ID cannot cancel a newer
job. Wrong, early or non-EOS ACKs do not complete a recording. Missing final ACK
closes both the logical stream and its physical connection after the finite
acknowledgement interval, even if the peer keeps draining without replying. Parent durable CAS
acknowledgement is required for effective recording authority, even though the
released Active/configuration callbacks themselves return void.

Every HDS output reserves capacity before bounded serialization. The selected
limits are266240bytes/frame, two pending frames and532480charged bytes per
connection, a five-second write deadline, finite graceful/hard close,16 global
physical connections, four per camera and16 prepared sessions per camera.
Admission drops excess connections before HDS allocation. Recording DATA awaits
local callback and required drain; that proves local progress rather than peer
receipt. Abort closes the physical encrypted connection and releases ownership
only after actual close. Cryptography, framing and nonce construction retain the
official implementation. Strict recording metadata validation does not alter
other released dataSend uses.

Warm capture supports all selected video-capable cameras, including battery and
unrecognized power/model classifications. There is one warm capture and one active
recording job per camera, without a global two-camera gate. Shared bulk transfers
wait for bounded transport capacity; they do not discard a clip simply because
another camera is transferring a fragment. Failed cleanup remains owned. Finite warm
lease rollover closes the old claim before rebuffering. Readiness reports actual
history and cleanup; native sample qualification is separate from Apple HKSV
interoperability. No normal daemon launch installs dependencies or patches HAP.


## Durable pairing storage and failure retirement

The adapter and offline reset helper enable the public durability guard and
register the public fatal callback before the first HAP storage access. The
existing raw-key, compact JSON backend remains in place. Private exclusive
temporary writes are fully written, fsynced, closed, renamed and followed by a
directory fsync. Removal is followed by a directory fsync too.

Before a target changes, guarded storage durably creates `.hap-storage-intent`;
it clears and syncs that marker only after the target is durable. A retained
marker produces `storage_recovery_required` and is preserved. No daemon start,
reset retry or release rollback silently erases it. Ordinary upstream-style
consumers may leave the guard disabled, but Halo requires exclusive ownership
and always enables it.

A filesystem failure latches a typed error. Its callback synchronously closes
new admission and retires local graph authority, then immediately exits the
child. The parent owns actual EOF, native-resource retirement and process reap.
This boundary also runs if diagnostic output or a local fence throws; cached
HAP model objects never continue after storage failure. The helper likewise
cannot report a successful reset after a failed commit.

Tests cover actual child failure with a video PREPARE pending over private IPC,
parent retirement, a preserved intent and refused restart; helper failure emits
no success. Separate storage tests inject short/zero writes and failures at
write, close, rename, unlink and fsync boundaries. They verify syscall ordering
and explicit uncertainty, rather than claiming a physical power-cut test.

## Independent non-camera bridge

`device-main.js` is a separately supervised HAP Bridge process. It has one
inherited private control socket and its own pinned pairing directory and lock.
Cameras continue using their existing process, identities and pairing store.
Python resolves and verifies the complete managed release before execution and
keeps its release guard inherited by the child. The matching `build_id` handshake
checks the parent's selected session; its echoed value is not independent
artifact attestation. Development launch requires an already-built checkout.

Device IPC uses a separate version-1 schema with a 256 KiB frame limit. Each full
snapshot contains one immutable account generation and bridge UUID/username,
a monotonic registry revision and sequence, and at most 149 selected devices.
Unknown message kinds, duplicate JSON fields, malformed observations and stale
identities are rejected. No Ring credentials or media cross this channel.
Python alone owns discovery and provider access. Explicit publication opt-in is
separate from the saved import selection.

Contact, motion, leak, smoke and carbon-monoxide services expose read-only typed
observations. Missing, stale or unknown values return a HomeKit communication
error and set `StatusFault`; they never become false or safe values. Each snapshot
has an absolute monotonic deadline capped to 15 seconds after receipt. Both read
handlers and a timer enforce expiry independently of heartbeat traffic. Optional
battery and tamper service graphs are deferred until their presence can be
persisted reliably; those observations remain visible in Halo.

Ring Alarm panels add a SecuritySystem service with observed current and target
state. Controls are disabled by default. The standard target characteristic
descriptor stays stable; its setter and Python independently require explicit
control opt-in and fresh authority. Only disarmed, home and away are accepted.
Night, panic, bypass and arbitrary provider commands have no IPC route. Unknown
alarm state faults instead of reporting disarmed, and startup/identify never
actuates a device.

Qualified physical Beams lights add one binary Lightbulb service. Brightness,
embedded motion services and lighting groups are deferred. The `light_set` command
requests a finite On override or a return to automatic operation; Python owns the
configured duration and per-device control grant. Actual lamp `On` is an independent
observation: returning to automatic mode must not fabricate an Off reading.

The Lightbulb uses a stable read/write/notify/write-response descriptor. Its setter
returns confirmed actual state so HAP cannot optimistically cache the request.
Actual On/availability changes received during a write take precedence over an
older callback; unchanged pending snapshots do not. A truthful follow-up event
also covers controllers that do not request write responses. The pinned HAP
accessory serializer omits a falsy response value; false still remains correctly
cached, readable and notified. Tests exercise this complete HAP write path without
changing the pinned HAP library.

The child can request an `alarm_set` or `light_set` using its boot, account,
registry revision, accepted snapshot sequence and exact child identity. Its correlated result is
bounded and reflects provider readback, never an optimistic state change.
Observation updates continue while a command waits. Policy revocation, account
changes, expiry and authority loss invalidate pending commands; an uncertain
result cannot be retried automatically. One command per device and four in total
are admitted, shared between alarm and lighting commands. A lost child connection
does not cancel or retry a potentially
sent Ring command: Python retains ownership through bounded retirement.

Stable child UUIDs and service subtypes preserve HAP IDs across rename, reorder,
restart and explicit removal/re-inclusion. Missing selected children stay in the
graph with faults. Unused identifier purging is disabled to preserve removed
identities. Existing private-store file/cache limits still fail closed on
excessive historical churn rather than regenerating IDs.

Pairing status never includes a setup secret. Reveal requires an unpaired,
listening and advertised bridge with matching account, registry and pairing
revision. Reset preparation permanently fences that process before unpublishing,
then checks the unchanged pairing manifest again. After the parent has reaped the
process, `device-pairing-reset.js` reuses the existing exact-namespace,
manifest-CAS cleanup primitive under the inherited exclusive lock. It never
resets cameras or another account's bridge.

Network-denied Node tests cover sensor, alarm and lighting mappings, unknown/stale reads,
identity and capacity boundaries, AID/IID stability, publication failure,
pairing/reveal/reset races, framing, bounded output and observed command confirmation.
Python tests exercise command/reply races through an actual private socket,
including authority revocation, duplicate commands and child-loss retirement.
Python tests additionally
exercise the actual reset helper against private synthetic files, checking
camera/other-account byte preservation, stale-manifest rejection, crash resume,
lock ownership and retained storage-failure markers. These tests do not claim
live Ring or Apple Home validation.
