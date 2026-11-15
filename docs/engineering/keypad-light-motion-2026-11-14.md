# Keypad light, brightness lock and proximity motion

## Behavior and evidence

The keypad is a dimmable HomeKit light with a separate Switch named **Brightness
lock**. On for this switch means protected. Halo persists the lock across restart,
and refuses brightness/On writes while locked, including Siri room-light requests.
The switch is a local control permission, not a door lock or Ring alarm control.

Homebridge Ring's [brightness-only implementation](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/homebridge-ring/brightness-only.ts)
uses `DeviceInfoSet → device.v1.brightness` (0–1) for keypads. Halo normalizes the
observed level to an integer percentage using half-up rounding. The displayed
level is a configured LED brightness; it does not assert that LEDs are presently
illuminated. On restores the most recent nonzero level observed in the current
provider session, or requests 100% when no such level is known.

Ring-MQTT's [keypad implementation](https://github.com/tsightler/ring-mqtt/blob/59aab41b4afd1692148b1180a6b0920591c86db1/devices/keypad.js)
handles the realtime `impulse.v1[].impulseType == "keypad.motion"` event, and uses
a retriggerable 20-second indication. Halo previously discarded these impulses.
We now parse only this known event from every document and impulse entry, without
retaining unrelated keypad/access-code payloads. Admission requires an already
known keypad in the current online asset/connection epoch and complete inventory.
Inventory-list impulses never count as new motion.

Motion is unknown until genuine evidence arrives. That evidence enables a motion
service alongside the light; its mapping and UUID remain stable on reconnect.
The active indication expires 20 seconds after the latest event. Expiry is an
explicit Halo-derived window result, not a fabricated Ring clear notification.
A healthy, continuously qualified session can continue to report no recent event;
source loss makes the observation unknown. Timer tasks are bounded, retriggered,
pruned and joined during shutdown.

Ring's [keypad LED documentation](https://ring.com/support/articles/izp62/Understanding-Ring-Alarm-Keypad-2nd-Generation-Lights-and-Buttons)
distinguishes motion wake, button wake and continuous mode illumination depending
on power. The Ring-MQTT path was qualified upstream on Gen2. Halo does not change
LED patterns, power-saving settings, alarm modes, volume or panic functions.
Whether this owner's keypad emits the proximity event remains a live observation
gate, not something local tests can establish.

## Command and policy boundaries

- The private account/device policy `devices/keypad.json` defaults to disabled
  controls and an enabled brightness lock. Owner opt-in is separate from bridge
  selection. Enabling in the web UI starts locked and issues no physical command.
- Brightness has a dedicated typed coordinator/provider/transport path. It does
  not reuse Beams timed overrides or Off-to-automatic semantics.
- Web, owner IPC and HomeKit share scope, generation, source epoch, state/policy
  revision, qualified capability, fresh authority and final transport admission.
  Only an exact bounded brightness setting is serializable; no generic SetInfo
  or arbitrary keypad commands are exposed.
- Results require fresh post-send Ring readback. Ambiguous send/timeouts stay
  uncertain and are not replayed. Requests never replace observed state.
- Lock writes have their own local lane at web, HAP and parent runtime layers.
  They revoke captured unsent dimmer leases before persistence and do not wait
  behind physical readback. A request already handed to Ring cannot be recalled.
- The lock's own successful save does not invalidate its acknowledgement. HAP
  uses observed write responses and notifications, with coalescing of combined
  On/Brightness transactions to avoid an intermediate full-brightness command.
- The lock governs Halo. Changes from Ring's app, firmware or another bridge
  remain visible; Halo does not repeatedly overwrite them. Siri may report that
  the protected light could not be changed while changing other room lights.

## Web interface

The GTK-style device page reports configured brightness separately from the
slider draft. **Apply brightness** sends one request; moving the slider sends
none. A saved lock remains actionable while brightness is pending and when Ring
is offline but the owner session is current. Owner enable requires recent
password confirmation; ordinary authorized brightness/lock actions use the normal
authenticated/CSRF-protected session. Pending and uncertain outcomes remain
visible, and late replies cannot cross account/device authority changes.

The whole device page remains readable for a keypad without brightness evidence;
unsupported controls stay disabled rather than invalidating the complete state
response. Motion shows Unknown until qualified, then Detected/No recent detection.

## Upgrade and recovery

`keypad_light` and the qualified `(keypad_light, motion)` combination extend the
persisted bridge registry. Existing device UUIDs, selections and exclusions stay
stable, and the new motion mapping is a one-way supported expansion. Reader15
understands this state; reader14 cannot safely load it. Existing reader14 policies
remain parseable, and activation advances the monotonic floor before new mappings
can be written. Older binaries are then refused instead of corrupting identities.

Deployment therefore requires a reader15-compatible recovery build derived from
the previously working baseline. That build preserves keypad registry records and
filters but omits keypad publication/controls, leaving existing camera and other
device behavior intact. Recovery and feature release qualification/deployment
receipts are maintained separately. Neither rollback nor upgrade restores stale
credentials, pairing snapshots or control grants.

## Verification and acceptance

Local coverage includes malformed/finite numeric bounds and rounding, exact
command envelopes, source/freshness/authorization/CAS rejection, confirmation and
ambiguous outcomes, persistent-lock corruption/conflicts, lock-versus-send races,
separate web/HAP command lanes, real HAP combined writes and observed responses,
conditional motion and identity preservation, timer expiry/retrigger/lifecycle,
authentication/CSRF and responsive Firefox/Chromium layouts.

Development tests use fake providers, isolated sockets and synthetic local HAP
accessories. They do not send production alarm or keypad commands. Physical
brightness, Siri behavior and live proximity acceptance are reserved for the
owner. Passive server checks preserve all existing pairings and unrelated grants.
