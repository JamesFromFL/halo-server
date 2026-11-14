# Lighting-group contract and remaining actuator qualification

This research and fixture implementation follows physical Beams wave 5. It does
not establish live Ring permissions, exact model compatibility, physical lamp
behavior or HomeKit acceptance on the deployed server.

## Source evidence

Halo GTK's `light_groups_rest.py`, `light_group_models.py`, `light_groups.py`,
`lighting_actions.py` and `doc/light-groups-support.md` provide the strict REST
schema, finite-duration controls, independent member reports, explicit uncertain
outcomes and one account-wide camera/group reservation. The server adapts those
contracts to unattended monitoring, typed provider records and shared ownership
with individual Beams controls.

Homebridge Ring current pin `152ea689e3bdc14632dd0e98dd14358cb376f8cc` confirms
the group POST in `packages/ring-client-api/location.ts` and the physical/group
split in `packages/homebridge-ring/beam.ts`. Stable `14.3.0` at
`3bcc624acc35b6f97c391089a12bff582bb6ed4a` supplies the inspected generic switch,
outlet and multilevel adapters. Python Ring 0.9.14 `group.py` independently
contains the same list/report/control endpoints and explicit report refresh.
The local earlier Scrypted Ring `location.ts` comparison shows typed light,
switch and lock adapters; the broader pinned-project research remains in
[the comparative research](ring-project-comparison-2026-11-14.md).

