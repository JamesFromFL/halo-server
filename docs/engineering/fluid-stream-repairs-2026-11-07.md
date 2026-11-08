# Fluid HomeKit quality changes and receive continuity

Implementation follows [post-capture research](postcapture-research-2026-11-07.md).
The owner confirmed VPN use on the viewing phone. It may affect Apple's selection,
but causation is unproved. Halo honors the selected resolution, FPS and codec
budget without assuming local/direct playback or overriding Apple's choices.

## Slice 1: video reconfiguration — implemented and locally qualified

Active adapted video retains its Ring decoder while a bounded worker command
prepares and installs a new output encoder. The command has strict finite fields,
an 8192-byte ceiling, an exact revision acknowledgement, and existing process
deadlines/retirement. A fresh output IDR carries the new parameters. Original PTS,
receipts, decoder-held frames, force requests, RTP session and audio are preserved.
FPS changes reinitialize output cadence at the new ceiling; unchanged-FPS changes
retain the learned cadence. Source discontinuities still use full recovery.

RTCP and MTU changes preserve codec state. MTU changes fence the existing send lock
before clearing cached repairs, so already admitted sends finish under the old
selection. Payload-type changes remain rejected. Copy-to-adapt still needs a
source IDR because a copied stream has no warmed decoder.

The recorder now distinguishes request, applied transition, first sent IDR and
superseded request. Rapid changes before the next picture legitimately produce
only the final revision's first IDR. Numeric changed-field bits follow width,
height, FPS, bitrate, profile, level, payload type, MTU, RTCP interval, in that
order. Encoded-frame events retain configuration/decoder generations, selected
bitrate and payload size; complete metadata can measure codec windows across
configuration boundaries instead of hiding them behind generation resets. Source
read events include existing NAL-type metadata for full-session keyframe analysis.

Validation completed locally:

- 145 worker/native/cadence/codec tests passed, including 54 new protocol and native
  transition tests. Commands cover cancellation, stale/bad acknowledgements, hung
  child termination, B-frame origins and queued force requests.
- 141 broader video/live tests passed; one IPv6 loopback case skipped because the
  test host lacks the address. This overlaps the native suite and is not additive.
- 94 diagnostics, lifecycle, metadata-size and provenance tests passed. The latest
  17 reconfiguration tests also pass after adding rapid supersession coverage.
- Real loopback SRTP repeated resolution/bitrate/FPS changes preserved sequence
  rollover, timestamp continuity, one source lease and independent H.264 decode.
- The original 144-frame moving-scene fixture now emits **144/144** through the
  production adapter/worker subprocess, versus **71/144** before this repair.
  Maximum PTS step is 41.667 ms rather than 1333.333 ms. All three source-P-frame
  transitions preserve the worker. Command times were 0.83–1.22 ms on this desktop;
  these are not server or end-to-end latency measurements.
- The actual fixture's maximum generation-local codec window is 76.4% of selected.
  The previously documented static 720p/132-kbps overshoot remains a separate
  empirical rate-control limitation. The 82% target has not been raised.
- Ruff and diff checks pass. No application deployment or Apple acceptance has
  occurred in this implementation pass.

The reproducible local qualification is retained under
`/home/james/Documents/Codex/2026-10-25/he/work/halo-server-fluid-stream-2026-11-07/`.
Its JSON includes source/probe hashes and loaded native library versions. A
[compact qualification receipt](fluid-video-qualification-2026-11-07.json) is
retained in Git and was refreshed against the final source on November 8. It uses
synthetic imagery; camera payload stays outside Git.
For the 61 complete one-second windows spanning configuration changes, the
maximum payload-to-budget ratio was 85.9%, using a per-frame allocation of each
selected rate at this fixture's fixed 24 FPS. This explicit accounting model is
not a claim about Apple's internal bitrate algorithm.

## Slice 2: receive-boundary observer — implemented and locally qualified

The [standalone observer](receive-observer.md) has no camera-opening or send path.
It uses a separate Linux packet socket with a locked exact-flow filter before
binding. The kernel retains only the fixed RTP header; artifacts contain numeric
timing/header observations and coverage/loss evidence. Duration, packet count,
storage, track count and output directory handling are bounded. It supports the
declared IPv4 direct-SRTP Ethernet path, with up to two visible VLAN tags; TURN,
IPv6 and other unsupported transport modes remain explicit exclusions.

