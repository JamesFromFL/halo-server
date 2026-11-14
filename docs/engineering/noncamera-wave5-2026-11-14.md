# Non-camera wave 5: individual Smart Lighting

Date: November 14, 2026. Status: implemented locally; installation and physical
acceptance remain pending. The preceding Alarm wave was committed as `ffbfee7`.
This wave is not deployed or pushed. No real Ring commands, HomeKit publication,
pairing changes or server modifications were performed during development.

## Scope and semantics

Support individual physical hub children whose exact model is `switch.beams`,
`switch.multilevel.beams` or `switch.transformer.beams`. Normalize them as
`beams-light`, retaining their existing device identity. Require known hub schema
evidence and an exact `beams_bridge_v1` asset for writes. Other models, unknown
asset versions, groups and generic switches do not inherit actuator permission.

Expose observed binary state in Halo and as a HomeKit Lightbulb. A per-account,
per-device control grant defaults off. The saved On duration is 30, 60, 300 or
900 seconds, defaulting to 60, and applies to both interfaces. Saving the grant
or duration never actuates a device. Enabling requires recent owner web
authentication; normal light toggles require authenticated, scoped requests but
do not repeat the password prompt.

On sends `light-mode.set` with `lightMode: on` and the saved duration. Off sends
`lightMode: default`, returning to automatic operation. It does not permanently
disable motion lighting. A fresh explicit post-send actual `on` matching the
target is required for confirmation. An Off request can therefore remain
uncertain while motion keeps the lamp on. We have not qualified an authoritative
`lightMode` readback, so no operating-mode state is manufactured. No local timer
pretends that the physical light went off after the requested duration.

Brightness writes, embedded motion services, groups, camera lights, generic
switches/outlets and Chime controls are outside this wave. Panic, emergency
dispatch and siren controls remain outside the permitted command path.

## Provider, authority and recovery

Reuse the existing Python account/session owner, discovery and CLAP connection.
No additional Ring login, token writer, socket service or Node credentials are
introduced. Join ticket `doorbotId` to REST `beams_bridges.id` within the same
location and require explicit `role: owner`. Missing or ambiguous correlations,
unknown role and shared role remain read-only. Ownership is not inferred from
`owner_id`, location membership or an unrelated Alarm base station.

Ring documents shared-user Smart Lighting control. Owner-only writes here are a
conservative implementation qualification boundary, not a Ring product limit.
Shared-role support needs separately qualified payloads and permission tests.

Commands have one in-flight operation per device, finite deadlines and exact
account/provider/epoch/observation/policy fences. Final validation occurs inside
the transport send lock. Current explicit light and connectivity observations
are required; an unrelated old battery value does not block a fresh light.
Disabled/replaced policies revoke already captured admission before persistence.
Re-enabling cannot revive a previously queued request.

A possibly sent command is never automatically replayed. Lost or cancelled
transport responses become uncertain and require fresh complete inventory before
another write. Readback has an independent monotonically advancing counter.
Replies recheck newer actual state, so an older matching observation cannot hide
a subsequent contradiction. Owned operations survive a browser/adapter request
disconnect long enough to settle and preserve their outcome.

Review found a shared cancellation bug in the existing Alarm coordinator: a
cancelled provider future could leave an unsettled receipt and interrupt cleanup.
Both coordinators now report that ambiguous result honestly. Nested monitor
cleanup also guarantees that an Alarm cleanup failure cannot skip lights or the
bridge. Regression tests cover both domains.

## Bridge and interface

Use a stable `halo:lightbulb` service with actual reported On. Unknown or stale
state yields a communication error/fault, never fabricated Off. Control remains
separately opted in. Pending observations do not revoke their own command or
replace the requested result with an unchanged older cache value. Newer actual
state wins over an older completion callback.

The adapter shares a bounded command dispatcher with alarms: four total pending
requests, one per device, correlated replies and a 15-second timeout. Existing
identity, registry sequence, account, boot and expiry checks apply to lighting.
Rejected replies carry no invented observation. Python waits do not block IPC
reading. The device child receives no Ring credentials.

The pinned HAP serializer omits a falsy write-response value. Tests verify the
full HAP request path nevertheless preserves actual false in cache, reads and
notifications. No HAP fork or camera media change was introduced for this wave.

