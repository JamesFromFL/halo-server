# Direct HomeKit implementation status

Engineering record, 2026-10-28. Foundation committed locally as
`798b7173ad9e8fbcbe655eb692a709b17cafcc87`; S05 is committed locally as
`16c5aeda589d8d59886450b74018104b49ee616d`. S06 is committed locally as
`7b735aaf13638decb1ad839bc3c5099658e72748`. S07 is committed locally as
`d89ad9436a0e3cd354cb8b9870e867ff81071559`. S08 is committed locally as
`90c8832f8ef317117e9dc633f51f194b1df7ae76`. S09 is committed locally as
`17077c7182e863b1dd0881f526fb92e433ccf136`. S10 is committed locally as
`e115f11344edf1ea08fce474163f5410c37b3da1`. S11 builds on those commits.

The approved architecture keeps Python as the sole Ring account, push-event,
shared capture and speaker owner. A supervised Node adapter uses released public
HAP APIs for HomeKit. Native media will be delivered directly from Python rather
than through RTSP. The existing plugin/RTSP integration remains functional during
development. Direct video, negotiated Opus audio and shared talkback are implemented as development paths.
Owner pairing management, HKSV and real Apple qualification remain gated.

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
- S06: service-owned adapter startup with visible missing-resource health;
  inherited private control and bulk channels; bounded scoped request maps,
  heartbeat, crash budgets, boot retirement and joined TERM/KILL cleanup. The
  compiled Node session now drives native inventory, policy and synthetic camera
  delegates. Typed binary transfers enforce authorization, exact lengths,
  sequencing, two-chunk credit and owned consumer/producer cleanup. The adapter
  does not yet publish accessories.

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
`homekit` and `dual` report `control_ready` with
`recording_transport_unavailable` once control and native sample qualifications
are ready (`recording_output_unavailable` if the native recording probe fails); missing compiled
resources remain visibly degraded. Selected accessories can publish only after native output qualification. The
complete owner pairing/settings interface is still S13.

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
transfers, with 64 KiB chunks and a two-chunk/128 KiB credit window. S06 implements
these transfer limits on inherited sockets, including exact end counts, scoped
ownership, credit after consumer completion and whole-operation deadlines. A
32-camera page can exceed the control-byte limit with valid Unicode metadata;
pagination checks encoded bytes as well as count. The child supports private
native delegates, with no HomeKit publication or live RTP transport.

## S06 ownership and health

The daemon starts the native adapter in an owned background task, so missing Node,
compiled files, wrong runtime versions or child failures do not block Ring/local
administration. A source checkout resolves an already-built adapter and the exact
supported Node runtime; startup never runs npm, downloads files or builds source.
Wheels without bundled adapter artifacts report degraded state. Release runtime
selection and manifest verification remain S14 work.

Every RPC belongs to one boot and full source/privacy scope. Incoming callbacks,
active requests, recent wire IDs, cursor pages and media claims are bounded. An
inventory notice invalidates changed state; exact duplicate complete notices keep
live sessions intact. Policy ACKs echo the submitted scope, then the Node delegate
fetches the Python-assigned privacy generation before admitting more work. Source
metadata does not authorize camera selection. S07 resolves explicit owner selection
from a private durable registry; discovery alone never selects a camera.

Python owns lease TTL expiry even if the peer disappears. Duplicate pending
acquisitions cannot share and accidentally close a handle. Stopped session IDs
have a finite 60-second replay window (longer than control deadlines), bounded to
512 recent/active identities. Runtime wire IDs use bounded recent caches; accepted
control operation IDs are retained for the whole boot, with a visible 4096-write
limit and no actuator retry. These bounds are not an infinite replay history.

Bulk reads allocate only registered typed chunks. At most two unacknowledged
chunks are staged globally, credit follows completed consumption, and transfer
bytes/sequences must match their declaration. Cancellation and deadlines retain
ownership of callbacks/iterators that have not finished. Failed retirement closes
admission and preserves its boot/source barrier; a new runtime cannot overwrite
it. This proves private IPC flow control, not released HAP's internal HDS bounds.

