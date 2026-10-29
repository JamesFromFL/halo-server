# Ring delivery and codec attribution: R7e

This is a passive diagnostic addition and primary-source comparison. It changes
no SDP, codec preference, transport, microphone, media clock or playback policy.
No server, camera or real media has been accessed by this task.

## What the physical evidence establishes

Parent-provided R7b metadata reports `transcoding=true` and
`transcoding_reason=codec_mismatch`, observed once with the instance observer
installed before the SDK reader started. The safe answer summary reports H264
High `640032` and Opus. It did not report packetization mode, full remote codec
parameters or whether the mismatch referred to audio, video or a camera-native
format. Those meanings remain unknown. Preferring the same High entry first is
therefore a mechanically testable but weak next experiment.

Incoming media quiet intervals have already been observed at the owned UDP
boundary. A downstream quality setting or HomeKit RTP timestamp cannot by itself
explain why no encrypted media reached that boundary. Neither a zero sampled ICE
queue nor local socket drop counters proves that no earlier transport stage
batched media. Root's separate authenticated abs-send-time observer is a useful
additional comparison, if negotiated and present. It must retain the distinction
between an unknown extension and a measured relative sender-clock advancement.

## Primary-source comparisons

The pinned Scrypted Ring source uses API 4.0 signaling, `activate_session`, then
audio/video `stream_options`, and a 5 s ping. Halo already has the post-activate
stream-options correction; its physical trial did not remove the periodic quiet
interval. No low-latency delivery selector was found in these request fields.

The newer independent ring-mqtt source is a useful cross-check, not evidence
that its topology would fix this camera. Pinned commit
`59aab41b4afd1692148b1180a6b0920591c86db1` declares package 5.9.3 and follows the
same API 4.0 activation/options sequence. It offers mode-1 H264 Baseline
`42001f`, Opus/48000/2 plus PCMU, audio first, and unbundled Werift transports.
Those are several differences; changing them together would destroy causal
attribution. There is no primary Ring description tying one of these differences
to the observed 2 s burst period.

The ring-mqtt maintainer's history associates some codec mismatches with an HEVC
rollout and later describes a streaming API that negotiates those cameras down
to AVC. This supports checking camera-native format as a hypothesis. It does
not establish that this source is HEVC, that the present mismatch is video, or
that a new client/backend is necessary. No authoritative Ring definition of
the `codec_mismatch` reason's track scope was found.

Pinned aiortc `find_common_codecs` copies the local H264 codec entry after
matching profile and packetization mode. It does not replace that entry's level
with the remote answer's level. The new actual-code test demonstrates remote
High `640029` while the encoded queue sees local High `640032`. Consequently,
receiver codec parameters and exact remote answer parameters must be separate
observations. Actual encoded SPS remains a third, different observation.

Opus signaling `48000/2` is required by RFC 7587 even when encoded content is
mono or has a lower audio bandwidth. A 16 kHz mono SILK TOC is not, by itself,
an SDP mismatch. This audit does not introduce a 48 kHz classic HomeKit wire
clock or claim Opus passthrough qualification.

## Implemented passive observation

The isolated helper records:

- A fixed remote-answer DTO: audio/video direction, bounded codec names,
  declared H264 profile/mode/asymmetry, bounded known Opus parameters, and
  finite RTCP-feedback/header-extension flags.
- A separate actual encoded-frame count and receiver codec interpretation for
  each track. It does not emit payload types, SSRCs, URLs, addresses, codec
  objects, raw SDP, arbitrary fmtp or media.
- Explicit answer states `not_seen`, `observed` and `unavailable`; missing
  packetization mode is distinguished from an explicit value. Invalid metadata
  cannot change the original answer application or queue forwarding outcome.

The helper parses only the answer already accepted by `setRemoteDescription`.
Input is limited to 64 KiB, with at most 4 media sections and 16 codec declarations
per section checked before parse and rechecked afterward. It retains only safe
metadata and up to two weak references, is fenced at attempt retirement, and uses
the existing owned 1 Hz diagnostic sampler. No additional task is created.

## Ranked next observation or experiment

1. Collect the exact remote answer and actual encoded-frame interpretation,
   alongside authenticated send-clock presence/advancement. Compare the same
   active attempt; do not treat an old or absent field as a current negative.
2. If packetization mode is 1 and profile is already High, do not claim that
   a High-first preference addresses the demonstrated mismatch. Use the new
   fields to identify a concrete contract difference before another pilot.
3. If evidence identifies a codec conversion stage, test one public, bounded
   preference change at a time while retaining compatible fallbacks/RTX, with
   the same quiet-interval and `stream_info` measurements. An order-only test is
   not a HEVC-native test. HEVC reception/decoding would be a wider source and
   native-worker change requiring its own explicit plan and qualification.
4. Keep unbundling, audio-first and idle-microphone experiments separate. They
   remain unproven and are not implemented by this slice.

## Primary references and local evidence

- Scrypted commit `50b8668ea8d36f281d5ab6d152cd47f3d45e57f5`, local
  `/home/james/Github/scrypted/external/ring-client-api`, Ring fork commit
  `516e96a24ec279168c246795e623b6bfdf58ec45`:
  [WebRTC connection](https://github.com/koush/ring/blob/516e96a24ec279168c246795e623b6bfdf58ec45/packages/ring-client-api/streaming/webrtc-connection.ts),
  [peer configuration](https://github.com/koush/ring/blob/516e96a24ec279168c246795e623b6bfdf58ec45/packages/ring-client-api/streaming/werift-peer-connection.ts).
- ring-mqtt pinned primary files, mirrored under `sources/`:
  [peer configuration](https://github.com/tsightler/ring-mqtt/blob/59aab41b4afd1692148b1180a6b0920591c86db1/lib/streaming/peer-connection.js),
  [WebRTC connection](https://github.com/tsightler/ring-mqtt/blob/59aab41b4afd1692148b1180a6b0920591c86db1/lib/streaming/webrtc-connection.js),
  [maintainer change history](https://github.com/tsightler/ring-mqtt/blob/59aab41b4afd1692148b1180a6b0920591c86db1/docs/CHANGELOG.md).
- aiortc 1.15.0 primary source:
  [common codec matching](https://github.com/aiortc/aiortc/blob/1.15.0/src/aiortc/rtcpeerconnection.py),
  [authenticated RTP parsing and routing](https://github.com/aiortc/aiortc/blob/1.15.0/src/aiortc/rtcdtlstransport.py).
- [RFC 7587 section 7: Opus SDP](https://www.rfc-editor.org/rfc/rfc7587#section-7),
  [RFC 6184 section 8.2.1: H264 packetization mode](https://www.rfc-editor.org/rfc/rfc6184#section-8.2.1).
- Physical metadata supplied by parent:
  `evidence/lifecycle-R7b/codec-metadata.json`. No raw SDP was copied into this
  research folder or the diagnostic output.

No result here establishes why Ring batches delivery or proves an Apple
playback improvement. The tests establish the new diagnostic's provenance,
bounds, source lifetime and noninterference.
