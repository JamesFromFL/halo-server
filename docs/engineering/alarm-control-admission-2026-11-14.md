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

## Installed result

Source `7510fe29777b8f7fc27021d267961f8ac4d16c90` is deployed as release
`ac738b2d7a522e53f4a23fa53ed239d748d7d2ab807e96a7cd43c5f05480d164`, selection
revision 27. Fresh qualification passed 162 focused source tests, 1,092 installed
Python tests with zero failures/skips, 328 adapter tests, and 64 frontend tests.
Chromium and Firefox each passed 60 general layouts, 6 alarm layouts and 10 alarm
behavior checks, including blocked Home/Away explanations with no additional
fixture command writes. Installed daemon and child lifecycle probes passed. All
4,396 unchanged runtime dependency files match the previous release byte for byte
and mode for mode. The earlier full-source suite is explicitly inherited rather
than represented as newly run.

Two local build-harness issues were corrected without changing the package: npm
needed a real local user/home rather than an unnamed sandbox uid; final receipt
assembly needed its unchanged prior runtime proof copied and hash-bound into the
new evidence directory. Original failed logs and the corrections are retained.

The passive production check now reports all three targets (Disarmed, Home,
Away), no target blockers, current panel observations, and a ready paired bridge.
The postcheck confirms every pairing file is byte-identical, all seven camera
pairings and 17 noncamera selections remain present, and owner credentials, TLS,
alarm grants and recording preferences are unchanged. The previous release is
retained for rollback. No pairing metadata exception was needed.

No physical alarm commands or synthetic production events were sent. Actual
Home/Away operation remains for the owner to test. No push was performed.
Evidence: `work/halo-alarm-control-2026-11-14/final-qualification.json`,
`deployment-result.json` and `installed-panel-status.json` in the Codex workspace.