The compiled synthetic delegates are tested against the real Python broker for
policy, shared capture leases, renew/stop, controls and PCM transfer. Image requests
return `not-ready` until S10 can meet decode/resize/pixel/CPU requirements. Neither
control readiness nor a private live lease means an Apple camera stream is ready.

## S07 identities, selection, storage and event delivery

The private registry retains opaque profile/camera identities across rename,
outage and ordinary restart. UUIDs, locally administered HomeKit usernames and
camera/doorbell birth class are durable; runtime account/device/privacy generations
are not. Complete inventory can enroll identities, but selection is always explicit.
Missing cameras retain tombstones. The current bound is 256 retained identities;
exhaustion is visible instead of deleting pairings. HAP chooses listener ports.

Owner-only administration adds bounded `homekit_list` pages and `homekit_select`
with current profile/account generation and revision compare-and-swap. These are
local admin RPCs; the full CLI/TUI selection and pairing workflow remains S13.
Cancelling a save joins its durable write, cache update and immediate authority
fence. An uncertain post-replace write fences authority, reloads and requests
reconciliation before any retry. A selection save does not mean a camera is live.

The Node adapter constructs released public CameraController/DoorbellController
and motion/fault services, preserves stable identity, and retires asynchronous
publication without deleting pairing data. Production publication requires the qualified S08 output catalogue and streaming
transport. Selecting native mode alone never selects cameras.
Neither a peer configuration flag nor control readiness can bypass that gate.
Selecting can publish a qualified development accessory; PREPARE still does not
wake Ring, and only START acquires a shared live capture. Secrets stay in Node
pairing storage, never registry/status. Owner pairing display remains S13.

Pairing storage preflight rejects unsafe files, symlinks, hard links, malformed or
duplicate-key JSON, invalid known filenames, inconsistent accessory/controller
records and mismatched Ed25519 keys. Corrupt records are preserved. Python pins
the owner-only directory and a separate exclusive lock; both descriptors are
inherited by Node and retained through child retirement. Tests cover a child
retaining ownership after the parent descriptor closes. This path is qualified
for Linux `/proc/self/fd`; native support elsewhere remains unqualified. These
checks do not add crash durability to HAP's own persistence.

Motion is published from source facts before media warming, with an independent
finite deadline. A doorbell does not invent motion, and unseen SDK press updates
do not become fresh edges. Event synchronization restores state and cursor without
replaying button presses. Full source/privacy scope, boot, source health and expiry
are rechecked at transport admission. Original receipt time survives daemon queue
delay. Absolute monotonic expiry prevents a stalled Node reader from reviving a
press or emitting even a transient expired-motion alert. The shared clock domain
was measured on the pinned Linux Python/Node pair; this is not a cross-OS claim.

## S08 direct video and managed compatibility

Public HAP PREPARE/START/RECONFIGURE/STOP delegates now control a Python-owned
UDP/SRTP sender over the same shared Ring capture handles. PREPARE binds and
retains an actual port-zero socket and crypto reservation, with no Ring wake.
START acquires a scoped lease; shutdown, selection/privacy/source changes and
failed acquisition join late work and retain failed cleanup claims. SSRC, crypto
state and sequence survive reconfiguration. Ephemeral track keys stay on private
control channels and are excluded from status and persistent registry.

The initial tested output geometries are 320x240@15, 640x360@30, 640x480@30,
1280x720@30 and 1920x1080@30, with Baseline/Main/High and classic levels up to4.0.
The coupled level/macroblock/rate constraints are checked after negotiation: HAP's
separate advertised lists do not make 1080p30 at level3.1 valid. All39 legal
catalogue combinations were actually encoded, inspected via SPS and decoded.
An account-free startup probe checks real native encoder/decoder behavior before
publication. Failed/missing output capability leaves local administration and the
control adapter available with visible degraded video health.

