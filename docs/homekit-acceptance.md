# Direct HomeKit hardware acceptance

Status: **pending real-device execution**. This is the S15 runbook for the owner and an
engineer at the actual home. Writing this document does not start Halo, log into Ring,
publish an accessory, operate a camera, or pair with Apple Home.

Synthetic qualification covers native media encoding/decoding, private IPC, public HAP
delegates, encrypted HDS framing, bounded transport and cancellation, privacy races,
stable identity, targeted reset, and storage fault injection. Those results do not prove
Ring model behavior, Apple pairing, Home hub reception, notifications, remote access or
HKSV timeline playback. Record the exact release's test receipt separately from this
hardware record.

## Record the test environment

Keep the completed record private. Do not include setup PINs/URIs, credentials, raw state
files, signed URLs, or unrelated household footage in an issue or support attachment.

| Field | Actual value |
| --- | --- |
| Date, tester and local time zone | Pending |
| Release ID and manifest SHA-256 from installed status | Pending |
| Host OS/kernel, CPU and wired/Wi-Fi interface | Pending |
| Selected Ring model, firmware and camera ID | Pending |
| Mains-only evidence or battery-capable classification | Pending |
| iPhone/iPad model and OS version | Pending |
| Active Home hub model, software and connection | Pending |
| Home/iCloud recording eligibility checked | Pending |
| LAN topology, guest/VLAN isolation and IPv4/IPv6 availability | Pending |
| Remote test connection: cellular or another external network | Pending |
| Existing selected cameras and identity fingerprints before changes | Pending |

Check Apple's current [camera setup and recording requirements](https://support.apple.com/guide/iphone/iph7bc5df9d9/ios)
for the installed Apple versions. HKSV recording needs an eligible iCloud+ plan and a
working compatible home hub. Configure Home/Away recording and notification choices
explicitly; those choices affect whether a clip or alert should exist.

## Establish one camera first

Use the verified installed launcher as `halo-server` below, from the daemon owner's
account. Follow [managed release activation](managed-release-activation.md) for installing
or switching releases. Fresh managed state needs the explicit
`activate --initial-mode homekit` option described there. A managed bundle supports
`homekit` and `ring-only`; migrate saved compatibility/dual settings deliberately
through the existing source service first. Use one process manager; a foreground daemon and the user service
must not compete for the same state.

1. Read `halo-server status`, `halo-server config get` and `halo-server doctor`. Save the
   release identity and any reason codes. If local codec qualification is needed, run
   `halo-server doctor --probe`; this performs bounded local probes, not camera tests.
2. Resolve pending reset, unsafe pairing paths, failed cleanup or
   `storage_recovery_required` before pairing. Preserve a retained storage intent and the
   affected files; do not delete the marker or restore an older pairing snapshot to
   force startup. Storage recovery requires an explicit engineering decision.
3. Confirm saved and applied mode are `homekit`. For a source installation, if
   necessary save `halo-server config set --mode homekit` and restart using the
   existing process manager before selecting cameras. `dual` is a separate source
   compatibility test; Scrypted is not required for the native path.
4. Sign into Ring through `halo-server tui`. Inspect `halo-server homekit list`; select
   the pilot camera with `halo-server homekit select CAMERA_ID`. Selection replaces
   the entire set, so preserve any previously selected IDs when adding a pilot to an
   existing installation. Never select a whole discovered account unintentionally.
5. Read `halo-server homekit pairing list`. Confirm the selected stable identity,
   availability and publication. Software readiness is distinct from a successful
   Apple pairing or a working Ring source. Wait for current publication evidence;
   selection or an accepted configuration page alone is insufficient.
6. Use **Reveal setup** in the TUI, or explicitly run
   `halo-server homekit pairing reveal CAMERA_ID`. Add the camera in the Home app using
   that camera's code. Do not capture the reveal in logs or screenshots. The TUI hides
   it after 60 seconds; hiding a code does not rotate or revoke it.
