# Non-camera devices: durable Halo Bridge preparation

Status: implemented locally; verification results below. This is slice 3 of the
[bridge plan](noncamera-bridge-plan-2026-11-14.md), with its owner interface pulled
forward so preferences can be inspected and tested. No server deployment, HomeKit
publication, pairing reset or Ring control command is part of this wave.

The prerequisite web/snapshot work was committed as `bbe1f18` and the read-only
provider/catalog first wave as `a906ebe`. Those commits were kept local.

## Identity and import policy

Each explicitly configured credential profile gets a distinct bridge UUID and
HomeKit username. An account change never puts a new account's devices behind a
previous account's paired identity. A retained profile recovers its own identities;
runtime account generations fence stale requests and observations independently.
No credentials, display names, sensor observations or pairing secrets are written
to the registry.

Child identity follows the catalog's account/provider/location/asset/device/endpoint
key. Renames, discovery order, temporary absence and restarts preserve its UUID.
Missing children are retained; a selected missing child reserves its bridge slot.
Exclusion changes selection, without deleting identity or reallocating a UUID.

The owner can prepare the bridge, opt into automatic sensor imports, explicitly
include individual devices, and exclude devices, locations, device types or sensor
capabilities. Exclusions take priority over automatic and explicit inclusion.
Auto-import is off until explicitly enabled. Preparation remains separate from the
read-only monitoring switch and does not start HAP publication.

The fixed v1 candidate mapping includes contact/retrofit contact, motion, flood,
smoke and carbon-monoxide sensor schemas, including a separately identified smoke/
CO listener. Qualification requires the exact provider, device kind, capability
and provider schema evidence. Unsupported devices remain cataloged with reasons;
they do not become generic switches. This is schema qualification with fixtures,
not a claim that every physical Ring model has passed live acceptance.

Two recovery details were improved during review:

- A sensor initially lacking capability evidence can qualify on later discovery
  while keeping its UUID and stable device classification.
- If later evidence conflicts with a previously qualified mapping, that rejection
  is retained. Disappearance cannot make it eligible again; matching authoritative
  evidence is required to recover. The existing mapping identity is preserved.

Capacity is explicit: 149 selected children per account bridge, 1,024 retained
device identities across profiles, and 16 retained account bridge identities.
Existing selected children take priority over new arrivals. Overflow appears in
the import preview; no existing identity is silently evicted. Filters have bounded
sizes compatible with the existing 64 KiB HTTP and local RPC request limits.

## Storage and lifecycle

The new private subtree is `state/homekit/devices/`:

- `registry.json`: strict version 1 identities, qualification and import policy.
- `.registry.json.enrolled`: durable enrollment marker. If an enrolled registry or
  its marker disappears, admission fails closed instead of minting replacement
  identities. Interrupted first enrollment can therefore require operator repair.
- `pairings/`: reserved independent HAP storage for the next runtime slice.
- `pairing-maintenance.json`: reserved path for a separate bridge reset journal.

The marker is written and synchronized before initial enrollment. Registry writes
use owner-only files, pinned paths, exclusive locks, revision checks, atomic replace
and directory synchronization. Corruption, unsafe paths and uncertain writes
preserve the files and hide cached enabled preferences until a successful reload.
There is no automatic destructive repair or implicit identity reset.

The pairing directory has its own parent and ownership lock, avoiding collision
with the camera adapter's process-global storage and `.pairings.owner.lock`.
Preparing camera storage does not initialize device storage. A future device child
has a separate runtime directory at `runtime/homekit/devices/`.

Actual bridge pairing reset is deliberately deferred to slice 4: it requires the
separate child to stop admission, quiesce publication and execute manifest-checked
cleanup. The camera reset journal cannot safely represent bridge-wide reset, and
this wave exposes no reset button or half-working cleanup endpoint.

Daemon metadata reconciliation is coalesced and kept off the event loop. Ordinary
sensor value changes do not trigger registry transactions. Shutdown joins any
in-flight disk write. Failed registry preparation cannot stop the camera pipeline
or read-only non-camera monitoring. Old-account callbacks and requests are fenced.

Older releases ignore this separate subtree. Configuration operations use the
existing journal kind and a distinct target digest, avoiding a journal schema
change on rollback. Synthetic release activation, rollback and uninstall checks
preserve both camera and bridge stores byte for byte.

## Owner interface

The GTK-style Devices page now includes a Halo Bridge preparation/import card,
capacity counts and exclusions. Generic device details offer per-device inclusion
and eligibility reasons. The interface explicitly says publication and pairing are
not available yet; no QR code or fake paired status is shown.

Authenticated HTTPS writes require same-origin/CSRF protection, current account
generation, registry revision and operation identity. Preferences replace the full
saved policy, preserving exclusions for missing devices and rows outside a cropped
preview. A lost reply does not automatically replay the operation. Uncertain saves
are described as uncertain configuration changes, not commands sent to Ring.

The owner projection remains within its 128 KiB budget. Both catalog and bridge
preview rows can be cropped with explicit flags; saved preference lists are never
silently cropped. Panic/emergency dispatch, bypass and all other non-camera
actuation remain blocked by the existing backend gates.

## Verification and remaining work

Maintained regression tests cover identity stability, immutable account ownership,
mapping qualification/recovery, capacity, unknown/missing devices, account changes,
CAS conflicts, cancellation, partial writes, corruption, missing enrollment state,
private path/lock safety, and camera-file preservation. HTTPS tests exercise real
authentication, CSRF and no-replay receipts. A large-inventory composed test retains
512 exclusion preferences while respecting the total owner-state byte budget.

The full server regression run passes **3,327 tests, with one skipped** because
this host has no IPv6 loopback address. Its 81 dependency deprecation warnings are
unchanged. The final focused run passes **108 tests**, including a last review
guard ensuring a future unsupported catalog classification cannot break camera
owner-state access. That guard was added while the full suite was running and is
covered by the final focused run. All 350 Python files pass Ruff checks/formatting.

The frontend passes 26 unit tests, type checking and formatting. After final UI
review corrections, its five affected tests and browser suite pass again. Chromium
checks cover 24 light/dark/mobile/desktop layouts, real Python registry projection,
account-switch replies, uncertainty/no-replay behavior, retained preferences,
capability exclusions and older-backend compatibility, with zero page errors.
Production assets and manifests are rebuilt and verified.

Qualification uses the pinned Node 26.12.1 runtime and previously extracted
GstRtspServer libraries. Release-path tests run with real host ownership metadata
outside the filesystem namespace sandbox; no security checks were weakened, no
system dependencies were installed and no live Ring account/device was used.

Evidence lives in
`/home/james/Documents/Codex/2026-10-25/he/work/halo-server-noncamera-wave2-2026-11-14/`.

Next is the separately supervised HAP bridge child, versioned IPC, read-only sensor
publication and bridge pairing/reset lifecycle. Then perform controlled installed
and physical sensor qualification alongside the seven standalone cameras. Alarm,
lighting, chime and other control families remain later work; subscription and
regional/account restrictions remain Ring-owned.
