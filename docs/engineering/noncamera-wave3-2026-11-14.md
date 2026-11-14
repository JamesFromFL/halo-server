# Non-camera devices: HomeKit bridge and read-only sensors

Status: implemented locally, not deployed or physically paired. The prior registry
wave was committed as `90a2787`; nothing was pushed. This wave implements slice 4
of the [bridge plan](noncamera-bridge-plan-2026-11-14.md), with the pairing and
publication interface from slice 6 brought forward for usable owner controls.

## Published scope and explicit admission

One Halo Bridge publishes qualified contact, motion, leak, smoke and carbon
monoxide sensor services. The existing registry retains each child's identity and
its explicit selection/exclusion policy. Selected missing sensors remain in the
bridge with unavailable readings. They are not silently removed on Ring outages.
HomeKit UUIDs, accessory IDs and characteristic IDs survive reorder, rename,
restart and explicit removal/re-inclusion. Capacity remains 149 children.

Publication has a new, separate owner switch. Registry preparation and auto-import
preferences saved by the previous wave do not start advertising after an upgrade.
Enabling requires current account authority, active monitoring, enabled imports
and HomeKit operating mode. Publication preferences are account-bound. Logging
out retires the bridge; logging back into the same account resumes explicitly
saved publication with the same identities. A different account cannot inherit
another account's paired bridge.

The data contract admits only the five sensor mappings and nullable boolean
readings. It has no provider command, Ring credential, media or panic operation.
All non-camera actuation remains denied, including alarm arming/disarming,
bypass, panic, emergency dispatch, sirens, lighting and chime controls. No startup
actuation was introduced. Cameras remain standalone accessories in their existing
process and pairing store; their transport and codec pipeline were not changed.

Optional HomeKit battery and tamper services are deferred. Adding optional
services based on whichever initial sample arrives would make the accessory graph
unstable across restarts. A later slice must persist graph qualification before
adding them. Their existing observations remain visible in Halo. Freeze is not
represented as a fabricated temperature reading.

## Separate runtime and freshness

Python retains the Ring provider, catalog and policy authority. A dedicated
`device-main.js` process receives bounded cumulative snapshots over a private,
framed socket. The protocol fences account generation, registry revision, bridge
identity, child identity, process boot and monotonically increasing sequence.
It rejects unknown fields, malformed text, unsupported mappings, non-boolean
readings, duplicate identities and oversized graphs. One process serves one
immutable account/bridge identity. A finite restart budget, request deadlines,
bounded pending work and joined retirement contain failures to this bridge.

Publication runs on validated observations and a five-second freshness check.
Snapshots expire in the child within at most 15 seconds even if heartbeats remain
alive. Ring source failure, disappearance, expired inventory or expired sensor
observations produces HomeKit communication failure plus StatusFault. Each sensor
reading uses its own freshness: stale battery data cannot suppress a current
contact, and a fresh battery cannot make an expired contact current. No last-known
boolean becomes a current safe state. HomeKit's actual display of faults and event
latency still requires physical acceptance.

The parent reports publication only after the child confirms listening and
advertisement. A reply from a retired process boot cannot authorize pairing on a
new boot. Account/policy invalidation immediately fences and terminates only the
device child; complete teardown follows asynchronously. Logout retirement is
reusable on relogin rather than permanently closing the coordinator.

Managed resource resolution reuses the existing complete immutable release
manifest verification and inherited release guard. The builder already copies
all compiled adapter source files, including the new entry and reset helper.
The handshake build ID is parent-provided correlation, not independent child
attestation. No second verifier or release schema was introduced. Qualification
uses pinned Node 26.12.1 and the checkout's HAP 2.3.0-halo.1; the deployed camera
version has not been changed by this work.

## Pairing, reset and private storage

The GTK-style Integrations page has one Halo Bridge card, publication status,
password-confirmed setup code reveal, and explicit pairing reset/resume. Sensor
pages link to this bridge instead of exposing individual setup codes. Camera
pairing remains separate. Setup secrets expire, clear on navigation/hidden tabs,
and are discarded when account, registry, publication or runtime authority
changes. They do not enter owner snapshots, activity events or operation journals.
The HTTPS routes retain authentication, exact origin/CSRF checks and bounded
bodies; reveal/reset additionally require recent authentication.

`state/homekit/devices/publication.json` stores the versioned publication policy
and reset journal together. Its enrollment marker fails closed if enrolled state
disappears. Existing profiles cannot be silently removed or replaced. Durable
atomic writes and compare-and-swap revisions use the existing private-file layer.
The previously reserved `pairing-maintenance.json` path is not used: combining
publication and reset denial avoids a cross-file window in which reset is pending
but startup could still publish.

Reset proceeds through `denied`, `quiesced`, `cleanup_started` and `completed`.
Publication is durably disabled before quiescence or cleanup. The parent keeps the
exact exclusive pairing lock across child shutdown and the separate offline
`device-pairing-reset.js` helper. Cleanup is scoped to the bridge's username and
an exact three-file manifest. A changed file is never deleted as part of an old
reset. Interrupted cleanup may resume only the original operation and original
manifest, permitting already-removed files. Other account bridges and camera
pairing files remain untouched. Cancellation joins owned work and preserves the
denial. Startup never resumes destructive cleanup automatically.

A completed reset stays unpublished. Remove the old bridge in Apple Home, then
explicitly enable publication and pair again. An interrupted reset is shown for
owner-confirmed resumption, including after a daemon restart or account generation
change. Its web operation digest uses stable profile/accessory/manifest identity,
so a retired boot does not prevent legitimate resumption.

## Verification and remaining gates

Maintained Python tests cover strict IPC and cross-language acceptance, real child
handshake, finite queues/restarts, cancellation, source and observation freshness,
account/boot fences, logout/relogin, explicit opt-in, private policy enrollment,
reset journaling and real offline helper cleanup. HTTPS tests cover authentication,
CSRF, recent authentication, secret suppression during account/session changes,
operation receipts and interrupted reset resumption. The Node suite exercises the
five real HAP service types, unknown/fault handling, event updates, graph changes,
149-child admission, stable IDs, pairing manifests and malformed frames. UI tests
include actual Python projections as well as desktop/mobile browser interaction.

The final full server run passes **3,422 tests**, with one IPv6-loopback skip and
81 existing dependency deprecation warnings. It was repeated after the final
process-identity and import-write fences; the final run includes all new tests.
All **302 Node tests** pass, including 27 new device bridge tests. Four server
checks execute the actual offline reset helper with namespace/manifest/lock
fixtures. All 363 Python files pass Ruff checks and formatting; adapter type
checking and diff checks pass.

The interface passes **31 unit tests**, zero Svelte errors/warnings, formatting,
production build and regenerated asset manifests. Chromium verification covers
24 existing device layouts plus six bridge layouts, actual Python prepared/
published/reveal/interrupted-reset projections, late secrets, password-confirmed
pairing, explicit publication and same-operation resumption, with zero page errors.

Evidence is in
`/home/james/Documents/Codex/2026-10-25/he/work/halo-server-noncamera-wave3-2026-11-14/`.

Installed upgrade/rollback qualification, pairing with Apple Home, physical sensor
latency and stale-state presentation, reboot/reconnect behavior, and coexistence
with the seven live cameras remain explicit acceptance gates. No live Ring
command, HomeKit publication, server deployment or push was performed in this wave.
Alarm and other verified controls remain the next development family.
