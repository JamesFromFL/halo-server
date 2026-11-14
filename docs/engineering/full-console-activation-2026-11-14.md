# Full console activation and pairing follow-up — 2026-11-14

## What the reported failure exposed

The running `52e72da…` release was a static UI repair on the older camera-only
backend. The completed non-camera waves and username login had been qualified
but not activated. The prior repair's public HTTPS checks established asset
identity and anonymous access rejection; they did not establish successful
pairing-code reveal against the real owner. Its real HTTPS regression used an
unpaired synthetic camera and therefore missed the production paired-camera
policy.

Fresh server checks found seven selected, published, already-paired cameras,
healthy daemon/web services and no active media. Forty bounded concurrent reads
through the installed `AdminClient`, web request pool and pairing-page logic
succeeded (maximum 0.371 seconds). A subsequent real pairing-reveal request
failed with `configuration_conflict`, without printing or retaining any secret.

The adapter deliberately denies setup-code reveal for an already-paired camera.
The UI nevertheless offered Reveal code, and the parent collapsed that policy
refusal into a configuration conflict. Correct the UI and preserve the actual
refusal category. Already-paired cameras do not need another setup code; moving
to another Home is an explicit remove/reset workflow, never an automatic repair.

An independent confirmed SSE leak retained subscriptions when header preparation
failed or was canceled. After eight such failures, further subscriptions were
rejected. Put the handshake under the subscription's cleanup block. This defect
could contribute to the interrupted-connection banner; its occurrence in the
reported browser session was not directly observed.

## Recording discoverability

The former “Keep recording source warm” switch controls Halo's HKSV support
advertisement as well as its pre-event source opt-in. Label it **Enable HomeKit
Secure Video support**, show actual eligibility and the reported source budget,
and distinguish it from Apple Home's recording/audio permission. Preserve the
existing default-off behavior and confirmation for continuous source use.

Current implementation accepts confirmed mains-only models and has a shared
budget of two sources. Battery-capable models remain ineligible even when wired
or solar-assisted. Passing fixture recording tests is not evidence of successful
clips in the owner's Apple Home timeline.

## Feature accounting

| Area | Current source | Deployment/acceptance requirement |
| --- | --- | --- |
| Standalone cameras, snapshots, live media | Implemented and previously deployed | Preserve all seven identities, pairings and existing media preferences. |
| HKSV capability/pre-event capture | Implemented, opt-in | Clear UI label; owner enables eligible cameras, then selects recording in Apple Home; real clip acceptance remains. |
| Device discovery/health | REST plus supervised Alarm/Beams location sockets; unsupported inventory remains visible | Deploy full backend and enable read-only monitoring; inspect real account results. |
| Single Halo Bridge | Stable import/exclusion identities and separate publication/pairing | Review import preview and pair once; cameras remain standalone. |
| Passive HomeKit devices | Contact/retrofit, motion, leak, smoke, CO and combined smoke/CO mappings | Qualify actual devices/events; unavailable never means clear/closed. |
| Alarm | State/trouble plus separately enabled normal Home/Away/Disarmed commands | Observe real authority; intentional owner-present control test. Panic/dispatch/bypass excluded. |
| Lighting | Qualified individual Beams light commands and explicitly included lighting groups | Controls remain opt-in; verify actual models and readback. |
| Legacy Chimes | Volume, snooze, reported automatic nightlight and separately permitted sound tests | Qualify supported models; enabling monitoring never makes a sound. |

Research parity is not completed feature parity. Brightness, generic outlets,
locks/valves/thermostats, Intercom actuation, additional sensor HomeKit mappings,
newer Chime models and tone settings, sensor chirps/keypad/base volume,
camera/location Modes, additional ecosystem outputs such as MQTT, and broader
shared-user command qualification remain follow-on work. Battery/tamper may be
visible in Halo observations without separate HomeKit services. No subscription
bypass was established.

## Verification and rollout

Use one complete managed package rather than another camera-only static backport.
Preserve enrolled authentication, TLS, camera pairing bytes, recording preferences
and existing device control opt-outs. The full package includes the already
qualified HAP 2.3.0-halo.1 update, so installed camera/device child startup and
retirement must be checked. Read-only monitoring can be enabled to fulfill the
owner's all-devices request; it must not enable commands, sirens, panic, sound
tests or recording. Device publication and physical acceptance are distinct.

