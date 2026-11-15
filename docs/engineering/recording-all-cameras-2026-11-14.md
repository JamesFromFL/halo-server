# Recording support for every selected camera

The owner requested removal of Halo's mains-only model allowlist and fixed two-camera
recording limit. Those were application policy choices, not evidence that the account's
other Ring cameras could not record. All seven currently selected cameras provide video.

## Behavior

- Any discovered, selected video-capable camera can opt into HKSV, including battery-capable
  cameras and unknown model/power classifications. Power evidence remains informational.
- Each camera retains its independent durable owner on/off switch. Apple Home recording,
  recording audio and negotiated codec settings remain separate permissions.
- No update enables recording automatically. Pre-event capture can keep a Ring stream open
  and consume battery power; the existing web confirmation explains this behavior.
- The two-camera gates were removed from warm policy, capture ownership, controller
  admission, broker admission, recording manager, native workers and adapter sessions.
- One warm source and one recording job per camera remain enforced. Cleanup must complete
  before that camera can acquire a replacement. Finite media buffers, protocol identity
  bounds, per-process constraints, request deadlines and privacy revocation remain intact.
- The shared bulk transport still admits four simultaneous transfers. Recording packets
  wait up to five seconds for a slot, rechecking authority and cancellation, instead of
  immediately discarding a clip under contention. A failed or expired transfer still
  retires its exact job and resources.
- The web interface and TUI use actual video capability for eligibility. The interface
  shows reserved sources without a configured camera cap and distinguishes warming,
  ready and pending cleanup from Apple's saved recording preference.

## Verification and acceptance boundary

Synthetic regressions cover seven owner opt-ins and accessory configurations, including
battery and unknown models; seven concurrent warm and active capture owners; independent
controller, broker, muxer and adapter sessions; shared transfer contention without lost
initialization packets; single-camera revocation; and retained cleanup/replay protection.
Seven actual native recording workers process local synthetic media and retire independently.
Browser checks use disposable HTTPS owner state; they never connect to Ring.

Live acceptance remains separate: the owner must choose recording in Halo and Apple Home,
then verify playable event clips, retained pre-event history and audio in Apple's timeline.
Admission tests do not establish sustained seven-camera encoding performance, Ring session
availability, home-hub/cloud acceptance or battery life. No alarm or physical device state
change is part of this work.

Deployment must preserve all camera and bridge pairing bytes, device grants (including
the protected keypad light), authentication, TLS and recording preferences. With all owner
recording switches still off, this release does not change the HomeKit accessory graphs.
The prior complete release remains available for recovery. Exact verification and deployment
receipts are retained in `work/halo-recording-all-cameras-2026-11-14/` in the task workspace.