7. Assign a recognizable room/name. Confirm that exactly one intended accessory
   appears. Refresh ordinary pairing metadata and verify it reports paired. Leave the
   camera's previously chosen privacy state intact until the next explicit test.

Pass: pairing completes once, the selected camera can be identified unambiguously, and
ordinary status/support output contains no setup secrets. If it fails, record the Home
error and Halo reason codes before considering reset.

## Live video, snapshots and simultaneous viewers

Perform at least ten local opens, including the first cold open and later warm opens.
Observe a clock or moving object in view. Record tap-to-first-moving-frame time, visible
delay, freezes, reconnects, image detail and orientation. Keep cold and warm results
separate. Leave a stream open for ten minutes, then close it and verify live owners and
workers retire; an explicitly opted-in warm recording owner may remain.

Open and refresh Home camera tiles. A cached periodic image can legitimately be older
than a new event image; a stale tile must not be reported as fresh capture. Check a
notification image as well as the tile. Record missing-image errors without allowing an
image request to delay the underlying motion/doorbell event.

Repeat five opens over an external network with the phone's home Wi-Fi disabled. Confirm
the phone is actually remote and the home hub is connected. Record remote first-frame
times separately. Record whether local and remote trials actually exercised IPv4 or IPv6. Mark an
unobserved family unqualified; a passing IPv4 trial does not qualify IPv6.
Repeat a live open on a second authorized Apple controller and, where
available, two selected cameras. Record the negotiated limits and any honest busy
response; do not assume unlimited viewers.

Halo's implemented catalogue is classic H.264 with a maximum advertised live target of
1920×1080. Apple chooses a negotiated target and source compatibility determines whether
Halo can copy compressed video or must adapt it. Do not infer full Ring source resolution,
codec-copy behavior or delivered frame rate from a sharp-looking Home preview. Record
negotiation diagnostics if available; otherwise mark those measurements unobserved.

Where practical, repeat a live view and warm collector across a real day/night IR
transition and a source reconnect. Record changed source format, buffering, stalls
and recovery; do not infer actual Ring epoch behavior from synthetic media tests.

## Microphone and talkback

For a camera with the corresponding saved graph/capability, test camera-to-Home sound
and Home-to-camera speech separately. Two people in different rooms reduce acoustic
feedback and make the direction clear. Speak a short numbered phrase immediately after
opening talk, pause, then speak another phrase immediately before releasing it. Repeat
ten times locally and five times remotely. Record missing first/last syllables, clipping,
delay, echo and whether the camera speaker stops promptly when talk is released.

Exercise live microphone mute, speaker mute and volume zero where exposed by the
controller. Change each while active, then open a new stream. Muted microphone audio
must not resume through a new view; muted/zero-volume speaker must not play queued or
new talkback. Restore only the intended value explicitly. Restart Halo while muted and
confirm saved mute/volume survives. Unsupported controls are N/A with evidence, not pass.

The current HomeKit audio path supports mono Opus negotiation at 8/16/24 kHz and
20/30/40/60 ms packet times using the classic HAP clock convention. Apple may select only
some combinations. Report the actual selected combination when observable; synthetic
coverage of the others does not establish that this Home controller exercised them.

## Motion and doorbell delivery

First choose Home notification settings that allow the test alert; record Focus,
Home/Away, activity-zone and specific-motion filters. Trigger at least ten separated
motion events and, for a doorbell, ten real button presses with no one relying on the
doorbell during the test. Record one press per trial, missed alerts, duplicate alerts
and the motion state returning to idle.

Use a common clock or one continuous external recording to compare physical trigger,
Ring's observed event arrival, Halo's event arrival and the Home notification. Measure
only timestamps that are actually observable. Ring-app notification time is a useful
comparison but is not Ring's server receipt time. Report end-to-end delay separately
from any observable Halo-to-Home delay; do not claim instant delivery from software tests.