Primary source entry points:
[Homebridge lighting](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/homebridge-ring/beam.ts),
[group request](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/ring-client-api/location.ts),
[stable switch](https://github.com/dgreif/ring/blob/3bcc624acc35b6f97c391089a12bff582bb6ed4a/packages/homebridge-ring/switch.ts),
[stable multilevel](https://github.com/dgreif/ring/blob/3bcc624acc35b6f97c391089a12bff582bb6ed4a/packages/homebridge-ring/multi-level-switch.ts),
[lock mapping](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/homebridge-ring/lock.ts).

## Identity, observations and authority

Canonical identity is profile + `ring-light-group` + exact location ID + exact
REST `device_group_id`. Display names never establish identity. CLAP
`group.light-group.beams` is an alias only when its retained `groupId` and exact
location match that canonical group. Do not merge by zid, name or approximate
numeric ID, and do not publish a second writable CLAP group accessory.

Inventory requires a complete `device_groups` list. Each row has an exact
location, `device_group_id`, name and complete `devices` membership. Members use
integer `doorbot_id`, exact location and type. Reports use `device_group_id`,
optional matching location, an optional boolean aggregate `lights_on`, and
member rows with canonical decimal string `id` plus independently optional
boolean `lights_on`. Duplicate identities, pagination/truncation hints, deletion
ambiguity and mismatched membership invalidate authority without inventing an
empty inventory. Unknown aggregate/member values stay unknown.

Writable groups require nonempty membership and a fresh complete inventory.
Every member joins by exact `(location, decimal REST ID, member kind)` to one
currently owned REST device. Duplicate, missing, mixed or uncertain ownership
stays read-only; a shared location or Alarm owner's role alone does not prove
ownership of all group members. A Beams bridge owner alone is insufficient
without a separately verified member-to-bridge join. User opt-in grants local
permission only; it cannot manufacture Ring-side authority.

Provider records use `family=lighting-group`, `kind=light-group`,
`model=Ring Light Group`, capability `switch` and evidence
`ring-light-group-schema`. The `light_on` observation is the group aggregate.
Member states remain separate in the provider and are not used to synthesize
individual camera/light observations. This wave's UI presents the aggregate. Binary HAP
Lightbulb can reuse the existing confirmed-light command DTO and truthful HAP
write response; it must be labelled/explained as a group.

## Controls and overlapping actions

On sends one POST with `lights_on.enabled=true` and a configured finite duration
of 30, 60, 300 or 900 seconds. Off sends `lights_on.enabled=false` without a
duration. Off is a REST group action; it is not the physical Beams
`lightMode=default` command. Neither promises to suppress subsequent motion,
schedules or linked rules. Duration is request policy, not a proven shutoff
deadline. Never send local timer-driven Off or infer physical Off from elapsed
time.

`RingClient.lighting_reservation` spans camera light SDK calls, physical Beams
commands and group requests. It rejects overlapping actions as busy and never
queues actuator intents. Consumer cancellation retains the slot through actual
SDK/transport cleanup. An admitted mutation invalidates prior camera-light
observations; a camera GET crossing the mutation cannot restore those values.
Siren state and controls remain separate.

The same reservation revision invalidates cached group aggregates/member reports
after another lighting mutation and rejects GETs that crossed such a mutation.
During its own pending command the group may continue showing the last observed
value; only a fresh report can confirm the new value or survive command cleanup.

Group commands preserve account/provider generation, exact group identity,
membership epoch, source revision, live local policy and reservation admission.
One POST is followed by at most two fresh group report GETs within the existing
10-second command budget. A pre-POST GET cannot count as readback. Reported
aggregate match confirms the group command only; it never confirms every member.
No POST retry, alternate payload, rollback or stale success is permitted. An
ambiguous write remains uncertain until a later fresh report. HTTP permission
denial stays disabled for that membership lifetime; GET success alone cannot
erase a write denial. Retry-After constrains subsequent requests.

Provider integration must call synchronous `update_ownership(ownership,
locations=..., valid=...)` on every newer REST authority receipt or failure,
before publishing it. Waiting for the 300-second group discovery schedule would
retain superseded owner proof. Invalid context revokes authority immediately.
Both list and report requests honor Retry-After, with bounded local backoff for
schema/network failures and longer backoff for denied/authentication responses.

## Bounds and unattended integration

Discovery permits 16 locations, 128 groups per location, 512 cached groups,
128 members per group and 8192 members total. Two discovery workers share the
existing authenticated HTTP budget, with 10-second per-location and 30-second
overall limits. Rotating start order avoids starvation. Overall timeout faults
only unfinished locations and preserves already completed healthy subsets.

Group discovery runs in its own provider-owned background task and does not
block camera, hub or Chime discovery. Refresh inventory around 300 seconds and
poll reports with a bounded rotating budget (default four, at most sixteen per
call) around 30 seconds. Report freshness is 120 seconds; inventory freshness is
600 seconds. Large inventories may truthfully show stale groups when the bounded
poll budget cannot cover them. Discovery/read failure never removes the registry
identity or resets pairing. The group task and command owner close before their
shared gateway; disabling noncamera monitoring does not retire the camera
client's lighting reservation.

Provider composition validates camera rows for member ownership even though
camera accessories are excluded from the device bridge. Ownership joins exact
raw location, canonical positive decimal device ID and exact raw kind; duplicate
IDs at a location are ambiguous even across REST families. The literal owner
flag, or the exact physical row's documented owner role when no contradictory
owner flag exists, is required. Group location receipts use the same strict
completeness and pagination validator as group inventories. Invalid receipts
retain faulted known groups and revoke controls immediately.

The hub store retains only a bounded literal `groupId` for deduplication. A CLAP
alias disappears only when that exact field and location match a canonical REST
group. Proven group removals publish explicit unavailable records while an
unrelated offline hub makes the overall catalog partial. Combined records retain
the existing 1024-device bound; a rejected oversized projection publishes no
current light control capabilities.

Fixture verification covers camera-only accounts, mixed and ambiguous ownership,
revocation before awaited hub cleanup, stale in-flight discovery, independent
poll schedules, partial removal, bounded CLAP aliases, shared Beams/group/camera
reservations, source cleanup, and the actual shared REST transport using synthetic
HTTP responses. These checks do not prove a particular Ring account exposes the
member ownership needed for group writes; unproven members remain read-only.

## Deferred families: specific qualification gaps

| Family | Observed upstream contract | Remaining Halo qualification |
| --- | --- | --- |
| Physical Beams brightness | Exact `switch.multilevel.beams`; numeric `level` 0..1; DeviceInfoSet `{device:{v1:{level:percent/100}}}` | Retain typed finite level and per-field receipt, reject booleans/out-of-range values, confirm fresh quantized actual level, preserve durable characteristic graph and bounded slider intents. No group brightness endpoint is established. |
| Generic switches | Exact `switch`; DeviceInfoSet `{device:{v1:{on:boolean}}}` | Exact model/category and current owner/state proof; no arbitrary unknown Z-Wave fallback. |
| Outlets | Homebridge selects `switch` with category 1 and sets `OutletInUse=Boolean(on)` | That value is a powered-state proxy, not measured load. Qualify the HomeKit characteristic semantics/source rather than inventing load evidence. |
| Multilevel lights/fans | `switch.multilevel.bulb`; `switch.multilevel` is category-dependent (lights 2, fans 17) | No category fallthrough that turns unknown actuators into lights; typed level/On source and stable graph first. |
| Locks | Observed `locked`, `unlocked`, `jammed`, `unknown`; `lock.lock` / `lock.unlock` | Upstream broad `lock.*` regex is insufficient for unknown Halo actuators. Require qualified exact types/category and fresh state, separate default-off control grant, bounded confirmed writes with no replay. Jammed/unknown must never become secured. |

Unsupported inventory stays visible and read-only. These gaps are not evidence
that arbitrary models can be safely controlled merely because another bridge
offers a generic accessory class.
