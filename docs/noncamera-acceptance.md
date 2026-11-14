# Halo Bridge deployment and first-device acceptance

The non-camera implementation is a locally tested candidate. Physical Ring
compatibility, the owner's account permissions and Apple Home behavior must be
checked on the server before calling a family production-qualified.

## Before installation

Use the [managed release procedure](managed-release-activation.md). Verify the
server's current Python/runtime files against the candidate; an old ABI receipt
or successful desktop build is insufficient after a system update. Preserve the
current immutable release as the recovery choice. Activation preserves mutable
state; it never restores an older copy of credentials or pairing/preferences.

Record the seven camera identities, selected release, saved mode and service
health before the switch. Keep both camera and device pairing stores, registry,
publication policy, pending maintenance records, and Alarm/light/Chime policies.
Do not copy production secrets into a development fixture or reset pairings to
make an upgrade pass. The device child uses its own nested pairing directory and
does not need a new user service.

This candidate builds the currently pinned HAP runtime, `2.3.0-halo.1`. The last
recorded server build used `2.2.3-halo.2`; this is therefore more than a web-assets
update. Both installed adapter children and camera coexistence require validation.
The manifest's existing entry schema remains compatible; the new device entry is
covered by the full payload hash census and required by build preflight.

Registry files retain stable identities and selections. Older releases that do
not recognize new device mappings may refuse the new bridge registry. Code
rollback preserves those files; it does not promise that an old bridge reader can
publish every newer family. Restore compatible code rather than deleting identity
or pairing state. Cameras have separate stores.

## First server checks

1. Confirm daemon and web services are healthy and using the intended release.
   Run local diagnostics. A device pairing directory reported as
   `unverified/not-created` before first publication is expected; a recovery marker
   or unsafe path is not. Check the device adapter entry is present and nonempty.
2. Confirm all existing camera names and pairings survived. Check snapshots and
   one established live camera before enabling any new family. Record any change
   from the last accepted stream behavior separately from non-camera findings.
   Confirm Ring's push channel reaches connected and an ordinary camera motion
   event arrives. Healthy services and working snapshots alone do not prove push
   delivery; the deployment candidate includes push decoding/recovery repairs.
3. Enable device monitoring in Halo. Compare device and location counts to Ring;
   capture sanitized kind/capability structures for unsupported devices. Unknown
   fields must remain unknown, and an offline location must not block healthy ones.
4. Review Halo Bridge's import preview. Choose contact/motion sensors first, then
   explicitly connect the bridge and pair it once. Cameras stay standalone.
   Lighting groups require deliberate individual inclusion even with auto-import.
5. Open/close a contact and trigger one motion sensor. Compare Ring, Halo and Apple
   Home state/timing. Then check disconnect/reconnect, a daemon restart and the
   stability of accessory identities. Unavailable must not become closed/clear/off.

## Controls, one family at a time

Control permissions default off independently of discovery/import. Enabling a
permission must never operate a device.

- **Alarm:** inspect real mode, entry/exit phase, trouble and Ring authority. With
  the owner present, opt into normal controls and verify a deliberate mode change
  and its reported result. Check that open/faulted sensors cannot be bypassed.
  Panic, emergency dispatch and siren controls are excluded. Do not trigger an
  emergency to test a disabled button.
- **Individual lights:** verify actual state, a finite On request and Return to
  automatic. Motion/schedules may keep or turn the light on. Confirm uncertain
  outcomes are displayed and never automatically retried.
- **Lighting groups:** compare actual group membership and aggregate state. Check
  owner proof for every member, then opt in. A group report does not prove every
  member matches. Verify group versus individual/camera requests cannot overlap,
  that fresh readback restores state, and that a removed/changed group loses old
  command authority. Ring motion/schedules can override a group Off request.
- **Chimes:** inspect volume, snooze remaining and automatic nightlight only when
  reported. Test one setting and confirm independent readback. Sound tests need
  their own opt-in, confirmation and recent password check; after listening,
  acknowledge that the sound stopped. HTTP acceptance is not audible completion.
  No sound is sent by startup, monitoring, pairing or preference changes.

Never deliberately flood Ring to test throttling. The automated suite covers
rate-limit backoff, connection loss, stale responses, cancellation and no replay.
Observe natural reconnects and controlled local service restarts during the pilot.

## Qualification boundaries

Known sensor mappings, normal Alarm modes, exact physical Beams lights, REST
lighting groups and four legacy Chime kinds are implemented. Physical acceptance
must still name the tested models and account role. Newer Chimes, tone changes,
brightness, generic switches/outlets, locks, valves, thermostats and Intercom
actuation need additional model/command evidence. Unknown devices remain visible
without guessed HomeKit controls. Ring subscriptions and account permissions are
enforced by Ring; this work does not bypass them.

For each tested model, record source events, Halo/Apple results, restart identity,
outage recovery and any remaining limitation in the engineering notes. A passing
fixture, child handshake or HTTP response alone is not a physical acceptance.
