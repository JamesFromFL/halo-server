# Direct HomeKit implementation status

Engineering record, 2026-10-28. Foundation committed locally as
`798b7173ad9e8fbcbe655eb692a709b17cafcc87`; S05 builds on that commit.

The approved architecture keeps Python as the sole Ring account, push-event,
shared capture and speaker owner. A supervised Node adapter uses released public
HAP APIs for HomeKit. Native media will be delivered directly from Python rather
than through RTSP. The existing plugin/RTSP integration remains functional during
development. Direct HomeKit is not ready to pair or use with cameras.

## Implemented foundation

- S01: transactional SDK inventory refresh preserves surviving wrappers, retires
  confirmed replacements, and keeps the last good inventory on partial failure.
  Credential restoration is fenced before disk reads. Account publication has a
  runtime authority scope. Old observations and replay cursors are retired on
  account changes; an event cannot be relabeled into a replacement account.
- S02: pending acquisitions retain admission until worker-thread completion and
  compensating release. Retiring receivers remain owned after a stop timeout.
  Speaker and live consumer budgets apply at the shared lease coordinator.
  Per-attempt media reset/fencing and useful-video startup/stall detection prevent
  audio, signaling success, or nondecodable video from masking a failed feed.
- S03: plugin revocation reaches the actual queued operation admission and each
  private delivery. Requests and connection retirement are tracked and bounded.
  Snapshot limits apply during authenticated HTTP reads, not after an eager SDK
  download. Administration uses bounded request/response framing and joined
  connection cleanup. External TUI text is literal; invalid timestamps, stale
  account rows and transient revealed bearer tokens are handled safely.
- S04 groundwork: compatible security dependency refresh, exact native media
  pins and explicit SRTP dependency; pinned released HAP/Node/TypeScript graph.
  Runtime and tooling advisory applicability are recorded separately in CI.
  The selected pair is aiortc 1.15.0 / PyAV 17.1.0, with exact guarded contracts
  revalidated in an isolated variant before adoption. PyAV 19 is outside aiortc's
  supported range and drops the project's Python 3.11 floor.
- S05: validated operating modes and revision-checked private configuration;
  bounded immutable inventory, source format, capability evidence and privacy
  inputs; and one native service facade over the existing account/capture/speaker
  owners. Native live, snapshot, talkback and control admission are scoped by
  account, source wrapper, adapter boot and privacy generation. Capture consumers,
  timestamp math, the Ring receiver and direct imports remain GI-free. Private
  control v1 defines strict scoped requests/replies and separate typed binary
  headers; public plugin v2/v3 schemas and fixtures remain unchanged.
- S06 groundwork: inherited private socket pairs, Python ownership of one Node
  process, handshake, bounded requests, heartbeat, crash budget, boot rotation,
  cancellation-safe startup and joined TERM/KILL cleanup. The synthetic adapter
  imports public HAP exports but does not initialize storage or publish anything.

Initial Ring restoration runs in an owned background task so local administration
is available while the network is slow. Pending authentication is retired during
shutdown. OS-level filesystem operations and cancellation-suppressing native work
cannot be force-killed as Python threads; ownership is retained and incomplete
cleanup is reported rather than admitting another upstream.

## S05 service contract and operating modes

`compatibility` remains the missing-configuration default and enables the plugin
and RTSP listeners. `homekit` requests only the native path, `dual` requests both,
and `ring-only` enables neither media integration. The CLI accepts `daemon --mode`
as an override for that launch. Overrides do not silently save settings. The
private version-1 `configuration.json` store uses revision compare-and-swap,
owner-only regular files, no-follow directory traversal, atomic replacement,
file/directory fsync and visible corruption/conflict/uncertain-durability errors.
The future settings interface will use this store; no TUI editor is added here.
`homekit` and `dual` currently report `not_ready` with
`native_adapter_wiring_pending`; choosing them cannot publish an accessory yet.

The facade starts with no selected cameras and unsynchronized, deny-all policy.
A current adapter boot explicitly selects current source scopes, then applies
policy. Every policy change advances privacy generation and retires existing
claims. Logout/replacement detaches the boot. Inventory refresh reconciles old
wrappers, while metadata-only changes preserve their generation. Account display
names, Ring credentials and SDK objects never appear in native inventory.
Observed media format and audio capability stay unknown until measured; capability
labels distinguish SDK reports from Halo's exact-kind compatibility overrides.
The SDK's `subscribed` flag is not used to claim network reachability.

