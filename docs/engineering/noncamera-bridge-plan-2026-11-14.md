# Halo Bridge: non-camera Ring integration

Date: November 14, 2026. Status: provider/catalog, durable registry, sensor bridge
publication, guarded normal Alarm controls and individual Smart Lighting controls
are implemented locally. Installed and physical-device acceptance remain pending.
See the [wave 1](noncamera-wave1-2026-11-14.md),
[wave 2](noncamera-wave2-2026-11-14.md), [wave 3](noncamera-wave3-2026-11-14.md),
[wave 4](noncamera-wave4-2026-11-14.md) and
[wave 5](noncamera-wave5-2026-11-14.md) implementation records.

The subsequent [Scrypted, Homebridge and Home Assistant comparison](ring-project-comparison-2026-11-14.md)
is complete. It expands the configuration, provider-recovery and control acceptance
requirements below. The [subscription-free alarm feasibility report](ring-alarm-subscription-feasibility-2026-11-14.md)
distinguishes permitted regional/legacy cloud control from a separate local alarm
architecture. The implementation records distinguish completed local work from
installed and physical qualification.

## Product decision

Adopt the owner's proposed topology:

```text
Ring account
  ├─ cameras and video doorbells → existing standalone HomeKit accessories
  └─ supported non-camera devices → Halo Bridge → Apple Home
                                     pair once
```

Pair **Halo Bridge** once. It imports supported non-camera accessories from the
active Ring account and can automatically add subsequently discovered supported
devices. Keep explicit exclusions and a preview of what will be imported. Cameras
remain individually paired; the seven existing camera identities and their
pairing files do not need migration. Camera-associated motion, light, siren and
battery services remain with their camera, avoiding duplicate accessories.

This matches current Homebridge Ring source: cameras enter the external-accessory
list and other supported devices enter the platform-accessory list. This is a
deliberate Halo product/lifecycle choice, not a claim that HAP's public API
technically forbids every form of bridged camera. Current source takes precedence
over older wiki pages describing a previous camera topology.

"All devices" means discover every device obtainable from the account and expose
every supported, correctly mapped capability. Unsupported devices remain visible
in Halo with an explanation. It cannot mean inventing a HomeKit control for every
Ring product or every Ring app setting.

The software Halo Bridge does not replace the physical Ring Alarm Base Station,
Ring Smart Lighting Bridge, or other radio gateways required by a device. The
initial integration uses Ring's cloud/account transports; it does not establish
local-only operation or eliminate Ring-side outages.

## Research and feasibility results

- Current Homebridge Ring main: `152ea689e3bdc14632dd0e98dd14358cb376f8cc`,
  checked November 14; its package version is `15.0.0-beta.0`. Stable remains
  `14.3.0` at `3bcc624acc35b6f97c391089a12bff582bb6ed4a`; that release's
  source independently confirms the same topology. Keep development-source
  findings distinct from released behavior.
- Python `ring-doorbell` remains at `0.9.14`, matching Halo Server's pin. Its
  camera/chime inventory is not a full alarm/lighting subscription implementation.
- Halo GTK already implements native Python alarm REST/CLAP transport, state
  reduction, reconnect handling, alarm commands, chimes, and lighting groups.
  Reuse baseline: clean local revision `c2f99aa3c8da3f75181a23201c53fcb972203309`.
  **486 focused fixture tests passed**, with zero failures/errors/skips. Four
  aiohttp BasicAuth deprecation warnings remain; these are not live-device tests.
- An unpublished in-memory probe against the deployed release's **Node 26.12.1 /
  HAP 2.2.3-halo.2** successfully built contact, motion, security-system, light,
  leak, smoke, CO, lock, chime-switch and siren-switch models. Ten models attached
  to one bridge; characteristic values survived per-child serialization.
- The same probe accepted **149 children** and rejected the 150th, nested bridges,
  and attaching one child to two bridges. Plan explicit overflow handling rather
  than silent truncation or moving existing children between bridges.
- HAP bridge serialization does **not** include its children. Halo must rebuild
  the graph from its own registry and restore handlers. UUID serialization alone
  does not prove stable AIDs/IIDs, pairing, or Apple Home behavior across restarts.

These checks justify development on the existing runtime. They do not prove Ring
model support, live event timing, successful Apple pairing, command completion,
or physical safety-device correctness. No Ring commands, HomeKit publication,
server deployment, or account/pairing changes were performed for this research.

## Capability policy

