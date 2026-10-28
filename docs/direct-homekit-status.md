# Direct HomeKit implementation status

Engineering record, 2026-10-28. Baseline: `d3c74883b16434d2d0244404fc6766ca1f38f98a`.

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
- S05 groundwork: capture consumers, timestamp math, the Ring receiver and direct
  service imports no longer require GI/GStreamer. Private control v1 has strict
  bounded framing/schema/fixtures and immutable scope/privacy inputs, independent
  of unchanged public plugin v2/v3 contracts.
- S06 groundwork: inherited private socket pairs, Python ownership of one Node
  process, handshake, bounded requests, heartbeat, crash budget, boot rotation,
  cancellation-safe startup and joined TERM/KILL cleanup. The synthetic adapter
  imports public HAP exports but does not initialize storage or publish anything.

Initial Ring restoration runs in an owned background task so local administration
is available while the network is slow. Pending authentication is retired during
shutdown. OS-level filesystem operations and cancellation-suppressing native work
cannot be force-killed as Python threads; ownership is retained and incomplete
cleanup is reported rather than admitting another upstream.

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

S04 still needs the final distribution/runtime capability gates. S05 still needs
the complete service facade, operational configuration and policy propagation.
S06 still needs production service wiring and bounded bulk transfers. A handshake
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