While a live view or snapshot is slow, repeat a button press. Then briefly disconnect
and restore Halo's network using an owner-approved method. Confirm current state
resynchronizes and old queued presses are not replayed as new doorbell rings. A cloud
push outage must not silently become a verified no-motion fact.

## HKSV recording and retained history

1. Use a selected camera with video capability. Include battery-capable and unrecognized
   models in the acceptance matrix; power classification must not block owner opt-in.
   Record the power source and observed battery impact of continuous pre-event capture.
2. Explicitly opt in with `halo-server homekit warm CAMERA_ID on`. Choose the desired
   Home recording and recording-audio settings separately. Owner warm opt-in does not
   grant either recording permission.
3. Wait until actual retained history is ready, including at least four seconds of
   usable pre-event media. Record warm/buffering status and any reduced-history reason.
   A cold or rebuffering source cannot supply history it never captured.
4. Put a visible clock in the scene and trigger five separated events with speech or a
   clap at the beginning and end. In Apple's Home timeline, wait for each clip and
   replay it locally and remotely. Record trigger-to-clip-availability time, retained
   lead-in, end of event, duration, picture continuity, A/V alignment and tail audio.
5. Repeat with recording audio disabled. Confirm the new clip is silent; if a clip is
   intentionally exported through Apple Home for private analysis, independently check
   the audio-track presence and timing. Do not export household media automatically.
6. If the public Apple controller exposes recording quality, change it during a
   clip. Confirm the active clip finishes using its frozen configuration and the
   next clip uses the newly accepted settings. Otherwise record that check as N/A;
   do not use hidden HAP fields to simulate controller acceptance. Disable recording
   during another clip: new delivery must stop and cannot resume from an old buffered
   job after re-enabling. Separately turn recording audio off during an active clip;
   its old audio delivery must be fenced. New clips remain silent after restart and
   cannot revive old buffered audio after permissions change.
7. Turn warm off with `halo-server homekit warm CAMERA_ID off`; verify the warm capture
   owner retires without interrupting other cameras. Repeat with all selected cameras,
   including overlapping events on at least seven cameras when available. Check that
   transport contention waits without lost init/fragments and retained cleanup prevents
   a duplicate source or job for the same camera.

Pass requires clips that are actually playable in Apple's timeline with the expected
privacy and continuity. Receiving fragmented MP4 over synthetic HDS is not this pass.
Home hub analysis, cloud availability and Home notification filters can affect whether
Apple retains a clip; preserve those facts when diagnosing a missing clip.

## Optional light and siren controls

For a positively supported camera, use `halo-server camera-control CAMERA_ID light on` / `off` and inspect physical state plus later observed readback. A successful
command receipt alone is not confirmation. Test siren only with explicit owner
approval and nearby people informed: use the documented siren action, verify the
30-second on limit and explicit off, then observed state. Unsupported or unsafe
controls are N/A. Construction, restoration and ordinary reads must not actuate.

## Privacy, identity and recovery

Observe a visible clock and newly triggered events when checking privacy. Home
may retain a tile or recording it already received; that alone is not new Halo
delivery. Record where evidence stops rather than declaring an old cached image a
new privacy leak.

Test the controls actually exposed by the installed Home version. Do not use private
HAP fields to force a hidden control and then count it as a user-interface test.

| Action during active media | Required observation |
| --- | --- |
| Home camera off / `HomeKitCameraActive=false` | Live view, snapshots, talkback and recording are denied; motion does not reappear from a later source event. |
| Live stream `Active=false`, if independently exposed | Existing live sessions stop; recording permission remains governed separately. |
| Recording off | Recording delivery stops; live access follows its own permissions. |
| Recording audio off | New recording audio is denied independently of live microphone mute. |
| Microphone mute or volume zero | Camera-to-Home live audio stops; saved state survives restart. Current privacy fencing may also retire a recording. |
| Speaker mute or volume zero | Talkback delivery and pending playback stop; saved state survives restart. |
| Deselect pilot camera | Publication/media retire without deleting pairing files or changing other cameras. Reselecting retains its stable identity. |