| Device family | Proposed Apple Home representation | Implementation boundary |
| --- | --- | --- |
| Contact sensors and supported retrofit zones | ContactSensor, with battery/tamper/fault where actually reported | First read-only pilot. Preserve unknown/offline separately from closed. |
| Alarm and supported lighting motion sensors | MotionSensor, plus reported health | Use hub state/events; do not treat them as camera FCM events. Model-specific reset behavior requires fixtures. |
| Ring Alarm panel | SecuritySystem: Disarmed, Home/Stay, Away, and observed alarm state | Separate requested/current state and entry/exit delay. No automatic bypass. Do not advertise Night until its mapping is explicitly defined and tested. |
| Smart Lighting lights and supported groups | Lightbulb, brightness/motion only where supported | Add lighting-only bridge discovery. Groups are logical controls; offer deliberate exposure to avoid confusing duplicates. |
| Compatible switches and outlets attached through Ring | Switch or Outlet based on actual device capability | Command support and confirmation must be implemented and qualified per model. Discovery alone is insufficient. |
| Chime / Chime Pro | Halo UI initially; optional clearly named HomeKit action/snooze controls after qualification | Tone, volume, sound tests and settings have no complete native Home representation. Upstream nightlight control enables its ambient-light sensor, not instantaneous lamp power; do not mislabel it as an observed light state. Newer Chime generations need qualification. |
| Flood/freeze sensors | LeakSensor for actual flood reports | Freeze is a boolean alert, not a temperature measurement. Show it honestly in Halo; any HomeKit substitute requires an explicit label/opt-in. Never fabricate degrees. |
| Supported smoke/CO detectors and listeners | SmokeSensor / CarbonMonoxideSensor, read-only | Preserve the difference between a detector and a listener. Require known payloads and real model validation; upstream support claims can be explicitly untested. |
| Compatible Ring-connected locks | LockMechanism after adapter qualification | Expose real current/target states, jam/unknown and command uncertainty. No implied Home Key, PIN management or universal lock support. |
| Glass-break sensors, valves, thermostats and other hub children | Per-capability extension after inspection | Catalog first. Some require new provider commands and model-specific mapping; no automatic generic switch fallback. |
| Intercom / access-control products | Verified capabilities only, possibly LockMechanism where appropriate | Unlock support is distinct from call audio/video. Do not advertise a working HomeKit camera or two-way audio from API discovery alone. |
| Base stations, keypads, range extenders and gateways | Halo diagnostics and parent relationships by default | Do not manufacture a controllable Home tile for an infrastructure device or imply keypad buttons are exposed as events. |
| Garage, Key/in-home and unknown products | Visible unsupported/limited inventory until investigated | An API kind or device name is not a verified control contract. |

Beams light commands also need semantic care: upstream Off returns a light to its
default automatic behavior, rather than permanently disabling motion lighting.
Preserve source duration/automatic-mode behavior and confirm what HomeKit On/Off
means for the actual model. Deduplicate REST and CLAP lighting-group discovery.
Sidewalk-only devices are not proven reachable through the existing hub transport.
An Intercom unlock RPC or notification also does not establish physical lock/door
position. Never invent a secured state or a timed relock to make a HomeKit service
appear complete; choose a limited representation if authoritative state is absent.

Account roles, feature availability and Ring entitlements remain enforced by Ring.
Inventory access does not establish command permission. Current US documentation
requires a compatible subscription for digital Alarm control, with historical
legacy and regional exceptions requiring account-specific evidence. Do not add a
blanket paid-plan gate: represent actual per-feature permission, denial and unknown
separately, and do not diagnose every timeout as a subscription or bypass problem.
No reliable software-only entitlement bypass was established by the project review.

Battery percentage, low-battery status, connectivity, tamper, brightness and other
optional fields require explicit source evidence. Their absence is not zero or
false. Dangerous/action-style controls such as alarm siren or chime sound tests
are deliberate opt-ins; discovery, startup, pairing and reconciliation must never
actuate anything. Panic/emergency dispatch functionality is outside this initial
scope. Existing camera controls retain their current behavior.

Auto-import applies to qualified accessory mappings. Enabling auto-import must
not silently opt into newly introduced action switches or change prior exclusions.
Bridge reset affects that bridge and its children together, never cameras.

### Configuration and monitoring informed by other bridges

- Import preview and persistent exclusions by location, device family, device and
  optional service; readable controls instead of requiring internal IDs.
- Separate operational controls, configuration and diagnostics. Show reported
  battery, tamper, power, firmware, radio health and last update without fabricating
  missing measurements or exposing household network identifiers.
