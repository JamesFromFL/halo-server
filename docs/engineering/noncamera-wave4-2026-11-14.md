# Non-camera wave 4: observed alarm state and guarded normal modes

Status: implemented locally; regression qualification is recorded below. No push,
deployment, physical alarm command or Apple Home acceptance test is part of this
wave. The prior sensor bridge was committed as `3c01ac5` before this work began.

This implements the alarm family of slice 5 in the
[bridge plan](noncamera-bridge-plan-2026-11-14.md). Chimes, lighting and other
verified device controls remain separate waves. Cameras, media transport and their
pairing files are unchanged.

## Behavior and limits

Qualified Ring `security-panel` devices now map to a `SecuritySystem` child of
Halo Bridge. Existing registry identities are enriched in place; sensor and bridge
UUIDs are preserved. Automatic import only exposes observed alarm state. A separate
per-account/location grant defaults off and must be explicitly enabled before a
normal mode request can reach Ring. Neither preference loading nor startup sends
any alarm command.

Supported targets are Disarmed, Home and Away. Night is omitted rather than
silently mapped to another mode or contact bypass. Current mode, entry/exit delay,
alarm phase and signal remain source observations; requested target is separate.
Unknown/offline/stale observations remain unavailable and never become Disarmed.
No countdown is fabricated where the current normalizer has no qualified source.

The existing conservative panel policy is retained: active alarms, ambiguous
panels, incomplete inventory, unknown authority, stale connections and cellular
backup do not admit normal controls. Arming also refuses faulted/unknown sensor
lists and active entry/exit transitions. When Ring omits a fault list, arming is
allowed only with a complete, fresh inventory of the same asset, at least one
recognized bypass-capable sensor, and explicit fresh non-faulted observations
from every such sensor. Unknown device kinds, stale/offline sensors and missing
fault observations refuse this fallback. There is no bypass parameter, panic,
emergency dispatch, siren action or active-alarm cancellation path. Those flows
continue to require the Ring app/keypad. Optional battery/tamper HAP services and
expanded shared-user role qualification are still deferred.

## Authority and source evidence

The monitoring-only provider previously forced all write authorization to unknown.
This wave preserves the native GTK helper's explicit literal `owned: true`
evidence from a matching Alarm base-station row. Ownership of a lighting bridge,
inventory access, a missing field or `owned: false` cannot authorize an alarm.
Mixed/unknown Alarm ownership stays unknown.

Ring's [current user permission table](https://ring.com/support/articles/clv68/managing-permissions-for-users)
lists owner mode controls separately from the compatible-subscription requirement
for digital arming/disarming. The owner flag is therefore treated as account-role
evidence, **not proof of a paid entitlement or successful control**. Ring still
enforces the actual request, and only observed readback confirms it. Halo adds no
blanket paid-plan gate and does not call a timeout a subscription denial. The
upstream [Ring client normal-mode implementation](https://github.com/dgreif/ring/blob/main/packages/ring-client-api/location.ts)
provides the narrow switch-mode/readback reference; the prior pinned research and
GTK normalization remain the implementation baseline.

## Command ownership and confirmation

`devices/alarm.py` owns owner/HomeKit admission, explicit grants and duplicate
operation receipts. `devices/alarm_commands.py` owns provider-loop serialization,
transport admission and readback. Both use the existing authenticated Ring client
and hub socket; no additional credentials, login or token writer is introduced.

Commands are location-wide, with no second queued mode change. They carry current
account and provider generation, panel epoch and a nonrepeating panel revision.
The final socket-send guard rechecks policy, source identity, panel state, role
authority and transport epoch. Revoking the preference clears the current lease
before the disk write. A write failure or cancellation leaves authority denied
until private policy state is reloaded.

An accepted socket write does not change observed mode. Confirmation requires a
fresh, post-send mode field from the same current panel/asset/epoch. The total
provider budget is 10 seconds, reserving part for a read-only inventory request;
bounded socket retirement can add at most two seconds. Sent-but-unconfirmed work
returns uncertain, blocks another request, and clears only after a fresh full
panel inventory. An interrupted browser/HAP request does not cancel and silently
replay a possibly sent Ring action.

The only new child-to-parent operation is `alarm_set`, with a strict finite target
enum and exact account, boot, registry, snapshot, device and accessory identity.
No arbitrary RPC or raw Ring request is expressible. The parent keeps reading
publication/status traffic while awaiting confirmation. Pending requests and
deadlines are bounded, and a retired child cannot reuse a current command.
HomeKit's SecuritySystem characteristic graph remains stable; disabled control is
enforced by the setter and parent, not by repeatedly changing its descriptor.

## Interface and persistence

The GTK-style panel detail shows observed state, account authority, separate Halo
control permission, requested target and uncertain/pending status. Enabling and
each web mode request require an explicit confirmation and recent password
authentication. Disabling is always available to an authenticated owner. HomeKit
controllers use their existing pairing authority plus the same saved Halo grant
and provider checks.

`state/devices/alarm.json` stores only bounded account/location grants and a CAS
revision, using the existing atomic 0600 private-file store. It contains no startup
action, credential or provider payload. Missing or corrupt state denies control
without stopping passive sensor monitoring. HTTP mutations retain same-origin,
CSRF and exact-body checks. Their durable operation journal classifies uncertainty
as unknown and prevents replay. Daemon-local duplicate receipts are bounded to
256 current-state operations. Completed receipts are pruned only when the provider's
nonrepeating state tokens make their original requests permanently stale, preserving
replay rejection without exhausting a long-running session. Outstanding or still
current-state receipts are never evicted to admit more work.

## Verification

Qualification includes provider/protocol/transport fixtures; composed daemon and
HomeKit admission; actual isolated Node child RPC; private policy CAS/failure
paths; HTTPS authentication, recent-auth and no-replay behavior; stable registry
identity; and browser tests against synthetic and actual Python owner projections.
Final local results:

- Full Python server regression: **3,532 passed, 1 skipped** in 276.05 seconds.
  The 81 warnings are existing upstream Ring/aiohttp `BasicAuth` deprecations.
- An additional composed provider-to-daemon-to-owner test: **1 passed**, using a
  synthetic Ring socket and the real provider normalization/readback path. This
  test was added after full-suite collection and is recorded separately.
- Isolated HomeKit adapter suite: **311 passed**; focused Python child runtime
  and protocol suite: **69 passed** (also covered by the full server run).
- Frontend unit suite: **39 passed**; formatting, Svelte checks and production
  build passed. Browser checks covered 10 alarm behaviors and six responsive
  light/dark layouts against the actual Python DTO, plus 24 existing device/camera
  layouts and six bridge layouts. No browser page errors were recorded.
- Ruff lint/format checks passed for all **366 Python source/test files**;
  `git diff --check` passed.

Review regressions include revocation before a policy/monitor disk write, source
lease retirement on malformed state, full-readback-only recovery of uncertain
commands, bounded duplicate receipts, and preserving a pending HomeKit command's
authority across ordinary state publication without reviving revoked commands.
`verification.json` records the base commit and changed-file hashes alongside the
test reports and browser receipts.

Evidence directory:
`/home/james/Documents/Codex/2026-10-25/he/work/halo-server-noncamera-wave4-2026-11-14/`.

Before calling installed support complete, qualify upgrade/rollback and bridge
pairing on the server, then perform an explicitly supervised alarm test with
current source permissions. Verify normal mode convergence, faulted-sensor
refusal, reconnect/reboot, stale-state rendering, and coexistence with all seven
cameras. No fixtures substitute for physical alarm or Apple Home behavior.