A bounded SPS parser checks actual profile, level, cropped geometry, pixel/chroma
format, timing and reorder evidence. Compatible source pictures retain their
compressed NAL bytes. Ring can negotiate High Profile level5.0; that does not fit
classic level4.0 merely because the picture is1080p. A managed isolated PyAV/libx264
worker performs real conversion when required. It uses no shell, executable swap
or user FFmpeg arguments. Upscaling/letterboxing adds no original camera detail.
Unqualified interlacing or output that cannot satisfy finite limits fails clearly.

Video sources carry an explicit capture epoch even when buffer sequence remains
contiguous across receiver retries. Decoder, timestamp/pacing reference and pending
output reset on epoch/config changes. Epoch and current privacy/source authority
are checked immediately before each datagram and cached retransmission. Reordered
source PTS is decoded without inventing DTS; copy requires qualified no-reorder
evidence. RTP NAL fragmentation honors negotiated final SRTP MTU, including tag
and header overhead. A finite byte clock paces entire access units; excessive copy
frames request real adaptation instead of bursting or relabeling source metadata.

Pinned native SRTP supplies AES128-CM/HMAC-SHA1-80 and replay protection. Literal
unicast address/family/interface checks avoid arbitrary DNS or multicast output.
HAP's sourceAddress is a local interface hint, not proof of the authenticated
remote TCP peer; no peer-equality claim is made. RTCP is authenticated before
receiver liveness, SR/RR, PLI, NACK or BYE handling. Sender reports have independent
timers and actual counters; bounded NACK history retransmits exact protected bytes.

Limits include eight prepared/active video sessions, two per camera, four managed
encoders, one access unit in flight per worker, finite source/output/receiver
watchdogs,8MiB source AUs and2MiB output AUs. Workers use one codec thread and a
768MiB address-space limit plus deadlines and joined kill/reap; these are not an
RSS ceiling claim. Prepared streams expire without waking a camera. Global
retiring-worker ownership also covers a failed account-free startup probe.

A composed test drives the real public HAP delegate through the compiled Node
child and private IPC, sends actual UDP/SRTP, independently unprotects/reassembles
and decodes the picture, and verifies exact copy NAL bytes. Shutdown admits only
owned correlated cleanup replies, closes new acquisition admission, and permits
acknowledged natural child exit. Actual Home/Home hub, live Ring and IPv6 delivery
are not inferred from these tests; this host denied the IPv6 loopback probe.

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

## S09 audio and talkback

Original validated Ring Opus has a separate bounded two-second packet lane. It
preserves the source encoded payload independently of the legacy normalized Opus
lane. Compatible mono packets copy or repacketize; other supported combinations
use a killable native PyAV/libopus worker. The selected output is 8/16/24 kHz
capture bandwidth, 20/30/40/60 ms packet time and CBR/VBR, with the classic HAP RTP
clock equal to the selected sample rate. Internal source/decoder timing stays
48 kHz. Thirty-millisecond output uses valid repacketized 10 ms frames. No system
FFmpeg executable or manual arguments are needed.

Each common live session owns separate video/audio UDP sockets, keys, SSRCs and
SRTP/SRTCP contexts with one shared capture lease and STOP lifecycle. Source RTP
sender reports are observed per pinned aiortc receiver and mapped into its encoded
callback timestamp domain. Missing/invalid source reports use an explicit receipt
time estimate; synchronized source clocks are not claimed in that case. Live
microphone output checks permission and capture epoch before reading, after native
conversion, after pacing and before every datagram.

Authenticated, selected-payload Opus must decode successfully before a lazy Ring
speaker claim starts. The shared speaker owner excludes competing native/plugin
talkers. Jitter, input/output queues, readiness, frame age, inactivity, native
workers and total speaker lifetime are bounded. Pending starts retain ownership
through cancellation and compensate late success. The final Ring PCM consumer
checks frame age and privacy admission, preventing an already queued frame from
playing after a settings fence.

