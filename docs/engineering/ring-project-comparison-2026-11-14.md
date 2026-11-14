# Ring device bridges: comparative research and Halo decisions

Date: November 14, 2026. Research completed before implementation. This augments
the [non-camera bridge plan](noncamera-bridge-plan-2026-11-14.md).

## Recommendation

Retain one Halo Bridge for supported non-camera devices, standalone cameras, and
one daemon-owned Ring authentication session. Combine Homebridge's device/service
selection, Scrypted's location/provider structure, and Home Assistant/ring-mqtt's
rich device health and alarm-state presentation. Preserve Halo's stricter
identity, freshness, authorization and uncertain-command handling.

The best result comes from adapting useful behavior, not copying entire clients
or all their configuration defaults. Scrypted, Homebridge and ring-mqtt also share
ancestry in the unofficial Ring client, sometimes through different forks. Their
agreement is useful protocol evidence but is not three independent validations
of Ring compatibility or subscription-free control.

## Sources inspected

| Project | Pin / scope | Actual non-camera role |
| --- | --- | --- |
| Scrypted Ring | Scrypted `fb679504ce2d8bc150bb504a7098826644627100` (Nov 12), plugin manifest `0.0.147`; Ring fork `c19ba4bb22e55850232bd30dd8539410a1a41cb8` (Nov 3) | Location provider, Alarm control, common hub sensors, lights, switches and locks. Client library support exceeds what the plugin actually exports. |
| Homebridge Ring | Current main `152ea689e3bdc14632dd0e98dd14358cb376f8cc`, package `15.0.0-beta.0`; stable `14.3.0` at `3bcc624acc35b6f97c391089a12bff582bb6ed4a` | Broad HomeKit accessory mappings and exposure options. Stable/current configuration schemas were independently read and match. |
| Home Assistant core Ring | Stable `2026.11.1`, `d8a0dc698c8946b44e455d904000db563cfbf1ba` (Nov 9), Python Ring `0.9.14` | Cameras, Chimes, Intercom and related settings. It does not implement the Ring Alarm panel/hub-sensor integration. |
| ring-mqtt | Stable `5.9.3`, `d28fdbd56a850d57cce2fdf43a438033e78f1e6b` (Mar 9); current main `59aab41b4afd1692148b1180a6b0920591c86db1` has later README change | Separate community application supplying broad Alarm/lighting/device support to HA via MQTT discovery; uses a patched Ring client fork. |
| Home Assistant HomeKit Bridge | Same HA stable pin | Translates HA entities into HomeKit; distinct from Ring source integration and its permissions. |
| Alarmo / Z-Wave JS | Alarmo `1.10.19`, `169e134f4b70d87aae36ba54a72a398ecc960afd`; Ring definitions at `4ee0e214dd46b5ff13023f92b90350e99f3b5af5` | Evidence for a separate local alarm architecture using supported peripherals, not a subscription bypass for the existing Ring base. |