An old unmapped `switch` registry entry can be enriched into a qualified Beams
Lightbulb without changing its UUID. The migration requires the complete mapping
qualification; unsupported or non-hub records cannot corrupt reconciliation.
Historical switch-family exclusions continue to cover these enriched lights in
both backend selection and web import controls.

The GTK-style web panel separates observed state, authority, saved controls and
the requested action. Preferences remain drafts until Apply. It explains the
return-to-automatic behavior, never substitutes requested for actual state and
does not display a fabricated countdown. Uncertain operations can be checked or
acknowledged against newer authoritative state without resending the action.
Account or provider changes discard stale asynchronous responses.

## Verification and acceptance

Automated verification completed:

- Full server suite: **3,651 passed, 1 skipped**, zero failures/errors, in 278.46
  seconds. The skip is the existing IPv6 loopback case because this qualified host
  has no IPv6 loopback address. Existing upstream GI/asyncio and Ring BasicAuth
  deprecations produced 81 warnings.
- HomeKit adapter build and **323 tests passed**. Frontend formatting, Svelte
  checks, production build and **45 unit tests passed**.
- Four isolated browser suites passed: lighting (9 checks), devices (10), Alarm
  (9) and bridge (7), with no page errors. Lighting includes six responsive
  light/dark layouts and an actual Python-generated synthetic DTO. Desktop dark
  and phone light screenshots were inspected for legibility and overflow.
- Ruff lint/format checks passed for **375 source and test files**;
  `git diff --check` passed.

The full server suite used the qualified pinned Node runtime and native media
libraries with the real host ownership view for release-directory checks. Test
guards and validation were not weakened to accommodate the sandbox namespace.
`verification.json` records the base commit, changed-file hashes and browser
receipts for the next commit review. These are fixture and local runtime checks,
not physical Ring or installed Apple Home acceptance.

Evidence directory:
`/home/james/Documents/Codex/2026-10-25/he/work/halo-server-noncamera-wave5-2026-11-14/`.

Focused coverage includes real provider normalization through monitor, owner API,
policy and transport with a fake socket; duration reaches the wire unchanged.
Tests also cover private policy persistence/CAS, malformed payloads, old source
observations, queueing, role/epoch changes, lost/cancelled results, readback
barriers, current-state publication and identity/exclusion migration. Browser
checks include an actual synthetic Python coordinator DTO, responsive light/dark
layouts, uncertain outcomes and late account responses.

Still required before claiming physical support:

- Confirm ticket/REST bridge correlation, role fields and exact model payloads on
  real hardware, including accounts with lighting but no Alarm base station.
- Observe actual On, finite override expiry and return to automatic while motion
  or Ring schedules are active. Confirm uncertain Off remains honest in Home.
- Exercise power loss, reconnect/full inventory, provider retirement and lost
  acknowledgements without replay or false success.
- Pair Halo Bridge and verify identity across restart, dynamic import and saved
  exclusions, with all seven existing cameras and their pairings intact.
- Qualify shared-user permissions and additional model capabilities separately.

## Remaining work and references

Lighting groups need deduplicated REST/CLAP identities and shared arbitration
with individual camera lights. A bounded shared-auth REST write boundary is a
prerequisite for those groups and Chime controls. Chime discovery also needs
`alerts.connection` normalization; current `connection_status`/`online` checks
do not fully describe real Chime connectivity. Start its controls in Halo's GUI
and qualify volume, explicit sound tests, snooze, tones and automatic nightlight
settings independently. Do not copy fake constant-state HomeKit switches.

- [Pinned Homebridge Beams implementation](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/homebridge-ring/beam.ts): finite On and default-mode Off semantics.
- [Pinned Ring client types](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/ring-client-api/ring-types.ts): physical model names, bridge role and ticket fields.
- [Ring shared users and Smart Lighting](https://ring.com/support/articles/0l486/Shared-Users-and-Alexa) and [permission management](https://ring.com/support/articles/clv68/managing-permissions-for-users): account roles remain distinct from Halo's initial qualification boundary.
- [Main bridge plan](noncamera-bridge-plan-2026-11-14.md) and [project comparison](ring-project-comparison-2026-11-14.md): family sequencing and acceptance requirements.
