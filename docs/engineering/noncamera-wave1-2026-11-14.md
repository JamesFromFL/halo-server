# Non-camera devices: first implementation wave

Status: implemented locally, pending installed and physical qualification.
No new release has been deployed and no live device commands have been sent.

## Delivered scope

This wave implements the shared-auth provider/catalog foundation from slices 1–2
of the [bridge plan](noncamera-bridge-plan-2026-11-14.md), plus the read-only owner
interface needed to inspect and qualify it. Actual owner inventory capture remains
part of installed qualification. It does **not** publish a Halo Bridge to HomeKit
yet; durable bridge identity and HAP publication remain slices 3–4.

- Opt-in non-camera monitoring, disabled by default on existing installations.
  The owner can enable or disable it under **Devices → Other Ring devices**.
- Bounded Ring REST discovery using the existing authenticated Ring session.
  Alarm base stations and Beams bridges both seed per-location hub connections,
  including lighting-only locations. Camera inventory remains on its existing SDK
  path.
- One supervised CLAP connection per discovered location, separate from camera
  FCM notifications. Reconnection resynchronizes inventory; one unavailable
  location does not stop healthy locations.
- A generic, ecosystem-independent catalog with stable account/provider/location/
  asset/device/endpoint identifiers. Public device and location keys are opaque
  hashes. Names cannot change identity.
- Read-only observations for recognized hub schemas: contacts, motion, alarm
  panel/keypad status, battery/power/tamper/fault, glass break, tilt, flood/freeze,
  smoke/CO, temperature, retrofit zones, locks, switches, valves and related hub
  devices. REST-only Chime/intercom entries currently expose identity/connectivity
  summaries. An enumerated family is not a claim that every model is qualified.
- Unsupported devices remain visible as unsupported. No generic writable switch
  is synthesized for an unknown model.
- GTK-style web lists/detail pages, search and location/type filters. Other-device
  pages do not fetch camera pairing/control/recording RPCs or open camera media.
  They work even when camera inventory is unavailable.

## Safety and ownership

All non-camera actuation is denied in this wave. Panic, burglar/fire/medical
dispatch, bypass, alarm arming/disarming, sirens, locks and light controls have no
enabled command path. The UI contains no panic-enable button. A forged request or
an edited policy cannot grant command authority: unknown policy fields are rejected,
the provider rejects execution, HTTP routes are inventory-only, and the socket
sender allows only `DeviceInfoDocGetList` against ticket-admitted assets.

The monitoring off switch stops discovery and closes owned hub resources. It does
not change the physical Ring system. Future action permissions and per-device
exclusions belong to the registry/control waves and must remain independent from
this monitoring switch.

The daemon stores the preference in the private `devices/policy.json` under its
state directory (version 1, revision, enabled). Writes are atomic, revision-checked
and owner-only. First use does not create a store merely by viewing state. Web
changes require authentication, same-origin/CSRF checks, account scope, policy
revision and an operation ID. They use the existing configuration operation-journal
kind with a distinct target in the request digest, preserving the journal schema.

RingClient owns provider resources so account replacement can close them before
closing authentication or its event loop. Daemon callbacks additionally check
client, account, provider generation and an active admission token, including
empty inventory callbacks. Shared token rotation is serialized across SDK camera,
snapshot and bounded hub requests; token persistence remains owned by the session
manager.

No camera HAP package, pairing store, camera identity, media pipeline or recording
preference is migrated by this wave. In particular, do not accidentally change the
deployed camera HAP package while packaging a later pilot.

## State and bounds

Unknown does not become closed, dry, locked or disarmed. Current values, last-known
values, source health and receipt times remain separate. Unrelated patches do not
refresh another observation's age. Missing devices are retained as unavailable
after a complete inventory; incomplete discovery is not removal evidence.
If successful discovery confirms one source was removed while another source is
offline, explicit unavailable records prevent the removed devices appearing current
until aggregate discovery recovers.

Default REST discovery interval is 300 seconds. Hub inventory refresh is 60 seconds;
hub sources become stale after 120 seconds without valid complete inventory. The
daemon applies a 360-second final observation age limit. Disconnects make hub state
stale immediately. REST request success alone does not assert device connectivity.

Payloads are bounded to 1 MiB, 32 JSON nesting levels and 50,000 nodes. Discovery
admits at most 128 locations and 128 assets per ticket; retained device and hub-asset
counts are bounded. Over-limit or malformed data is rejected, not silently treated
as a complete empty inventory. The owner view shows at most 256 non-camera rows
within the existing 128 KiB total state budget; a cropped view is explicitly
incomplete/truncated.

## Qualification

Maintained tests cover model validation, full/partial reconciliation, unknown/stale
values, forged policies, command denial at multiple boundaries, token-refresh races,
per-location recovery, old-account callbacks, empty inventory, concurrent shutdown,
policy revisions, TLS/CSRF and operation replay protection. Composed daemon tests
verify opt-in defaults, provider cleanup, media-independent monitoring and the
bounded owner projection.

The final focused wave suite passes **249 tests**. This includes request cancellation
during a monitoring disable and an uncertain post-replace policy save: the owned
operation finishes cleanup, and later admission reloads disk state instead of
restarting discovery from the previous enabled value.

Frontend type checking and formatting pass; 21 unit tests pass. A Chromium fixture
passes 24 desktop/mobile/light/dark layout combinations, no-camera inventory,
generic-page camera-RPC isolation and one scoped monitoring-disable mutation, with
no page errors. Production assets and manifests are rebuilt.

The broad server regression results and environmental qualifications are recorded
below. The full run passes **3,240 tests, with one skipped** because the test host
has no IPv6 loopback address. The final 249-test
focused pass also covers the confirmed-removal correction added during review.
Evidence and the pre-wave source backup are in
`/home/james/Documents/Codex/2026-10-25/he/work/halo-server-noncamera-wave1-2026-11-14/`.

Qualification uses the project's pinned Node 26.12.1 and the previously extracted
GstRtspServer runtime. The desktop's default Node 26.13.1 is intentionally rejected
by the existing HAP adapter. Tests requiring root-owned release path validation run
outside the filesystem namespace sandbox, which maps those ancestors to `nobody`;
the release validation code was not weakened. No system dependencies were installed
or live server settings changed to run these tests.

## Remaining gates

1. Deploy a deliberately selected artifact and enable read-only monitoring for a
   bounded pilot. Capture redacted structural inventory and qualify the owner's
   actual devices, permissions, reconnects and resource use alongside all seven
   cameras. Fixture success does not prove Ring account or hardware compatibility.
2. Implement the private durable Halo Bridge registry, import/exclusion policy and
   stable child identities without changing camera stores.
3. Publish verified sensors through one separately paired Halo Bridge; keep cameras
   standalone. Qualify HomeKit stale-state behavior and restart/account transitions.
4. Add narrowly authorized, confirmed controls by device family. Panic/emergency
   dispatch and automatic bypass remain prohibited unless a separately reviewed,
   explicit policy and appropriate physical acceptance are developed.

Ring subscription and regional/account restrictions are unchanged. No subscription
bypass or independent local alarm backend is implemented.