Primary source entry points: [Scrypted adapters](https://github.com/koush/scrypted/blob/fb679504ce2d8bc150bb504a7098826644627100/plugins/ring/src/location.ts),
[Scrypted manifest](https://github.com/koush/scrypted/blob/fb679504ce2d8bc150bb504a7098826644627100/plugins/ring/package.json),
[Homebridge config](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/homebridge-ring/config.schema.json),
[HA Ring source](https://github.com/home-assistant/core/tree/d8a0dc698c8946b44e455d904000db563cfbf1ba/homeassistant/components/ring),
[ring-mqtt orchestration](https://github.com/tsightler/ring-mqtt/blob/d28fdbd56a850d57cce2fdf43a438033e78f1e6b/lib/ring.js).

## Discovery, detection and monitoring

Three source channels have different jobs:

1. **REST discovery and settings** enumerate locations, hubs and device families,
   and refresh Chime/settings/health values. Coalesce refresh requests; bound
   concurrency and payloads; use explicit auth recovery and rate-limit handling.
2. **Location CLAP WebSockets** provide Alarm/Smart Lighting initial inventories
   and device changes. Discover the union of Alarm and Beams hubs, including
   lighting-only accounts. On reconnect or hub return, rebuild from a fresh
   snapshot before accepting deltas as authoritative.
3. **FCM** handles existing camera/doorbell notifications and relevant Intercom
   events. It is not a replacement for the hub sensor-state feed.

Scrypted and Homebridge use these distinctions; HA core separates its REST
coordinator and push listener, while ring-mqtt separates socket and polled-device
classes. Adopt the architecture without presenting one misleading global
"polling" toggle. One source can be healthy while another is degraded.

Sources: [Ring location transport](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/ring-client-api/location.ts),
[Scrypted fork transport](https://github.com/koush/ring/blob/c19ba4bb22e55850232bd30dd8539410a1a41cb8/packages/ring-client-api/location.ts),
[HA coordinator](https://github.com/home-assistant/core/blob/d8a0dc698c8946b44e455d904000db563cfbf1ba/homeassistant/components/ring/coordinator.py),
[ring-mqtt socket devices](https://github.com/tsightler/ring-mqtt/blob/d28fdbd56a850d57cce2fdf43a438033e78f1e6b/devices/base-socket-device.js).

Halo must improve discovery isolation: an offline hub must not block other
locations or turn an incomplete scan into accessory deletion. Maintain immutable
source identities, an explicit completeness marker, generation-scoped updates and
freshness per value. Consumers reconnecting to Halo receive a consistent snapshot
followed by deltas; do not depend on sleeps for ordering or replay old edges as
new motion/contact events.

## Adopt, adapt, and defer

| Feature | Halo decision | Qualification required |
| --- | --- | --- |
| Location/type/device/service filters | Adopt in the GUI with readable names and import preview; retain exclusions and automatic import policy | Identity survives filtering/restart; unsupported inventory remains visible. |
| Compound devices | One physical identity with genuine component services, such as smoke plus CO | Per-component payload evidence; absent is unknown, not clear. |
| Operational, configuration and diagnostic categories | Adopt HA/ring-mqtt separation | Avoid Home tiles for gateways, metadata or unsupported controls. |
| Battery/tamper/radio/power/firmware/last update | Add a useful device health view where reported | No invented percentage, exposed Wi-Fi identifiers, or stale green status. |
| Full Alarm state | Preserve mode, target, entry/exit delay, countdown, triggered state and trouble reasons | Ring panel is authoritative; qualify HAP projection and timestamp quality. |
| Lighting timers and groups | Adopt explicit duration/automatic-mode semantics and optional group exposure | Shared arbitration across overlapping groups/camera lights; group acceptance is not member confirmation. |
| Chime configuration | Add supported volume, tone actions, snooze duration/remaining time and automatic nightlight setting to Halo UI | Exact model capabilities; automatic light sensor setting is not lamp power. Action switches in HomeKit remain opt-in. |
| Sensor chirps and keypad/base volume | Useful follow-on configuration where source commands are verified | Per-model bounds, readback, ownership and failure behavior; not a prerequisite for passive sensors. |
| Coalesced brightness changes | Adopt a bounded latest-intent scheduler for sliders | Preserve confirmed state and cancel obsolete pending values; do not apply this strategy to arm/unlock commands. |
| Camera/location Modes without an Alarm base | Separate optional feature with clear naming | Must not look like a real armed security panel or alter camera privacy implicitly. |
| Night mode / automatic bypass | Do not copy default bypass behavior | Night needs a deliberate documented mapping. Any future bypass must name sensors, use fresh evidence and be scoped to the intended action. |
| Siren/panic/dispatch | Report real state; keep siren exposure separate; defer emergency actions | Pairing/import never actuates a device. No emergency-trigger smoke tests. |
| MQTT ecosystem output | Preserve a clean future adapter seam; no broker dependency for current HomeKit milestone | Future contract needs availability, retained-state validity, non-retained commands and consumer restart ordering. |

References: [Homebridge filtering](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/homebridge-ring/ring-platform.ts),
[lighting](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/homebridge-ring/beam.ts),
[Chime](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/homebridge-ring/chime.ts),
[ring-mqtt panel](https://github.com/tsightler/ring-mqtt/blob/d28fdbd56a850d57cce2fdf43a438033e78f1e6b/devices/security-panel.js),
[sensor configuration](https://github.com/tsightler/ring-mqtt/blob/d28fdbd56a850d57cce2fdf43a438033e78f1e6b/devices/binary-sensor.js),
[HA HomeKit Bridge](https://www.home-assistant.io/integrations/homekit/).

## Source behaviors Halo should improve

These are observations at the pinned revisions, not claims that every upstream
installation exhibits every failure or a claim of measured superiority for Halo.

- **Scrypted Alarm state:** the Ring adapter sets `triggered: false` and defaults
  an unrecognized location mode to Disarmed. Its Night bypass iterates a Map with
  `Object.values`, which yields no device entries. Use actual panel state and
  explicit errors; do not inherit either shortcut. [Adapter source](https://github.com/koush/scrypted/blob/fb679504ce2d8bc150bb504a7098826644627100/plugins/ring/src/location.ts).
- **Homebridge normalization:** missing contact/alarm fields can become closed or
  disarmed, categorical battery strings become estimated percentages, generic
  sensor names affect type selection, and unknown Z-Wave devices can receive
  writable switches. Keep typed capability evidence and unknown states.
  [Device factory](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/homebridge-ring/ring-platform.ts),
  [battery normalization](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/homebridge-ring/base-device-accessory.ts).
- **Command acknowledgement:** some upstream HAP handlers acknowledge writes
  before asynchronous completion, device methods swallow errors, and alarm mode
  completion uses the next update rather than a bounded matching observation.
  Halo must distinguish admission, send, rejection, uncertainty and convergence.
  [Homebridge plumbing](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/homebridge-ring/base-data-accessory.ts),
  [device transport](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/ring-client-api/ring-device.ts).
- **ring-mqtt recovery:** sequential location discovery can wait behind offline
  hubs; availability reporting includes delay; alarm control retries up to five
  times after short state checks. Preserve healthy subsets and source timestamps;
  never replay ambiguous actions against a newer intent. Alert debounce may be
  useful, but it must not conceal loss of source validity from control admission.
  [Orchestration](https://github.com/tsightler/ring-mqtt/blob/d28fdbd56a850d57cce2fdf43a438033e78f1e6b/lib/ring.js),
  [panel retries](https://github.com/tsightler/ring-mqtt/blob/d28fdbd56a850d57cce2fdf43a438033e78f1e6b/devices/security-panel.js).
- **HA HomeKit projection:** supported-mode constraints are useful, but generic
  arming maps to an Away target and pending/unavailable changes can retain prior
  values. Halo should retain the actual requested target and expose source faults,
  with richer phase information in its own UI. [Alarm adapter](https://github.com/home-assistant/core/blob/d8a0dc698c8946b44e455d904000db563cfbf1ba/homeassistant/components/homekit/type_security_systems.py).

## Subscription-free alarm feasibility

The [separate feasibility report](ring-alarm-subscription-feasibility-2026-11-14.md)
records official regional policy, dated maintainer evidence and local-radio leads.

For a subscription-gated Ring account, none of the examined integrations provides
a demonstrated reliable software-only bypass for arming the original base. They
still use Ring's cloud. Eligible legacy/regional accounts differ, so Halo must
reflect actual per-feature permission instead of adding its own blanket paid-plan
requirement. State observation and control permission are separate capabilities.

A separate local alarm using compatible Ring Z-Wave peripherals, an independent
controller and an engine such as Alarmo is a credible alternative. It requires
hardware and migration, with a new alarm lifecycle; it does not remotely unlock
the existing Ring base or retain its monitoring/backup services automatically.
Keep that as a separate future provider decision. The current plan changes no
sensor pairing, subscription, region, or alarm mode.

## Additions to implementation and verification

Keep the eight slices, with these expanded acceptance cases:

1. **Inventory:** partial success with one offline hub, lighting-only locations,
   identical device IDs across locations/providers, disabled zones and dynamic
   capability changes. No delete-on-timeout or name-based capability inference.
2. **Providers:** concurrent auth refresh with cameras active, missed/gapped
   deltas, reconnect bursts, source epochs, nested flood false versus aggregate
   trouble, and source-specific motion reset semantics.
3. **Registry:** exposure by location/device/service and persistent exclusions;
   stable identities across filtering, rename and service additions.
4. **HAP sensors:** unknown/stale/false distinctions, genuine combined devices,
   battery category versus percentage, UI-debounced outages without stale command
   authority, and per-provider health projection.
5. **Controls:** unrelated state updates while arming; exact Home/Away targets;
   authoritative triggered/delay states; explicit role/entitlement denial versus
   ambiguous failure; blocked arming without bypass; command supersession;
   overlapping lighting groups; no replay after restart/reconnect.
6. **UI:** operational/configuration/diagnostic categories, readable import policy,
   panel phase/countdown, command outcomes, valid Chime/duration settings, and
   unsupported-device explanations. No exposed Ring PINs or tickets in diagnostics.
7. **Installed recovery:** observer and adapter restarts yield ordered snapshots,
   account switches retire old authority, mixed-provider failures preserve cameras,
   and rollback retains stores and operation outcomes correctly.
8. **Physical acceptance:** qualify each family's actual source schema and HomeKit
   behavior. Observe real latency and reconnects; no simulated fixture result is
   reported as an entitlement check or physical command success.

## Verification and retained evidence

This pass inspected current/pinned source, release/configuration files and primary
documentation. A small isolated JavaScript check confirmed the Map enumeration
behavior; it did not run Scrypted or an alarm. No integrated bridge tests or live
device tests were performed. Earlier 486 Halo GTK fixture passes remain prior
reuse evidence and were not rerun or reclassified as cross-project compatibility.

Detailed source notes and snapshots are under
`/home/james/Documents/Codex/2026-10-25/he/work/halo-server-cross-project-research-2026-11-14/`:
`scrypted.md`, `homebridge.md`, `home-assistant.md`, `subscription-feasibility.md`,
`sources/` and `upstream/`. Preserve applicable upstream notices if code is later
copied; this research recommends behavior and contracts, not wholesale code reuse.

No production application code, server configuration, dependencies, camera
pairings or alarm hardware were changed during this research.
