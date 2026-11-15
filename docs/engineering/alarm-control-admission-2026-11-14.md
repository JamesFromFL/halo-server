# Ring Alarm Home/Away admission repair — 2026-11-14

The owner could see the security panel in Apple Home but Home and Away did not
change Ring's state. Passive status on release `d979e7aa` showed an online,
current, paired panel with the normal-control grant enabled and only Disarmed in
`allowed_targets`. This was a local admission restriction, not evidence of a Ring
subscription requirement or a rejected command from Ring.

## Cause and bounded correction

When Ring omits its optional panel fault list, Halo checks current sensor evidence
before permitting arming. It conservatively blocked every unknown hub record. The
installed inventory includes `access-code` and `access-code.vault` administrative
records, which remained classified unknown. They therefore prevented both Home
and Away despite fresh, clear contact/motion sensor readings. A synthetic replay
reproduces the restriction with those records present and removes it with those
records absent, without invoking a command.

The exception is exact: those two raw types, unknown kind, and only administrative
health capabilities (battery, AC power, tamper). An unexpected fault, bypass,
sensor, alarm or actuator capability prevents the exception. Unknown types and
new sensor types remain blocked. A clear, current supported sensor inventory is
still required; no sensor-free assumption, bypass, optimistic state, policy grant,
subscription inference, automatic retry or startup command is added.

The pinned Homebridge implementation independently excludes these types from
physical accessories. See [its hidden-type list](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/homebridge-ring/ring-platform.ts#L54-L62)
and [Ring type names](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/ring-client-api/ring-types.ts).
Local reviewed source is retained in the noncamera research evidence.

## Explanation without changing HomeKit identity

`can_set_mode` means at least one normal mode can be requested, not that all modes
are admitted. A bounded per-target reason projection lets the owner console
explain unavailable modes even when Disarm remains available. Unknown provider
reason text is not displayed. Target restrictions still apply at the last
pre-send admission check. HAP's standard supported-mode descriptor stays stable;
this repair does not rebuild accessories or reset pairing.

## Validation boundary

All automated arming/disarming scenarios must use fake providers/transports in
isolated local tests. The owner explicitly prohibited real state-change tests.
Production verification reads current capabilities and service status only. No
arm, disarm, siren, panic, bypass, synthetic production alarm event or phone-alert
test is part of this work. Real state changes remain for the owner to test.

Qualification and passive deployment results will be appended after completion.
