# Non-camera wave 8: optional lighting groups and shared lighting ownership

This wave follows Chime commits `a668efb` and `a73a0a2`. No real Ring command,
HomeKit publication, production state change, deployment or push occurred.
Detailed schemas and research are in the [group contract](lighting-groups-contract-2026-11-14.md).

## Implemented behavior

REST groups have account/location/group identities independent of names. The
provider discovers them in a separate bounded task; group reads cannot hold up
Alarm/Beams sockets or Chime discovery. Inventory refreshes every 300 seconds and
up to four reports rotate every 30 seconds. Two bounded location workers and a
rotating start position prevent one offline location monopolizing discovery.
Retry-After and local failure backoff apply to lists and reports.

Group membership and report identities are strictly matched. Every member must
join to one exact currently owned REST device by location, positive decimal ID
and raw kind. Duplicate/mixed/unknown ownership remains read-only. A bridge owner
does not confer ownership on every group member. Every ordinary REST receipt or
failure synchronously updates that authority; an older in-flight group list cannot
restore superseded ownership. Membership changes advance the command epoch.

Only an exact CLAP `group.light-group.beams` location/groupId match is an alias of
the canonical REST group. Names, zids and approximate IDs never deduplicate it.
Unknown aliases remain read-only. Complete removals explicitly invalidate old
records even when another unavailable location makes the overall inventory partial.

Groups reuse finite light preferences and the established HomeKit Lightbulb
command path. On requests 30, 60, 300 or 900 seconds. Group Off uses Ring's REST
Off action, distinct from a physical Beams Return to automatic request. Neither
suppresses later motion/schedules. Only actual fresh aggregate readback confirms
a group request; it does not establish every member's state. No local timer sends
an automatic Off and no requested state becomes an observation.

Groups are available in Halo and require explicit individual inclusion in Halo
Bridge even with auto-import. This avoids automatically adding redundant group
and physical-light controls. Inclusion and control permission remain independent;
both discovery and saving preferences are inert. The interface explains aggregate
state and the distinct group Off meaning. A provider namespace is now included in
the sanitized catalog DTO so new group UI checks can require the exact source.

## Shared lighting guard and provenance

One thread-safe reservation belongs to the authenticated Ring client and spans
camera SDK lights, physical Beams and REST groups. Overlapping commands return
busy instead of queueing. The actual operation owns its reservation through
cleanup, including consumer cancellation. Account retirement revokes it.

Each admitted write advances a lighting revision. A cached camera inventory or
group report from before that revision cannot become current after a write.
Camera provenance belongs to the actual inventory GET, including an already
in-flight or SDK-cached read; copying a response cannot stamp it with a newer
revision. Siren observations retain their independent provenance. New current
light state requires a genuinely fresh read or qualified hub event, not an
optimistic SDK cache update.

## Verification and remaining limits

The final provider/group/monitor/Beams/Chime/hub regression passed 358 tests.
The separate lighting/provenance/bridge regression passed 155 tests; the earlier
camera inventory provenance regression passed 88. These suites overlap and are
not additive. Frontend validation passed 53 tests, Svelte checks, formatting and
the production build. Browser fixtures passed group (10), individual-light (9),
device (10) and bridge (7) checks, including six responsive light/dark layouts,
with no page errors. Ruff and diff checks passed. Full-source and installed
qualification are recorded in [wave 9](noncamera-wave9-2026-11-14.md).

Focused fixtures cover exact identity/ownership, bounded and partial discovery,
crossed ownership/list/report reads, retirement, mixed groups, command uncertainty,
slot retention, camera cache provenance, optional bridge selection, stable UUIDs,
separate write grants and HomeKit command dispatch. No fixture result establishes
physical device behavior.

Polling is deliberately bounded. Very large group inventories can exceed the
120-second report freshness window; those reports become unavailable instead of
remaining falsely current. Selected-group prioritization is future scale work.
Brightness, membership editing, shared-user roles without explicit proof and
generic switches/locks remain outside this qualified wave. The existing camera
media transport and credentials/session architecture remain in place.
