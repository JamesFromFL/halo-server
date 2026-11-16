# Ring and HomeKit camera privacy

## Decision and evidence

Honor effective Ring camera permissions by default. Add an explicit per-profile,
per-camera `homekit` preference for owners who want Apple Home to manage their
viewing and recording permissions. Do not infer consent from subscription status,
model, power source, the alarm's Disarmed/Home/Away state, or a capability flag.

Ring documents separate camera Recording and Live View actions for each mode,
and qualifies Modes availability by subscription:
[Ring Modes](https://ring.com/support/articles/p0klz/Controlling-Your-Ring-Devices-with-Modes).
Its privacy documentation says the audio privacy switch affects both streaming
and recording:
[Ring privacy](https://ring.com/gb/en/support/articles/g4e2w/Creating-and-Deleting-Privacy-Zones?hasLangChanged=true).
The upstream Ring client also checks `settings.live_view_disabled` before a live
call and `settings.motion_detection_enabled` before snapshots:
[ring-camera.ts](https://github.com/dgreif/ring/blob/master/packages/ring-client-api/ring-camera.ts).

These are permission signals, not proof of the physical camera's LED cause or a
complete audit of all other Ring consumers. Earlier local status observations
found no active Halo media session during the reported indoor-camera blue LED.
This implementation closes an identified privacy gap; it does not establish that
Halo caused that incident.

## Implemented boundaries

- The Ring loop copies a minimal immutable receipt after a successful inventory
  GET. Mutable SDK dictionaries, inventory cache hits and receipt timestamps from
  a different wrapper cannot manufacture fresh permission evidence.
- In the default `ring` mode, live/talkback require explicit Live View permission;
  background recording and snapshots require both Live View and motion permission.
  Microphone audio requires explicit audio permission in addition to the relevant
  video/live or recording permission. Unknown fields deny the affected path.
- Successful inventory receipts expire after 120 seconds. Ordinary inventory
  refresh currently runs every 60 seconds. Therefore Ring-side changes are
  eventually observed, not instant push-based privacy transitions. The expiry
  sweep and per-operation admission checks prevent stale permission from lasting
  indefinitely. No extra per-viewer Ring polling is introduced.
- `homekit` overrides Halo's local Ring-permission admission only for native
  HomeKit demand. It never changes Ring settings, starts media just by being
  saved, enables HKSV, or grants access to compatibility consumers. Ring itself
  may still refuse or suppress the requested media.
- HAP camera activity, stream activity, microphone, recording-audio, event
  snapshot and periodic snapshot permissions continue to apply. Event/periodic
  changes also retire an in-flight image and clear parent snapshot authority.
- Media admission and renewal carry a material privacy epoch. Revocation fences
  existing source authority and clears buffered/history state before asynchronous
  cleanup. A permission change back to allowed cannot resurrect an old request.
- Source audio admission runs before original Opus buffering, legacy decoding,
  forwarding and diagnostic trace handoff. A blocked microphone is not merely
  muted at the final HomeKit output while its samples remain in Halo history.
- Compatibility RTSP lacks a separate camera-audio export filter, so Ring audio
  denial conservatively denies that combined source instead of leaking audio.
- Explicit diagnostic media capture requires current Ring Live View and audio
  permission, even for a camera using the HomeKit-only override. A privacy epoch
  change permanently detaches that capture. Its nonblocking checks skip a sample
  under lock contention without delaying normal media handling.

If Apple requests recorded audio while Ring forbids the microphone, the combined
HKSV request remains blocked. For video-only recording, turn off **Record Audio**
in Apple Home. Halo does not silently rewrite Apple's recording preferences.

## Administration and storage

`camera-privacy.json` is private, bounded, revision checked and atomically written.
Preferences belong to a Ring profile and camera. Missing preferences mean `ring`;
corrupt or uncertain private state cannot silently retain an override. No auth
tokens, camera images or Ring settings payloads are persisted in this file.

The private owner methods are `camera_privacy_list` and `camera_privacy_set`.
The browser exposes authenticated `GET /api/web/v1/privacy` and recent-password,
CSRF-protected `POST /api/web/v1/privacy`. Writes include profile, account
generation, camera ID, per-camera expected revision, selected mode and a web
operation UUID. The web operation journal records outcomes without replaying an
uncertain action. The UI keeps saved mode, current Ring source permission and
effective local source admission separate; none claims actual Apple recording.

The setting is in **Devices → camera → Privacy**. Enabling HomeKit-managed privacy
requires an explanation and confirmation. Owners should allow Live View and motion
detection/recording in Ring for the modes they intend to use, allow audio if desired,
then configure Apple Home. Halo does not alter Ring cloud recording behavior or
bypass any subscription or server restriction. Existing HAP pairing identities,
recording opt-ins and non-camera device grants are preserved.

## Managed release compatibility

Reader 16 is required for this privacy behavior. The managed activation barrier
raises the reader floor before the new release runs, so a reader-15 rollback
cannot silently bypass saved privacy restrictions. Before a production deployment,
qualify a reader-16 recovery release as well as the candidate. Do not relabel an old
binary as reader 16; it must actually enforce these privacy rules. No production
release or mutable server setting was changed during this implementation pass.

## Verification boundaries

All implementation tests use synthetic inventories, media buffers, IPC, local TLS
and isolated browser fixtures. They do not open Ring streams or send alarm,
light, camera or recording state changes. Live propagation, physical LEDs and
Apple Home acceptance still require a coordinated owner test after deployment.

## Local qualification — 2026-11-15

- Final broad Python run: 4,384 passed, one skipped, one deselected, and one
  existing fixture timeout. The failing device-bridge test had applied its 100 ms
  request deadline to child startup too. Moving that test-only deadline after
  startup preserves the request-expiry assertion; all 64 tests in its module
  then passed. No production timeout was changed. The full suite was not repeated
  after this test-only correction.
- The optional `GstRtspServer` GI namespace is absent from this desktop. The RTSP
  module could not be collected, and the daemon smoke test that requires that
  namespace was deselected. Those checks remain a package qualification item.
- HomeKit adapter: 342 tests passed. Web UI: 75 unit tests, Svelte checks and
  production build passed. Privacy browser checks passed in Firefox and Chromium
  (seven each), with desktop/mobile and light/dark screenshots reviewed.
- Existing page regression checks passed in both engines. The real local HTTPS,
  authentication, SSE and synthetic pairing regression passed in Firefox, with
  zero media sessions and zero HAP listeners.
- All changed Python files passed Ruff and formatting checks; `git diff --check`
  was clean. Independent integration review found no remaining blocker.

Evidence is retained under
`work/halo-camera-privacy-2026-11-15/` in the development workspace, including
`qualification/python-final.log`, `qualification/device-runtime-final.log`,
`qualification/admin-web-release-final.log`, `qualification/pairing-firefox/`,
`source-privacy-tests.log` and `frontend/frontend-verification.json`.

This is local implementation qualification, not a deployment or a live-device
acceptance claim. No production Ring, alarm, lighting, recording, pairing or
release settings were changed during this pass.
