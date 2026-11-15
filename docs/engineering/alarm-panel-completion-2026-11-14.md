# Ring Alarm panel completion — 2026-11-14

The installed panel was already selected in a paired Halo Bridge. Its mode,
connection, siren and tamper observations were current, but optional alarm fields
were absent from full Ring inventories. Halo normalized that shape to stable/no
alarm without giving those observations receipts. The panel consequently remained
unavailable in HomeKit and its normal controls stayed blocked.

This work completes that observation path and the notification delivery path.
The user explicitly prohibited real state-change testing. All automated alarm
scenarios use isolated fake providers/transports or an unadvertised in-memory HAP
fixture. No physical arm/disarm, siren, panic, bypass, simulated live bridge event,
or phone-alert test is authorized by this qualification.

## Observation contract

- A complete security-panel inventory with a recognized `none`/`some`/`all` mode,
  omitted optional alarm fields, and valid transition metadata now supplies fresh
  stable/exit-delay and no-alarm receipts. This follows Halo's existing complete
  inventory replacement semantics, not a new assumption about sparse messages.
- Sparse mode, siren or tamper updates cannot clear a retained alarm or renew its
  safety receipts. A transition-only update can refresh the applicable countdown
  phase; it cannot refresh unrelated retained alarm evidence.
- An explicit `alarmInfo: null` clears the optional alarm on a known panel;
  maintained Hubitat source documents this Ring wire behavior. Malformed non-null,
  empty or unrecognized alarm fields remain unknown, and active alarmStatus still
  takes precedence. Invalid transition metadata cannot look like a cleared countdown.
- Disarmed, Home and Away are main modes. Entry and exit countdowns remain separate
  phases. Ring must confirm their completion; a local timer does not invent it.
- Actual burglar, fire, CO, combined life-safety and panic observations map to
  HomeKit's Alarm Triggered state. Entry and exit countdowns do not. Unknown or
  disconnected observations never become Disarmed.

Maintained implementation evidence: [Ring types and optional fields](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/ring-client-api/ring-types.ts),
[Homebridge panel mapping](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/homebridge-ring/security-panel.ts),
and [Ring-MQTT countdown mapping](https://github.com/tsightler/ring-mqtt/blob/59aab41b4afd1692148b1180a6b0920591c86db1/devices/security-panel.js).
Ring's hub protocol remains undocumented. Upstream also shallow-merges some full
refreshes, so arbitrary omission is not a universal clear signal. The new receipt
rule is narrowly limited to Halo's already-authoritative inventory path and the
stable shape observed on the installed panel. The explicit-null clear is documented
in [Hubitat's Ring message handler](https://github.com/HubitatCommunity/hubitat_ring_integration/blob/334738136f4b91cc259edffd208ecdc7439a8b59/src/drivers/ring-api-virtual-device.groovy).
The official Home Assistant Ring
integration does not implement the Alarm panel.

## Timely, ordered alarm notifications

The old publisher coalesced all observations behind one wake flag. An alarm could
trigger and clear while a publication reply was pending, leaving only the clear
snapshot. The publisher now captures distinct alarm transitions synchronously,
before the next provider batch overwrites the catalogue. A bounded FIFO preserves
order across locations, then publishes the latest state. It holds at most 64
observations for 15 seconds; overflow, expiry and unconfirmed delivery are counted
and surfaced in the bridge UI.

History is fenced by account, provider binding, source epoch, selected graph,
accessory identity and adapter boot. Admission rechecks these and expiry at the
actual IPC send. One location's reconnect does not discard another location's
valid events. Historical observations carry only mode and trigger values. Control permission
always comes from the current policy/source snapshot, and history never replaces
the current snapshot used to admit a new command. Temporarily forcing permission
off would incorrectly revoke a pending normal command when its readback arrived;
the final implementation preserves that live permission without restoring an old
grant. Repeated identical
observations do not produce repeated alarm events. Retired history is not replayed
on startup or reconnection.

HAP 2.3.0-halo.2 adds only SecuritySystemCurrentState to the immediate notification
list. It retains the previous audited media/storage patches. The default 250 ms
batching for ordinary events remains unchanged, as do subscription checks,
encryption, pairing and distinct-event ordering. An in-flight HTTP response still
completes before event bytes; immediate does not mean interleaving protocol data.
The immutable archive, source patch, lock integrity and reproducibility provenance
are stored under `homekit-adapter/vendor`.

Halo sends truthful security-system events without deliberate alarm batching. It
cannot choose Apple Home's APNs severity, sound or text, enable phone permissions,
or confirm phone receipt. Users must enable the security system's notifications
in Apple Home and the relevant notification/Critical Alerts permissions on each
device. Remote notifications require an Apple home hub. See [Apple Home notifications](https://support.apple.com/en-us/105042)
and [Apple notification settings](https://support.apple.com/en-us/108274).

## Controls and interface

The UI distinguishes the observed mode, temporary phase and alarm category. An
online device with some stale readings is no longer labeled as though its entire
connection were out of date. The alarm page explains notification permissions and
that monitoring remains independent of the normal-control grant.

The existing normal-mode grant, owner verification, freshness, serialized command
admission, readback and uncertainty guards remain in place. No startup command,
automatic retry, bypass, panic or dispatch control has been added. Active alarm
cancellation remains outside this control scope and continues to require Ring's
app/keypad. Night mode is not invented or silently translated into Home.

## Qualification and acceptance boundary

Initial focused qualification: 59 isolated Python publication/fencing tests, 64 private IPC runtime tests,
328 adapter tests with zero network attempts, 63 frontend unit tests, and a clean
Svelte check. The expanded hub/provider/alarm suite passed 314 tests before the
final two send-race cases. Chromium and Firefox each passed nine alarm UI checks
and six responsive layouts with no page errors. Installed-package results are
recorded with final deployment evidence. The HAP dependency built identically
twice; all 283 installed files match its archive, and its only payload changes
from halo.1 are the package version, notification condition, and source maps.

Production verification is passive only. Deployment must preserve the enabled,
paired device bridge, all camera pairings, alarm grants and import selections.
The HAP firmware version marker may migrate from halo.1 to halo.2; no other pairing
record change is accepted. Physical normal-mode behavior and phone alert delivery
remain for the user's later test, and are not claimed from synthetic results.

Local evidence roots: `work/halo-alarm-panel-2026-11-14`,
`work/halo-alarm-notifications-2026-11-14`, and
`work/halo-alarm-completion-2026-11-14` in the Codex workspace.