- Preserve Alarm target/current mode, entry/exit phase, observed countdown,
  triggered state and trouble reasons. Constrain HomeKit modes to actual supported
  mappings. A camera-only location mode is a distinct optional feature.
- Supported Chime volume, test-tone actions, snooze duration/remaining time and
  automatic nightlight configuration belong in Halo's GUI first. Sensor chirps and
  keypad/base volume are useful follow-on controls after model qualification.
- Expose explicit lighting duration/automatic-mode policy and optional groups.
  Coalesce supported slider updates without substituting requested for observed
  state or applying replay behavior to alarm/lock actions.
- Distinguish hub push health, camera notifications and REST-refresh health.
  Use coalesced bounded refreshes, per-location discovery deadlines and jittered
  reconnects. An offline location must not indefinitely block healthy locations.
- On consumer reconnect, deliver an ordered state snapshot followed by deltas.
  UI alert debounce must not delay source invalidation or allow stale commands.

Home Assistant's built-in Ring component is not its broad Alarm integration:
ring-mqtt supplies that community functionality. None of these reference projects
is a new runtime dependency. A future MQTT output remains optional, and a local
Z-Wave alarm engine is a separate product/backend decision.

## Backend strategy

Retain Python as the sole Ring session/credential owner and retain the qualified
camera/media path. Adapt suitable non-GUI Halo GTK modules into server providers.
Both projects declare GPL-3.0-or-later; retain source provenance and relevant
headers when reusing code. Do not introduce a second Node Ring login/token writer
or replace the camera backend merely to get non-camera devices.

There is a concrete prerequisite: GTK's shared Ring JSON transport depends on its
serialized token-refresh coordinator (`_halo_refresh_serialized` and
`_halo_auth_request_scope`), which the server does not currently install. Port and
qualify the shared-auth boundary before adopting those service modules. Merely
copying modules or creating another Auth instance is insufficient.

GTK alarm discovery currently begins with `base_stations`. Accounts with only
Smart Lighting `beams_bridges` need additional discovery. Existing alarm state
normalization covers more families than its command layer; only normal alarm-mode
writes are currently established there. Commands for locks, lights and other hub
children must be implemented and tested separately.

```text
Halo daemon: one account/session and token-refresh owner
  ├─ camera discovery / FCM / media → existing camera adapter child
  │                                  └─ standalone cameras
  └─ REST discovery + location/hub event providers
       └─ device catalog / observed state / command coordinator
            ├─ owner API → existing GTK-style web console
            └─ separate supervised device adapter child
                 └─ Halo Bridge → non-camera accessories
```

The new child is supervised by the existing daemon. It does not need another
user-facing systemd service. It receives bounded device data and allowlisted
command messages, not Ring credentials or media handles. A child crash/reset must
not restart the camera adapter. Common daemon/CPU/network limits still exist.

The canonical device model remains independent of HomeKit. Future ecosystem
adapters consume the same catalog, state and commands; this work does not itself
implement Matter or another ecosystem.

## Current-code integration map

| Area | Required change |
| --- | --- |
| `ring/devices.py`, `ring/client.py` | Keep camera SDK contracts stable. Add generic provider identifiers instead of forcing hub IDs into the current positive-integer camera namespace. |
| New `devices/` domain and Ring providers | Immutable device identity/capabilities, locations/parents, observed values/freshness, discovery transactions, snapshot/delta reduction and scoped commands. |
| `service.py`, `homekit/service_backend.py` | Separate provider readiness from `_account_integrations_ready`, currently tied to camera media/FCM setup. Failed camera media must not invalidate sensors and vice versa. |
| `homekit/models.py`, `registry.py` | Retain strict camera models/store. Add a separate device-bridge registry and account-scoped selection/auto-import policy. |
| `homekit/runtime.py`, IPC and release packaging | Add a distinct device child role and versioned protocol, exact build handshake, supervision, release lifetime guards and bounded queues. Preserve camera protocol v1. |
| `homekit-adapter/` | Use a separate entry point/build role for an actual HAP Bridge and typed services. Current `AccessoryManager` publishes camera accessories individually. |
| Pairing storage | New nested `state/homekit/devices/pairings/` subtree with its own parent. Existing `.pairings.owner.lock` is in the parent's directory; sibling roots under one parent would collide. HAP storage is process-global, so keep device and camera storage in separate children. |
| `owner_state.py`, `owner_admin.py`, `owner_client.py` | Generic scoped inventory/commands, independent provider/bridge health, bounded pagination and explicit bridge pairing operations. Preserve existing camera API shapes. |
| `web/operations.py`, web routes | Version-compatible operation journal evolution, uncertainty handling and rollback. Reuse authentication, CSRF, recent re-auth and generation/revision checks. |
| `web-ui/src/lib/types.ts`, Devices and Integrations pages | Discriminated device kinds; one Halo Bridge pairing card with child count/import policy. Child accessories inherit pairing and have no individual QR. Only cameras request recording/pairing details. |