All 54 focused tests pass. A separate native-kernel test on synthetic Unix-domain
datagrams accepted/truncated 18 valid fixtures and rejected five invalid fixtures.
It opened no packet socket and observed no network traffic. Independent review
resulted in pinned parent-directory operations and an explicit coverage boundary
before final statistics/cleanup. Actual AF_PACKET reception and kernel timestamps
still require a controlled host qualification. No capture service or persistent
privilege was installed.

## Slice 3: complete audio release — implemented and locally qualified

The [audio repair](audio-retention-repair.md) preserves the receiver's existing
first-release boundary, then makes up to four complete original Opus packets
available per callback. The existing HomeKit pacer owns delivery. A fresh,
authorized startup marker prevents the ordinary live-edge discard and catch-up
rules from consuming that already-incurred source retention. Shared/prewarmed
sources without a valid marker keep the conservative live-edge behavior.

No filtering, fabricated sound, independent timer, or additional reservoir was
added. Source PTS, original packet receipts, privacy/epoch fences and existing
storage limits remain enforced. Codec changes and sequence resets reestablish
prefetch; discontinuities reanchor pacing. Only CameraMediaSession opts in, so
other Ring receiver callers keep their existing callback contract.

The final 9.9-second captured payload excerpt was replayed at its original
callback intervals through the actual receiver, native audio conversion,
HomeKit pacer, loopback SRTP, and independent Opus decoder:

- Maximum send gap fell from 215.53 ms to 22.44 ms; gaps above 100 ms fell from
  five to zero. First output was 268.80 ms before and 260.61 ms after.
- All 166 original payloads were preserved. The candidate produced 497 decoded
  20 ms packets with zero decoder errors. Its four additional source inputs are
  the baseline receiver's retained stop tail, not synthesized sound.
- Retained output peaked at 17 packets / 3441 bytes, within unchanged limits of
  32 packets / 40 KiB. No initial discard, stale disposal, adapter restart,
  overflow, or budget rejection occurred in either captured replay.
- A separate native test with recurring 200 ms arrival holes retained all 182
  expected output packets. Focused receiver/source/audio regressions passed.

Original receipt preservation intentionally removes assembly delay from the
estimated audio phase. Tests exercise actual audio transport against a shared
video timeline and common sender-report instant; the video side is a model,
not a real encoder/sender. Physical HomeKit A/V synchronization remains unproved.

## Final integrated validation — November 8

The final Python suite passed **2909 tests**, with one IPv6 loopback test skipped
because the host lacks that address. All **268 HomeKit adapter tests** passed
after rebuilding with pinned Node 26.12.1. Ruff lint/format and whitespace checks
passed. The suite reported 76 existing upstream deprecation warnings.

Broad validation caught and repaired a synthetic video fixture missing the new
configuration metadata and an audio queue refactor that rebuilt the original
decoder tuple. The queue now forwards the exact original item; additional drained
frames receive their own items. The original identity assertion remains intact,
with additional exact-once forwarding coverage. The video clock regression also
verifies completion of the requested configuration transition.

The initial desktop runtime was newer than Halo's pinned Node version; the final
run used the existing qualified pinned runtime. Filesystem/user sandbox UID
remapping also made root-owned `/tmp` appear owned by `nobody`, correctly failing
release ownership checks. Those tests and the final complete suite passed with
real host ownership outside that sandbox. No release-security rules were relaxed.

## Physical qualification still required

The local video and audio qualifications support these repairs; they do not prove
that all full-session stutters are resolved. No server deployment or new camera
session occurred during this implementation pass. The VPN may explain Apple's
quality choices but has not been isolated as a cause.

Next, qualify the bounded observer on the server and run a controlled Apple
session through startup, changing quality, sustained playback and shutdown.
Check real A/V synchronization and previews, and use the observer to separate
packet delivery before callbacks from application dispatch delay. A matched
VPN-on/off comparison can then isolate viewing-path effects without forcing a
quality level or changing multiple variables together.