`homekit/audio-settings.json` stores immutable microphone/speaker graph birth and
four mute/volume preferences, keyed by profile, camera and durable accessory UUID.
It uses the existing private CAS, no-follow, atomic-replace and fsync store. A
durable companion enrollment marker makes loss of an initialized settings file a
visible failure instead of silently recreating unmuted defaults. Birth
and saved mutes survive outages and capability drift. Public HAP characteristic
writes wait for durable acknowledgement; Python fences only the affected camera
before the disk await. A failed/uncertain write reloads durable truth and keeps the
old scope fenced. Setting changes conservatively stop that camera's active streams;
a new Home view uses the saved settings and new privacy scope.

Graph support and observed source audio are separate facts. A cold source may
have no measured audio format yet; validated source packets establish what can be
delivered. No Ring model name invents a measured format. Native fixtures cover
independent SRTP decrypt/decode, the negotiated clock matrix, waveform boundaries,
source-clock mapping, queue expiry, persistence races and late cleanup. Actual
Apple playback, source-device audio and talkback remain S15 hardware qualification.

## S10 snapshots and isolated binary cancellation

Public HAP snapshot requests now reach Python's shared reason/dimension service.
Caches and coalesced jobs are keyed by full boot/account/device/privacy scope,
geometry and reason. Each caller has its own cancellation token. Policy is checked
before lookup, after download/conversion and at final HAP delivery. Periodic and
unknown HAP reasons use Ring's cached-image GET path; event and explicit requests
may request a fresh image. The SDK can fall back to a cached image, so capture time
stays unknown. Retrieval TTL is never described as camera capture freshness. No
new capture lease or automatic battery warming is needed for tiles. Optional
recent-video frame extraction is deferred.

A strict finite JPEG marker/table/scan inspector checks bytes, dimensions, scans,
components and orientation before native allocation. Two killable PyAV workers
have512MiB address-space limits, one codec/scaler thread, two-second conversion
budgets and finite terminate/kill/reap ownership. Source and output bytes are
limited to8MiB, source pixels to8Mi and requested pixels to8,000,000 with sides
at most4096. Exact odd geometry is honored with aspect-preserving letterbox.
An exact-size baseline JPEG preserves its bytes only after isolated native
entropy/corruption/geometry verification. Progressive or different-sized accepted
images are converted and independently decoded inside the worker. CMYK and
nonidentity EXIF orientation are explicitly unsupported; malformed/truncated and
dimension-bomb inputs fail. These are per-process/operation budgets, not a claim
of a whole-daemon RSS ceiling. No Pillow runtime dependency or FFmpeg executable
was added.

The matched private protocol adds `bulk-cancel-v1`. Cancellation fences delivery
before awaiting, then drains only already credited frames to a producer-owned
sequence/byte frontier. Its exact correlated ACK releases charged slots once.
An uncooperative native callback retains its cleanup reservation independently
of drained wire state. Other transfers continue. Unknown remote cancel never
creates authority. A canceled trusted pre-OPEN authorization sends zero credits
when exact OPEN arrives. Malformed framing or an undrained frontier remains a
real shared-transport failure. Terminal records retain minimal metadata only,
at most4096 for60seconds, enough for the existing50Hz PCM cadence plus other
traffic; a3000-transfer test covers that workload.

Outgoing ownership is reserved before READY. OPEN starts only after that scoped
reply is flushed. This prevents media from racing its operation authorization and
rejects capacity before acceptance. Image/client/private-RPC/Node-job budgets are
12/14/16/20seconds; Node's READY budget is17seconds, within the pinned HAP25second
callback limit. Ordinary RPC deadlines are unchanged. Motion/event publication
never waits for a snapshot. Native/synthetic/composed tests cover actual image
content, independent decode, public HAP callbacks, real IPC/bulk, cancellation,
partial frames, privacy retirement and source/native cleanup failure. Real Apple
and Ring device qualification remains S15.