Local diagnostic evidence: `work/halo-server-live-web-followup-2026-11-14/`.
HKSV UI evidence: `work/halo-hksv-ui-clarity-2026-11-14/`.
The final package/deployment receipt records exact release identifiers and checks.

The repair passed 162 affected Python tests, 58 frontend tests, Svelte and
format checks. Browser verification covers paired/unknown/unpaired camera and
bridge states, the real HTTPS session-rotation lifecycle, and 60 dashboard
layout combinations including the clarified recording controls. A new test
crosses real HTTPS, `AdminClient`, the Unix admin server and pairing
administration with a synthetic paired adapter. Rejected reveals preserve
pairing state; interrupted event handshakes release their subscriber capacity.
These checks do not substitute for the owner's final browser or device test.

## Completed activation and live observations

Activated the complete managed package from application commit
`7e8381bb094cd74ba56cbe3592c00c60ff1a1eae`, release
`f8818f8713d25eed3cc017e96ecc4990f5bce7f07d1b148291d22a4cd3ac1699`,
at selection revision 24. The prior release remains available for recovery.
The existing enrolled password and TLS identity were preserved; the owner
username is now `admin`. No credential reset was needed.

Fresh installed-package verification passed 892 Python tests, 323 adapter
tests and 58 frontend tests, plus 60 browser layout combinations and five
pairing scenarios. Installed camera/device children and daemon lifecycle checks
passed. The earlier 4,025-test full-source receipt is inherited evidence, not
a newly rerun full suite. Harness repairs and failed attempts remain in the
qualification evidence; no application bytes changed during qualification.

The first activation postcheck correctly rejected changed pairing-file bytes.
Investigation isolated seven changes to HAP's `lastFirmwareVersion` marker,
from `2.2.3-halo.2` to `2.3.0-halo.1`. Substituting that one field back in memory
reproduced every original complete-file SHA256. All keys, setup codes,
identities and paired-client content were therefore preserved. A separately
reviewed read-only postcheck accepted only this exact marker migration, with
unchanged inventory and hash checks; it passed. The failed strict postcheck was
retained rather than overwritten. No pairing file was restored or edited.

Live HTTPS certificate/index/assets match the package, and anonymous pairing
access remains rejected. The real paired-camera reveal now returns
`pairing_not_available` promptly, without exposing a setup code. This refusal
comes from Halo's HomeKit pairing policy, not Ring. The real server projection
passes the current frontend validator before and after device discovery. These
checks do not establish the owner's authenticated browser experience; a reload
and owner acceptance test are still required.

Read-only monitoring was enabled with the current account scope and policy
revision. The account returned 28 noncamera records: 14 contact sensors, two
Chimes, one motion sensor, one smoke/CO listener, one security panel, one keypad,
one range extender, two base-station records, three hub records, and two unknown
records. These are provider inventory records, not a claim of 28 distinct
physical products. The two unknown types are `access-code` and
`access-code.vault`; neither has a supported HomeKit mapping. Their contents
were not inspected. Seventeen records have eligible bridge mappings; the
combined smoke/CO listener supplies two services.

Twenty-seven records report online. A REST `base_station_v1` record has no
connection observation and correctly remains unknown. One online panel keeps
`alarm_state` and `alarm_signal` stale because Ring has not supplied the field
receipts required by Halo. Its mode/connection/siren/tamper observations have
current receipts. This makes the aggregate inventory partial, even though the
28 records are displayed. Unknown status must not be converted into an assumed
safe alarm state. Qualify the missing-field semantics before enabling physical
alarm control; do not solve the partial flag by inventing timestamps.

All seven cameras remain selected, paired and published. Camera recording
preferences stayed unchanged and no recording source was enabled. Halo Bridge
publication and all noncamera control grants remain off. Monitoring did not
arm/disarm, play Chimes, operate lights or enable panic/dispatch. Bridge import,
publication/pairing and physical acceptance remain separate owner-visible steps.

Local package/deployment evidence is under
`work/halo-server-full-console-2026-11-14/`, including qualification,
the retained strict and reviewed migration postchecks, public HTTPS checks,
read-only monitoring results and sanitized discovery health. Application and
documentation commits are local only; no push was performed.