Initial native limits are two live handles per camera, eight total, two snapshots,
two controls, and one speaker globally. The speaker also shares the existing
per-camera exclusion against plugin talkback. Native and legacy live claims share
one capture registry. Admission is checked after locks, when queued Ring work
starts, before snapshot HTTP retries/fallback, and before read/renew/write results
are delivered. Audio filtering advances the encoded cursor even when audio is
withheld. Cancelling an acquisition joins late mutation and compensating release;
cancelling a task before its first instruction also releases its reservation.
A cleanup failure remains reserved and visible instead of reopening its source.
Logout still attempts credential removal when resource cleanup fails. A command
already handed to Ring cannot be undone; uncertain accepted controls are reported
conservatively and are never retried automatically.

Private control JSON is capped at 64 KiB and carries metadata only. The binary
contract has a fixed 32-byte versioned header and typed snapshot/init/fragment/PCM
transfers, with 64 KiB chunks and a two-chunk/128 KiB credit window. These are
validated contracts, not an operational bulk sender. In particular, a 32-camera
page can exceed the control-byte limit with valid Unicode metadata; S06 must
paginate by encoded bytes as well as camera count. The synthetic child continues
to accept only lifecycle operations and rejects media operations as unsupported.

## Account continuity and migration

Credential format v3 adds a durable, private UUID for one authenticated profile.
Existing v1/v2 records migrate transactionally; token refresh and ordinary restore
preserve the UUID. The current SDK adapter has no verified canonical Ring account
principal. A successful interactive login therefore creates a fresh profile even
when the email matches. An email or camera API ID alone never authorizes reuse of
future HomeKit identity. Explicit reassociation belongs in the pairing/settings
work before accessories can be published.

Older binaries cannot read v3 credentials. Source changes and test migrations do
not migrate the user's actual account file. A future installer must check state
compatibility before rollback. It must not restore an old secret-state snapshot
that could revive revoked pairings/controllers.

## Remaining implementation

S04 still needs the final distribution/runtime capability gates. S05 is complete
as the shared service/configuration/contract slice. S06 still needs production
adapter wiring, owned request maps, policy propagation across IPC, bounded bulk
credit/cancel machinery and byte-aware inventory pagination. A handshake
echoed build ID verifies correlation, not independent artifact provenance; frozen
resource hashes and packaged-manifest verification belong in S14.

S07–S15 remain: durable camera/doorbell accessories and selection; immediate source
motion with resynchronization; actual H.264 RTP/SRTP/RTCP; negotiated audio and
shared talkback; bounded JPEG decode/resize/cache; timed recording samples and an
isolated fMP4 worker; HDS delivery; CLI/TUI management; packaged installation and
rollback; authorized Ring/Apple Home acceptance and measured performance.

JPEG download byte limits do not establish decoded pixel or CPU limits. Network
read-ahead and bounded temporary copies do not establish a process RSS ceiling.
The later image-worker and bulk-channel slices must preserve these distinctions.

## Decisions before recording release

Released HAP 2.2.3 does not expose a bounded HDS send/drain/cancel API. IPC credits
cannot make its internal socket writes strictly bounded. A reviewed upstream API
extension or maintained narrow fork is required before strict HDS backpressure
can be claimed. No private-socket monkeypatch or library fork has been adopted.

HAP pairing persistence also needs private-path/corruption checks and verified
durability. Native muxing must prove real timestamps, keyframe fragments and
cancellation before choosing an alternative backend. Full HKSV requires actual
decodable pre-roll and supported output formats; cold event capture cannot promise
that. Battery capture will not become continuously warm without an explicit policy
decision. Conditional HEVC/modern HomeKit slices S16–S19 remain separately gated.

## Verification boundary

Tests use fake Ring clients, real pinned SDK wrappers, isolated HTTPS fixtures,
synthetic native media, and a real compiled Node control child. They do not prove
live Ring connectivity, Home pairing, Home hub delivery, HKSV, Apple interoperability,
camera performance, or deployment on an unattended machine. The adapter is
development source at this stage; the daemon does not download/build it on startup.

Raw dependency audits retain two known OAuthlib provider-side advisory IDs (one
also has a duplicate scanner record). RingSDK requires OAuthlib below 4 and uses
its client password/refresh flow. CI exceptions are limited to those reviewed
provider paths and the exact locked version; raw reports remain visible and new
findings fail the gate. This is not a zero-advisory package claim. Tooling findings
are audited separately, with no exceptions.