## Remaining implementation

S04 still needs the final distribution/runtime capability gates. S05–S10 implement service/configuration/contracts, operational private IPC,
identity/accessory/events, direct H264 live video, negotiated Opus, shared talkback and bounded snapshots. Build identity remains correlation, not independent provenance; immutable
bundled-resource manifests and release packaging belong in S14.

S11–S15 remain: timed recording samples and an
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

S07 validates HAP private paths, corruption and key consistency before library
reads. Verified power-loss durability still requires S14 work: released HAP writes
use rename without fsync. Native muxing must prove real timestamps, keyframe fragments and
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

## S11 recording capture and native fragmented MP4

Recording has its own scoped handle and recording/audio permissions, separate
from live viewing. One active recording and one explicitly authorized warm
collector per camera share the existing upstream; each purpose allows two
globally. Lease renewal has finite absolute limits. Failed cleanup stays charged.
Public plugin v2/v3 TTL objects retain their original five fields. In direct
HomeKit mode, an unsolicited Ring event no longer acquires the compatibility
prewarm lease; event publication remains independent of capture. Dual mode keeps
its separate legacy event consumer.

An authorized collector retains bounded real video/original-Opus history and
frozen SPS/PPS independently of rolling packet eviction. Actual capture/config
epochs, receipt and source timestamps, pressure and gaps fence old history.
Recording permission changes establish receipt floors so earlier live footage
cannot be replayed under newly enabled recording authority. Audio-off recordings
have no audio track. Warm history requires explicit trusted owner opt-in,
positive mains evidence and current recording permission; a false battery flag
is insufficient evidence. Production warm capture is not enabled in S11.

Strict recording admission requires at least four seconds of actual pre-event
history at the frozen event boundary. Waiting after the event cannot invent
pre-roll. Cold capture can be used only through an explicit internal experimental
reduced-history choice, with truthful coverage/status. The original two-second
live Opus lane remains unchanged. A recording's independent source lease survives
closing its warm collector.

The fixed PyAV worker copies compatible, proven no-reorder H.264 or performs
bounded native adaptation, and converts original Opus to AAC-LC when recording
audio is authorized. Recording AAC uses an explicitly supported VBR target;
strict CBR is rejected rather than inferred from an average encoder bitrate.
Source sender-report clocks share a frozen A/V origin;
missing valid anchors are labeled receipt estimates. PTS is not blindly treated
as DTS for arbitrary input. A bounded MP4 inspector verifies init tracks and
complete keyframe-starting fragments, offsets, sample sizes, sequence and every
track's duration against the 4000ms container maximum. Two-second encoder GOPs
leave room for AAC frame boundaries; hard cuts never manufacture decodable IDRs.

One input request at a time and at most two staged output packets preserve pull
backpressure. Two killable native recording workers, finite bytes/deadlines and
owned TERM/KILL/reap bound conversion. Every feed, queued result and final
dequeue checks full authority and actual capture/config epoch. EOS freezes and
drains an admitted source cursor; later footage cannot extend it indefinitely.

A common worker bootstrap also constrains each isolated Linux child to one of
the service's allowed CPUs before importing PyAV. Parent affinity is unchanged
and CPUs are selected round-robin. This constrains automatic resampler graph
threads as well as explicit codec threads. Codec thread_count alone did not
bound the automatic graph in the pinned PyAV version.

These are recording sample/ownership capabilities. HKSV publication remains
disabled: released HAP2.2.3 lacks awaited bounded HDS output, pre-allocation
physical connection admission and proven hard-close ownership. A separate
review-only transport proposal exists; no HAP fork, beta upgrade, cryptography
replacement or FFmpeg executable backend is adopted. Owner administration,
release/service packaging and real Apple/Ring qualification remain later gates.
