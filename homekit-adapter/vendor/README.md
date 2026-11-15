# Halo's bounded HAP transport and durable storage dependency

`hap-nodejs-2.3.0-halo.2.tgz` is a private, immutable build of official
[`@homebridge/hap-nodejs` 2.3.0](https://github.com/homebridge/HAP-NodeJS/releases/tag/v2.3.0),
based on commit `bd7a01b889cf1c74cbfcc4a4e5efdfa53fc3532b`. It is installed by
the adapter's ordinary `npm ci` through a local file dependency and lock integrity.
It is not an official upstream release and is not published to npm.

The adjacent patch, provenance manifest and original Apache-2.0 license describe
the complete divergence. This rebases the existing Halo transport and durable
storage extension onto upstream 2.3.0, including its ESM loading, HAP/HDS input
validation and encrypted TCP frame fixes. The older 2.2.3-halo.2 artifacts remain
available for rollback review; rollback of a deployed release still requires
compatible paired-state and release ownership checks.

The `halo.2` revision adds one notification scheduling change: observed
`SecuritySystemCurrentState` transitions use immediate HAP event delivery.
Other status events retain upstream's 250 ms batching. Duplicate unchanged
alarm observations stay quiet; distinct transitions retain their order even
while an HTTP response temporarily prevents event writes. The permission and
subscription checks are unchanged. This reduces local delivery delay; it does
not select Apple notification severity or prove a notification reached a phone.
Home app notification permissions, home hub connectivity, and Apple delivery
remain outside the bridge's control. Entry delay is not presented as a triggered
alarm, and unavailable observations remain faults rather than safe states.

Two clean offline builds of `halo.2` matched exactly. Its archive differs from
`halo.1` only in package version, `Accessory.js`, and that module's two source
maps; every other installed file is byte-identical. All 328 adapter tests pass
with zero network attempts, including five new regressions through the installed
bridge dispatch, event queue, and EVENT/1.0 serializer using inert socket fixtures.
The complete upstream test suite was not rerun for this one-condition change;
the detailed upstream qualification below belongs to the retained `halo.1`
provenance. No physical alarm action, live bridge event, or Apple alert test was
performed. Both older immutable archives remain available for recovery review.

Upstream 2.3.0 replaces tweetnacl pairing crypto with native Node X25519/Ed25519.
The stored secret remains the existing 32-byte seed plus 32-byte public key.
The fork leaves the complete upstream 2.3.0 `hapCrypto.ts` and HDS encryption
block unchanged. Qualification covers RFC vectors, tweetnacl compatibility in
upstream tests, loading and signing with an old pairing-store fixture, invalid
keys/signatures, split encrypted TCP headers, and independent HDS ciphertext.
This is not a claim that crypto implementation is unchanged from 2.2.3.

## Added transport guarantees

- `DataStreamConnection.sendEventAsync` waits for the local write callback and
  required drain. This proves local writable completion, not an Apple receipt.
  `SendOptions.signal` aborts the entire encrypted physical connection after
  enqueue; dropping arbitrary ciphertext while retaining that connection is unsafe.
  `onQueued` runs only after a successful local enqueue and supplies the recording
  sender's EOS acknowledgement fence.
- All legacy events, requests and responses share the same finite writer.
  Admission occurs before serialization. Excess awaited sends reject without a
  waiter queue or retained message. A rejected legacy control send retires the
  connection so a silently lost control response cannot leave it half open.
  Encoding failure retires the connection because a nonce may have been consumed.
- `closeAndWait` has a finite graceful interval and hard-destroy fallback;
  `abortAndWait` destroys immediately. Reservations remain charged until actual
  socket close. A close deadline reports cleanup pending and preserves ownership.
- `PhysicalConnectionBudget` sets every registered server's public
  `maxConnections` to zero when full, before another HDS object is constructed.
  It restores admission only after actual close. Node 26.12.1 is qualified; this
  relies on the [Node >=21 zero-limit behavior](https://nodejs.org/api/net.html#servermaxconnections).
  Cluster, socket transfer and worker distribution are not supported by Halo.
- Outbound serialization rejects cycles, proxies and accessors, and bounds
  depth, nodes, keys, strings and data bytes before creating entry arrays or
  growing encoder buffers. RecordingManagement separately validates its fixed
  recording chunk shape, preserving generic legacy dataSend audio semantics.
- RecordingManagement awaits every 256 KiB chunk, preserves initialization
  metadata across every initialization chunk, and validates ACK stream identity,
  EOS and actual enqueue. Numerical stream IDs cannot be reused on the same
  physical connection; at 1,024 used IDs it retires that connection. A fresh
  physical connection can reuse an ID. Missing final ACK retires the physical
  connection after 12 seconds. A full writer cannot bypass the synchronous
  logical close fence, and a rejected OPEN never starts the native generator.
- `CameraController.closeRecordingStreams` fences the logical delegate first,
  then awaits real physical retirement. Existing reusable services and saved
  pairing identities are preserved.

Halo configures 16 shared physical owners across at most 256 HDS servers, four
connections and 16 prepared sessions per server. Each writer admits at most two
266,240-byte frame reservations (532,480 bytes), with a five-second write deadline,
one-second graceful interval and one-second physical close observation. Generic
traversal permits depth 16, 4,096 nodes, 1,024 keys, 262,144 UTF-8 string bytes
and 1,048,575 buffer bytes. Encoded frame ceilings apply separately.

These are owned queue/admission bounds, not a hard RSS ceiling. Native source
history, complete recording fragments, serializer/crypto temporary copies,
inbound frames, kernel TCP buffers and allocator behavior have separate budgets.
Real Home hub compatibility and end-to-end Ring acceptance require the later
hardware qualification; synthetic tests do not claim those results.

## Existing storage durability and explicit recovery

This version retains HAPFileStorage, raw filenames and bare compact JSON. It
adds no database. Every mutation writes an exclusive no-follow 0600 temporary
file, fully writes UTF-8 with finite progress, fsyncs and closes it, renames it,
and fsyncs the pinned directory. Removal fsyncs the directory as well.
Filesystem mutation failures latch HAPStorageError; cached reads and further
mutations refuse the same error. Argument/serialization failures before I/O
preserve the old readable cache and target.

Halo must call the public HAPStorage.setFailureHandler(handler) and
HAPStorage.setDurabilityGuard(true) before the first model/storage access.
The synchronous handler must close acquisition, fence all cached model authority,
and terminate the child; its parent owns actual exit/reap. Throwing alone does
not retire already held AccessoryInfo or active HAP connections. Direct storage
users can supply equivalent HAPFileStorage construction options. Handler and
guard are optional for unrelated upstream-style consumers; the guard defaults
off because a fixed directory marker requires exclusive ownership, unlike
upstream child bridges that may share one folder across processes.

Guarded writes durably create `.hap-storage-intent` before touching the target:
marker file fsync, directory fsync, target write/removal and sync, marker unlink,
then directory fsync. The marker contains only a version and operation name.
Any existing marker refuses initialization; incomplete markers are preserved,
never repaired or removed automatically. Halo's existing private preflight also
refuses this unexpected entry before HAP can load state. A post-mutation failure
cannot silently reload an older revoked pairing after restart. Once the target
is durable, failure clearing/syncing the marker still fails closed, although the
confirmed target itself is already safe. A marker does not identify a target;
owner recovery must not guess that an arbitrary camera reset can clear it.

The local extension preserves target serialization and pairing key format and
does not alter upstream 2.3.0 crypto. The backend
retains upstream's generic read semantics; Halo's strict pinned-file/JSON/key
preflight remains mandatory. The qualified environment is Linux/Node26.12.1
with supported local filesystem fsync semantics. Initial private-directory
creation and state/release rollback authority are installation/launcher gates.
Tests prove syscall ordering and injected failures, not survival of faulty disks
or a physical power-cut experiment.

The storage fault suite covers short and zero-progress writes, all meaningful
sync/rename/remove/close failure points, durable removal intent, startup refusal,
private permissions, existing symlinks, redacted callback errors and the first
fatal latch. One official rename-failure assertion intentionally changes from
reading undefined to requiring the latched error. No ordinary unpublish or
selection operation calls destructive reset.

## Reproduce and maintain

With the qualified Node 26.12.1/npm 12.2.1 toolchain:

```sh
python tools/build_hap_dependency.py --fetch --test
```

Or use an existing official clone with `--source /path/to/HAP-NodeJS`. The builder
archives the immutable commit, ignoring local changes, applies the reviewed patch,
uses the official dependency lock with install scripts disabled, compiles, and
normalizes tar/gzip ownership and timestamps. It does not patch the installed
runtime. Two clean builds produced identical bytes; their hash is in provenance.

The supported-host upstream run passed 1,030 tests in 33 suites, including the
retained 18 durability tests and new upstream input/crypto tests. The complete
upstream run retains the seven `eventedhttp.spec.ts` cases that cannot connect to
this host's unavailable `::1`; `--test` does not silently filter them. All 13
HTTP transport tests additionally passed using a temporary IPv4-only test copy;
its single IPv6 local-address assertion was omitted and remains unqualified.
The published patch does not change those IPv6 tests.

The upstream HDS test socket now implements pause/resume, write completion and
physical close, and its close tests flush asynchronous fake timers. These are
fixture updates for the bounded writer, preserving all 15 upstream HDS assertions.
One original storage rename-failure assertion requires the retained fatal latch.
Modified dependency sources lint clean and production/test TypeScript checks pass.

Halo's adapter passes 268 tests with its network guard recording zero attempts,
including three additional installed-artifact regressions for legacy key use,
OPACK binary/back-reference semantics and split encrypted control frames.
Two clean builds match byte-for-byte, and all 283 installed files match the
archive after a fresh `npm ci --ignore-scripts`; lock SHA512 is verified.
No physical Home hub, paired controller or live Ring acceptance was performed.

When updating, review the transport and storage divergence, increment the local suffix,
regenerate provenance and the adapter lock, rerun upstream and Halo tests, and
retain pairing/crypto compatibility checks. Follow official upstream releases
and remove the divergence once equivalent supported APIs are qualified. Do not
add unrelated backend or dependency changes; review storage and transport independently.