Full architecture inspection is retained in the research workspace listed below.
The current working tree has prior UI/snapshot changes; preserve them throughout.

### Identity, freshness and command requirements

- Persist identity using profile, provider namespace, location, parent asset when
  relevant, device ID and endpoint. Never use display names or list positions.
  Keep durable identity separate from account/provider/device generations.
- Preserve UUIDs and HAP identifier caches across rename, inventory reorder,
  temporary disappearance and restart. Rebuild children explicitly. Partial
  discovery or a cloud outage must not erase accessories and Home automations.
- Reject old-account/old-provider events and commands after account changes.
  A bridge paired for account A must not silently expose account B.
- Reconcile initial snapshots with live deltas; detect sequence gaps where
  supported; resubscribe and resynchronize after disconnect/overflow. Bound queues,
  retries, payload sizes and state history.
- Separate valid current state, last-known state, source availability and pending
  commands. Expired observations must report an error/fault rather than a fabricated
  closed contact, dry sensor, secured lock or disarmed alarm. Qualify actual Apple
  Home stale-value presentation per characteristic.
- Serialize commands per device; use scope, observed revision, operation ID and
  deadline. An accepted write is not confirmed physical state. Await authoritative
  events/readback and report an uncertain outcome after ambiguous transport failure;
  do not automatically repeat a possibly sent action.
- Also arbitrate shared resources: Alarm modes are location-wide, and lighting
  groups may overlap each other and camera lights. Preserve the intent of GTK's
  `lighting_actions` reservations while integrating the server camera-light path;
  per-device locks alone cannot prevent conflicting group and individual writes.
- HomeKit's alarm targets do not map one-to-one to every Ring mode. Explicitly
  constrain valid targets. Do not copy upstream default-to-disarmed behavior for
  unknown values or Night-mode contact bypass. Ring location modes without a
  physical alarm are a separate feature, not a substitute panel.
- Record source-event receipt, state reduction, HAP notification submission and
  command convergence timing without storing credentials or raw household payloads.
  Notification submission alone does not prove Apple Home display latency.

## Implementation slices

Progress: [wave 3](noncamera-wave3-2026-11-14.md) implements the separate bridge
runtime and read-only sensor publication in slice 4, with its usable pairing
interface from slice 6. [Wave 4](noncamera-wave4-2026-11-14.md) adds the Alarm
family of slice 5, including observed state and separately opted-in normal modes.
[Wave 5](noncamera-wave5-2026-11-14.md) adds individually qualified physical Beams
lights: observed binary state, bounded On requests, return to automatic, HomeKit
Lightbulb publication and per-device control preferences. These controls require
an exact bridge/asset match and explicit owner role; shared-user lighting control
is a remaining qualification task, not a claim that Ring forbids it.
All changes remain local pending installed and physical qualification. Lighting
groups, brightness, chimes and other control families remain future work.

Next control work needs the bounded shared-session REST write path: lighting
groups require REST/CLAP deduplication and arbitration with overlapping camera
lights, while Chime actions/settings use REST instead of the new hub command.
Qualify Chime connectivity from `alerts.connection` before exposing controls;
the existing discovery checks do not yet handle that field. Start Chimes in
Halo's interface with known-model volume and explicit sound tests, then qualify
snooze, tones and automatic nightlight configuration separately. Do not create
constant-state HomeKit action switches or imply nightlight settings are lamp state.

