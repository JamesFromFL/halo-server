# Camera lighting: manual switch and native motion

## Intended behavior

The owner clarified the target on November 15: a physical Kasa switch, connected
through Apple Home automation, should turn camera floodlights On for manual use
and turn them Off on the next switch action. Ring should continue to handle
ordinary motion lighting. Extra configurable manual timers are deferred.

The floodlights must remain powered; the switch is an automation input rather
than a power disconnect for the cameras. Exact Kasa/HomeKit compatibility and the
automation configuration depend on the chosen switch and are outside this change.

The requested manual hold is **not yet physically qualified**. Halo already
publishes a capability-gated HomeKit Lightbulb service for each supported camera.
This change repairs its command confirmation and adds truthful lighting
observations. It adds no recurring On commands, timer, automatic compensating Off,
or inferred manual override. Ring-app Off must not be undone by a Halo keepalive.

## Native evidence and limits

The owner's Floodlight Pro screenshots show motion-light shutoff presets of
30 seconds, 1 minute, 3 minutes, 5 minutes, and 15 minutes. The selected one-minute
setting matches a passive inventory observation of
`settings.floodlight_settings.duration=60`. The screenshots also show enabled
motion lighting, three enabled light-motion zones, sensitivity and brightness
sliders, and an empty light-schedule list. No manual timer appears on those pages.

`always_on=false` in that inventory while motion lighting is enabled establishes
that this field must not be labeled the motion-enable switch. Zero-valued manual
duration fields do not establish an indefinite saved setting. Light On also does
not identify whether motion, a schedule, a linked device, or a person caused it.

The [Ring light-settings documentation](https://ring.com/support/articles/b7aj8/using-light-settings-for-ring-security-cameras)
describes native motion settings. The
[pinned ring-client-api implementation](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/ring-client-api/ring-camera.ts#L325-L391)
establishes plain camera On/Off commands and generic settings transports. It does
not establish the field-specific timing mutation contract or an indefinite value.

Correction to earlier scratch research: the legacy settings writer PUTs to
`/clients_api/doorbots/{id}/` with a `doorbot.settings` envelope, not to a
`/doorbots/{id}/settings` path. The separate modern device-settings writer PATCHes
`/devices/v1/devices/{id}/settings`. Neither generic writer is used by this change.
Smart Lighting/Beams duration commands are a separate protocol.

## Implemented locally

- A light command is submitted once through the existing account/device guards
  and shared lighting reservation. A bounded inventory GET after acknowledgement
  supplies observed state. An unavailable or disagreeing read does not retry the
  setter or turn the requested state into an observed fact.
- Multiple camera commands from a HomeKit scene may wait briefly before sending;
  they share one deadline with submission and confirmation. Waiting never retries
  a setter. Canceled, expired, replaced-account, or replaced-device requests must
  not be admitted later. The shared reservation still excludes overlapping hub
  and group writes.
- HomeKit light claims have separate bounded capacity from sirens, whose existing
  two-command limit is unchanged. Admission checks alone do not turn proven
  pre-send light failures into uncertain outcomes. Other errors retain conservative
  uncertainty. The service admits up to 64 pending light requests; existing IPC
  limits still apply, so this is not a guarantee of 64 concurrent HomeKit actions.
- The confirmation read shares the existing seven-second control budget and uses
  at most three seconds. Account, client, device, admission, and lighting revisions
  fence publication. A newer observation wins over an older confirmation.
- A successful setter advances a separate acknowledgement revision. An inventory
  GET started while a command was in flight cannot confirm it, even if that read
  is published afterward. Camera-specific revisions prevent another camera's
  write from invalidating a valid observation; unscoped hub/group writes remain
  conservative invalidations of all camera light observations.
- A fresh privacy revocation discovered by that GET uses the existing media fence.
  Confirmation does not acquire a stream or snapshot.
- Successful inventory GETs copy immutable, minimal camera lighting observations.
  Cache hits never renew freshness. Duplicate IDs, replaced devices, stale
  observations, and intervening light commands cannot supply current state.
- The owner interface displays observed On/Off and the native motion shutoff
  interval. Missing or conflicting fields remain unknown. Manual timing and the
  activation source remain unverified. Reading this section does not contact Ring
  or write settings. Existing capability-gated manual On/Off remains available.

No pairing, alarm state, camera mode, motion setting, light setting, recording
policy, or server deployment was changed during development. Tests use synthetic
devices and transports. The prior privacy work is committed as `0a04e7d`.

## Owner-operated acceptance still required

After an authorized deployment, the owner can turn a light On through Apple Home
and leave it on beyond the native motion interval, then turn it Off. Observe
whether the lamp remains steadily on, whether motion during the hold changes the
result, and whether ordinary motion lighting resumes after Off. Also issue manual
On while motion has already lit the lamp: the automation must still deliver the
manual request rather than infer that no action is needed from an already-On
observation. The Kasa switch state represents user intent, not the lamp's motion
state; avoid an automation that copies motion-driven lamp changes back to it. A separate
Ring-app Off should remain effective. Repeat for each installed camera model;
passing for one model does not establish another model's firmware behavior.

The assistant must not perform those physical actions under the standing
no-state-change instruction. If native manual On expires, investigate the exact
native command contract before claiming an indefinite override or introducing a
different control strategy.

## Local verification

Final combined Python suite: **528 passed**. This includes complete two- and
seven-camera broker requests with independent post-command state checks, shared
reservation exclusion, same-camera FIFO, canceled/expired waiters, delayed SDK
submission, account/device replacement, privacy fencing, bounded claim capacity,
and compatibility with individual/group lighting. No synthetic setter is retried.

HomeKit adapter control/session checks: **28 passed**. Web verification: **80 unit
tests**, Svelte zero errors/warnings, successful build, and seven camera-light
browser checks each in Chromium and Firefox. Desktop/mobile light/dark screenshots
were inspected. Ruff, formatting, and `git diff --check` passed.

These checks establish local control-flow and interface behavior. They do not
establish a physical indefinite manual hold, Kasa interoperability, or deployed
server behavior. The lighting changes remain local and undeployed.