Use the existing process manager for a clean stop/start, then verify pairing, privacy
preferences and stable identity survive without a duplicate accessory or a new setup
request. Test a brief network outage and subsequent recovery. On an explicitly approved
disposable test installation, test adapter failure and parent failure separately; verify
children are reaped or retain their ownership locks, so a second publisher cannot take
over while old owners are still alive. Never perform crash/power-loss injection on the
owner's only pairing store as an acceptance shortcut.

For unattended operation, verify the rendered user unit and selected release identity,
then observe at least 24 hours with periodic local/remote opens and natural events.
Record CPU/RSS, owner/worker counts, restart count, event gaps and clip failures at the
start, after activity and after idle. A stable bound/plateau matters more than one RSS
sample. An owner-approved real reboot and login/logout-policy check remains a separate
step; synthetic process tests do not qualify service startup after reboot.

If recovery requires pairing reset, first read current pairing status and obtain the
owner's decision for that camera. Run `halo-server homekit pairing reset CAMERA_ID --yes`
only for the chosen identity. This restarts the adapter and temporarily interrupts other
native views. Verify other camera namespaces and pairings remain intact. For a pending
operation, inspect its status and resume its exact recorded operation UUID with
`--operation-id UUID`; a conflict requires fresh inspection. Remove/re-add the affected
accessory in Apple Home when needed. Do not delete all pairing storage.

Code rollback must pass the installed reader floor and preserve current credentials,
privacy settings, reset deny journals and pairing state. Follow the managed operator;
do not replace canonical state with an older backup to make older code start.

## Acceptance record and evidence

Set the owner's tolerances before executing the measurements. The figures below are
blank deliberately; there is no hardware performance result yet. Record N/A with its
reason when the required device or public control is unavailable.

| Check | Target chosen before test | Actual result / evidence | State |
| --- | --- | --- | --- |
| Pilot pairing and stable identity after restart | No duplicate or re-pair | — | Pending |
| Local video cold/warm opens, 10 trials | First-frame median/p95: ___ / ___ s | — | Pending |
| Remote video, 5 trials | First-frame median/max: ___ / ___ s | — | Pending |
| Continuous live view, 10 min | Freeze/reconnect tolerance: ___ | — | Pending |
| Snapshot tile and event image | Freshness/error behavior recorded | — | Pending |
| Local/remote audio and talkback | Missing syllables/echo/delay tolerance: ___ | — | Pending |
| Motion and doorbell, 10 each | Missed/duplicate tolerance and latency: ___ | — | Pending |
| Apple timeline clips, 5 events | Lead-in/tail/A-V/availability tolerance: ___ | — | Pending |
| Privacy controls and saved mute | No unauthorized new delivery or revival | — | Pending |
| Cleanup, outage and restart | Recovery deadline: ___ s; no duplicate owner | — | Pending |
| Targeted reset, if authorized | Other pairings intact; exact operation completes | — | Pending |
| 24-hour unattended run | Resource/restart/failure tolerance: ___ | — | Pending |
| Real reboot/startup, if authorized | Selected release and pairings intact | — | Pending |

On failure, stop the failing trial, note the time and fixed reason code, and export a new
private sanitized report with `halo-server support --output ./halo-support-UNIQUE.json`.
The destination must not already exist. Keep raw screen/media evidence local and review
it before sharing. Record which layer failed: source/cloud, local software publication,
negotiation/media transport, Apple controller/hub, or cloud timeline. Do not replace an
unknown result with a pass based on a different layer's health flag.

Acceptance is complete only when the owner and engineer date the relevant rows and
record any model-specific limitations. Until then, real Apple/Ring qualification remains
pending even when all local software tests pass.