Each slice should have meaningful regression checks and a reviewable commit.
Physical acceptance and deployment are distinct from fixture success.
The [comparative acceptance additions](ring-project-comparison-2026-11-14.md#additions-to-implementation-and-verification)
are mandatory scope for the corresponding slices, including partial discovery,
full alarm phases, command convergence, exposure filters and source freshness.

1. **Actual inventory and contracts.** Read-only discovery of the owner's device
   families/locations, redacted structural fixtures, capability matrix and generic
   identifiers. Confirm camera discovery remains unchanged. No control commands.
2. **Shared-auth providers and catalog.** Qualify the token-refresh coordinator,
   account fencing, CLAP/REST providers, lighting-only discovery, freshness and
   reconnect/snapshot-delta reduction. Start read-only. Test concurrent refresh,
   logout/relogin races, malformed payloads and partial inventory.
3. **Durable Halo Bridge registry.** Import/exclusion policy, stable child identity,
   separate private storage, bridge pairing maintenance and explicit capacity
   handling. Test corruption/write failures, rename/reorder/missing devices, account
   changes, rollback and byte-preservation of camera stores.
4. **Bridge runtime and sensors.** Separate HAP child, versioned IPC, bridge
   publication and verified read-only sensor services. Test event ordering,
   stale/error behavior, child crash isolation, restart identity, dynamic additions,
   bridge reset and no startup actuation.
5. **Alarm and other verified controls.** Alarm state/modes first, then lighting,
   supported chime actions and qualified additional families. Tests cover blocked
   arming without bypass, timeouts, uncertain outcomes, readback, account change
   during a command and per-device serialization. Split by family if necessary;
   unsupported hardware does not block verified sensor release.
6. **Owner interface.** GTK-style family/location filters, state/battery/fault,
   bridge pair-once flow, exclusions, scoped controls and diagnostics. Test actual
   HTTPS APIs, operation journals, multiple tabs, keyboard/mobile layouts and large
   inventories without activating camera media.
7. **Installed coexistence and recovery.** Reproducible artifacts, compatible
   persistent schemas, upgrade/rollback, daemon/web/provider restarts, resource
   budgets and all seven camera identities/snapshots/live media. The deployed camera
   HAP is `2.2.3-halo.2` while the working tree declares `2.3.0-halo.1`; avoid an
   accidental camera upgrade during this build. Select and qualify artifacts
   explicitly.
8. **Controlled physical pilot.** Pair Halo Bridge once; exercise contacts/motion
   first, then supported controls with the owner present when appropriate. Verify
   state timing, Ring outages/reconnect, reboot, dynamic import, exclusions and
   camera coexistence. Expand only qualified families; record remaining model gaps.

No broad backend replacement is recommended. The necessary new work is a generic
device layer, provider integration, durable bridge lifecycle, HomeKit mappings and
owner UI—not another camera transport redesign.

## Evidence and references

- [Current Homebridge Ring topology](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/homebridge-ring/ring-platform.ts): camera classification, external versus platform accessory publication.
- [Stable 14.3.0 topology](https://github.com/dgreif/ring/blob/3bcc624acc35b6f97c391089a12bff582bb6ed4a/packages/homebridge-ring/ring-platform.ts#L240) and [release](https://github.com/dgreif/ring/releases/tag/homebridge-ring%4014.3.0).
- [Current Ring device types](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/ring-client-api/ring-types.ts) and [location transport](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/ring-client-api/location.ts).
- [Homebridge alarm mapping](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/homebridge-ring/security-panel.ts): reference behavior; do not copy implicit safe defaults or bypass semantics.
- [Homebridge flood/freeze mapping](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/homebridge-ring/flood-freeze-sensor.ts): freeze uses an occupancy workaround, not a measured temperature.
- [Homebridge Ring releases](https://github.com/dgreif/ring/releases): distinguish stable releases and explicitly untested model additions.
- [Ring Alarm digital arming/disarming](https://ring.com/support/articles/8fscc/Arming-and-disarming-Ring-Alarm?redirect=true): account/entitlement requirements remain Ring-owned.
- [HAP-NodeJS Bridge source](https://github.com/homebridge/HAP-NodeJS/blob/bd7a01b889cf1c74cbfcc4a4e5efdfa53fc3532b/src/lib/Bridge.ts) and [Accessory source](https://github.com/homebridge/HAP-NodeJS/blob/bd7a01b889cf1c74cbfcc4a4e5efdfa53fc3532b/src/lib/Accessory.ts): current upstream reference; deployed-version behavior was checked separately.
- Local reusable sources: `../halo-gtk/src/halo_gtk/alarm/`, `ring_json.py`,
  chime and lighting modules, and their focused fixture tests (paths relative to
  the Halo Server repository root).
- Research workspace:
  `/home/james/Documents/Codex/2026-10-25/he/work/halo-server-noncamera-research-2026-11-14/`.
  It contains `architecture.md`, `providers.md`, the pinned upstream source
  extracts, `reuse-fixtures.xml`, and `probe/bridge-feasibility.{cjs,json}` with
  source hashes and `probe/README.md` qualification boundaries. The prototype is
  research evidence, not an installed runtime feature or a maintained regression
  suite; convert relevant cases into project tests during their implementation
  slice.
