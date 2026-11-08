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
  transitions preserve the worker. Command times were 0.85–1.17 ms on this desktop;
  these are not server or end-to-end latency measurements.
- The actual fixture's maximum generation-local codec window is 76.4% of selected.
  The previously documented static 720p/132-kbps overshoot remains a separate
  empirical rate-control limitation. The 82% target has not been raised.
- Ruff and diff checks pass. No application deployment or Apple acceptance has
  occurred in this implementation pass.

The reproducible local qualification is retained under
`/home/james/Documents/Codex/2026-10-25/he/work/halo-server-fluid-stream-2026-11-07/`.
Its JSON includes source/probe hashes and loaded native library versions. It uses
synthetic imagery; camera payload stays outside Git.

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

## Remaining audio and physical qualification

Audio release is being qualified through the actual output pacer. Its existing
startup discard and catch-up rules must not consume the intentional source
retention; any new path must preserve original receipt/PTS and A/V alignment,
bounded storage, permission epochs and raw audio. The arrival-only timing model
does not qualify production integration by itself.

Actual privileged host capture and a subsequent controlled Apple session remain
physical test gates. The observer will separate packet delivery before callbacks
from application dispatch delay without replacing the active UDP reader.
