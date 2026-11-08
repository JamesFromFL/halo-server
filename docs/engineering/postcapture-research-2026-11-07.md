# Halo Server: research after the full stream capture

Research date: November 7, 2026, America/New_York. Status: research and isolated experiments complete; production implementation and physical acceptance remain pending.

The strongest repair is to preserve the input video decoder when HomeKit changes output settings. A separate audio prototype should use the packets Halo already retains to absorb incoming bursts. The remaining shared audio/video disturbance needs one additional measurement boundary before attributing it to Ring, the network, or host receive dispatch. This evidence does not justify replacing the media backend.

## Scope and evidence

Audited Halo revision: `66f5c812134f5c306bc0fdd0116e03d126ddda89`. The production checkout was clean before research. This pass used the completed floodlight capture and isolated local experiments; it did not open another camera session, deploy a build, change pairing, or enable HKSV.

Capture: `run-444438c97d0542210c9dedb20353b2f0`, approximately 157.85 seconds of live media, within a closed→open→running→closed lifecycle. Timing metadata covers the lifecycle with `trace_complete=true` and zero recorder-loss counters. The rolling media excerpt contains only the final roughly ten seconds. It cannot supply payload or source keyframe flags for earlier quality transitions. Copied artifact sizes and hashes were verified against the manifest.

The main questions now have the following answers:

| Question | Finding | Strength and limit |
| --- | --- | --- |
| Does Halo create the longer video holes? | Output reconfiguration destroys the input decoder, then discards source P frames until Ring sends another IDR. | Captured timing, source inspection, and native reproduction agree. |
| Can output settings change without that wait? | Keeping the decoder and replacing only the encoder preserved every source frame in the main synthetic experiment. | Native feasibility proved; production IPC, lifecycle integration, and Apple playback not yet tested. |
| Why do picture and sound still hesitate together? | Shared incoming gaps have a strong two-second pattern and already appear at the UDP callback boundary. | Location narrowed; the physical origin remains unknown. |
| Can existing audio retention help? | Current arrival-driven release holds packets but does not spend that reserve during incoming gaps. | A timing model strongly supports a prototype; it is not a working receiver or listening test. |
| Why does output become low resolution? | Apple requests 360p/132 kbps; Halo additionally targets 82% of that codec budget. | The values and policy are confirmed. Apple's internal reason for choosing them is not exposed. |
| Are output RTP clocks the problem? | Source-PTS mapping and sender-report slopes remain consistent. | Another output clock rewrite is unsupported; absolute source A/V phase and Apple rendering remain unproved. |

## 1. Preserve the input decoder during output changes

The request path is `homekit-adapter/src/video.ts` → `src/halo_server/homekit/broker.py` → `src/halo_server/media/homekit_live.py` → `AccessUnitAdapter.reconfigure` in `media/transcode.py`. Halo correctly retains the HomeKit RTP session, sequence, timestamps, sockets, and audio pipeline. However, almost every changed video setting resets the native worker. That worker owns both the Ring H.264 decoder and the HomeKit encoder. Retaining SPS/PPS does not retain the decoder's reference pictures.

The physical session contains three quality transitions: 720p/299 kbps → 360p/132 → 720p/299 → 360p/132. All 56 empty video conversion feeds fall across those transitions. The corresponding encoded-output wall-clock gaps are 1.080, 1.429, and 0.242 seconds. Source packets continue arriving. The largest output PTS step is 1.333 seconds, while the largest source PTS step is approximately 83 ms.

The isolated test supplied 144 moving synthetic 720p frames at 24 FPS. Source IDRs occur at frames 0, 48, and 96; output changes occur on P frames 17, 75, and 123.

| Implementation | Output frames | Largest output PTS step |
| --- | ---: | ---: |
| Current production adapter and real worker subprocess | 71/144 | 1,333.333 ms |
| Experimental encoder-only replacement, same source/settings | 144/144 | 41.667 ms |

Every experimental transition begins with independently decodable IDR/SPS/PPS output. Source timestamps remain exact. Additional probes preserved delayed B-frame source origins and pending force requests. A 30→12→30 FPS ceiling test produced only the intended frame selection, without waiting for a new source IDR. These are codec tests: the candidate runs through an in-process worker subclass, and does not yet implement the production command protocol or test Apple Home.

The first FPS prototype exposed a real design trap: restoring a higher FPS ceiling while retaining the old capped nominal rate can overshoot the codec budget. Resetting only the output cadence estimator to the new ceiling corrected that experiment. Geometry and bitrate changes with unchanged FPS should preserve the existing cadence estimate.

Use a replacement encoder context instead of assigning only `bit_rate`. FFmpeg/x264 expose some dynamic rate changes, but not a complete geometry/FPS transition through the current PyAV interface. PyAV 17.1.0 does not expose all the needed VBV setters. An encoder-only replacement provides a supported, complete boundary without replacing PyAV or aiortc. Sources: [FFmpeg libx264 wrapper](https://github.com/FFmpeg/FFmpeg/blob/n8.0.1/libavcodec/libx264.c), [x264 API](https://github.com/mirror/x264/blob/master/x264.h), [PyAV codec context](https://github.com/PyAV-Org/PyAV/blob/v17.1.0/av/codec/context.py).

### Proposed implementation boundary

- Add one serialized, bounded configuration command to the existing killable worker. Validate finite fields, cap the message, acknowledge its exact revision, and preserve existing timeouts and cleanup.
- Prepare the replacement encoder before committing. Preserve the decoder, decoder-held origins, original receipt times, extended source PTS, and pending force requests. Emit a fresh output IDR and SPS/PPS using the next real decoded picture.
- Preserve RTP identity, sequence, media time, audio, leases, and capacity accounting. Commit configuration and repair-cache changes using the existing adapter→send lock order.
- Classify identical, RTCP-only, and MTU-only changes separately. An MTU change needs packetization/cache handling, not destruction of decoded reference pictures. Continue rejecting payload-type changes through this path.
- Retain full source recovery for actual source epoch/parameter discontinuities or damaged reference chains. A command timeout has unknown child state and must retire the view safely.

The continuity guarantee applies to **adapted video remaining adapted**, as in this capture. A compressed-copy stream has no warmed decoder; copy→adapt may still need a source IDR. A continuously warmed decoder or GOP replay cache is a separate resource/design decision, not implicit scope for this repair.

## 2. Locate the periodic incoming disturbance

The capture contains 2,610 contiguous audio RTP sequences with 60 ms timestamp increments. There are 43 audio arrival gaps of at least 200 ms; every one overlaps at least 100 ms of video silence. The longest audio gap is 359.577 ms. Twenty combined audio/video gaps exceed 200 ms, with the longest at 342.782 ms.

Those twenty shared gaps start within 13.23 ms of the same phase of a two-second cycle. This is descriptive evidence of a periodic disturbance, not proof of its cause. It persists after Halo's upstream PLI requests have stopped. The retained excerpt does not establish that source keyframes cause it.

The earliest UDP callback gap reaches 343.472 ms. In contrast, maximum measured ICE queue wait is 12.277 ms, DTLS dispatch is 13.041 ms, and receiver wake-probe delay is 19.355 ms. Inside long audio gaps, 99 of 103 sampled UDP queue observations are empty. Queues do accumulate at other times; samples cannot substitute for per-packet kernel timestamps.

The next decisive instrument is a bounded, independently owned observer of kernel receive timing for the selected media transport. It must not replace or compete with aioice's socket reader. Correlate packet header sequence/timestamp/size with the existing callback records, retain clock anchors and loss/coverage counters, identify supported TURN framing explicitly, and keep media payload and unrelated traffic out of the retained output. Kernel software receive time is still not physical wire time. [Linux timestamping documentation](https://docs.kernel.org/networking/timestamping.html).

Then follow the evidence:

1. **Gaps already exist at kernel receive:** compare Halo with an actual pinned reference peer/signaling profile, serially on the same camera and network, with full cleanup between attempts. Change one negotiated dimension at a time.
2. **Kernel arrivals are regular but callbacks bunch:** investigate host scheduling, socket dispatch, and the affected queue rather than changing Ring's backend.

The existing synthetic reference comparison did not negotiate a real Ring session. It cannot establish whether BUNDLE, codec offers, feedback, signaling endpoint, or Ring's reported `transcoding=true / codec_mismatch` causes these gaps. That report also does not identify the responsible media track.

Separate downstream delays remain visible, including consumer/worker waits and late audio sends. Kernel measurements narrow the incoming problem; they do not dismiss local scheduling costs elsewhere.

## 3. Use existing audio retention more effectively

Current audio assembly is arrival-driven: after initial prefetch it releases at most one frame when another packet arrives. The original packet-to-frame delay is a median 247.987 ms, yet frame releases still pause for up to 359.530 ms. The retained packets provide lookahead, but the current release rule cannot use them while arrivals pause.

An RTP Opus payload is one complete Opus packet, possibly containing multiple codec frames. Its duration must be validated, but another RTP timestamp is not required to discover its boundary. This is codec-specific and must not be generalized to fragmented video. [RFC 7587 §4](https://www.rfc-editor.org/rfc/rfc7587.html#section-4).

An offline model starts at the **same observed first audio release**, then schedules according to actual media time. It finds 2,609 of 2,610 complete packets already available at their deadlines. The sole deficit is 5.685 ms. This uses an online-known starting event, not a favorable offset fitted to future arrivals.

That result excludes actual worker/pacer scheduling, reordering, packet loss, changing Opus durations, and audiovisual synchronization. It is promising availability evidence, not proof of uninterrupted audio in Apple Home.

Compare two isolated prototypes: clock-driven release of complete retained packets, and bounded draining into the existing audio conversion/output queue so the existing HomeKit pacer remains the sole presentation scheduler. The second may avoid a redundant timer. Both must preserve original arrival/PTS provenance, queue and batch bounds, retirement fencing, and other consumers' semantics.

Qualification must cover loss, late/duplicate/reordered packets, wraparound, DTX, malformed or variable-duration Opus, source restart, and stop/revoke during release. Use known synchronized A/V fixtures through the actual paced output path, inject captured scheduling delays, and measure real latency. Preserve samples through the new release stage; no denoise, gating, fake replacement sound, or new 1.2-second playback reservoir is proposed.

## 4. Separate Apple's selection from Halo's quality policy

The shipped HAP parser reads width, height, FPS, and maximum bitrate from Apple's selected-video fields. Halo forwards them without a fallback to 360p or a bitrate-unit error. The classic request exposes no reason, viewport, orientation, network type, or renderer-buffer state. After this research pass, the owner confirmed that the viewing phone uses a VPN. This is a plausible contributor to controller adaptation, not proof of the reason for a particular request. Halo must honor changing selections regardless of route; it must not infer or force a quality override from VPN use. Source: [HAP-NodeJS selected configuration parser](https://github.com/homebridge/HAP-NodeJS/blob/bd7a01b889cf1c74cbfcc4a4e5efdfa53fc3532b/src/lib/camera/RTPStreamManagement.ts#L1020-L1054); the shipped 2.2.3-halo.2 path was checked separately.

The camera source was 2546×1440 at approximately 2.36 Mbps. Halo advertises up to 1080p, and Apple's final selection was 640×360 at 132 kbps. Halo's additional 82% encoder target reduces that to about 108 kbps. This reserve is Halo's empirical rate-control policy, not an Apple-required percentage. Native source detail cannot survive that reduction unchanged.

Fresh reference inspection explains an important difference. Scrypted's compatible H.264 path normally copies video; requested dimensions can be upstream hints, and reconfiguration forwards bitrate through optional feedback. Homebridge Ring forwards compressed video and acknowledges reconfiguration without changing it. Neither wrapper performs Halo's input-decoder reset. Their sharper output can therefore reflect a different response to the same request, without proving Apple selected a larger budget. Sources: [Scrypted request handling](https://github.com/koush/scrypted/blob/0482ac4b0bb1bf1d17a5da847f6ad55df84a680e/plugins/homekit/src/types/camera/camera-streaming.ts), [Scrypted FFmpeg path](https://github.com/koush/scrypted/blob/0482ac4b0bb1bf1d17a5da847f6ad55df84a680e/plugins/homekit/src/types/camera/camera-streaming-ffmpeg.ts), [Homebridge Ring](https://github.com/dgreif/ring/blob/1c52affa18084e9edbf6ae6200b531ab0aa064f5/packages/homebridge-ring/camera-source.ts).

First repair continuity while keeping quality policy stable. Then benchmark the 82% reserve against moving scenes, new-encoder IDRs, and repeated FPS/bitrate changes. Preserve both generation-local and cross-generation budget windows. The isolated bitrate-only test reached 101.10% of selection; a static 720p/132-kbps control already reached 103.10%. This is an existing rate-envelope limitation, not a reason to hide failing cases or blindly raise the target. Define transition-window expectations explicitly: bytes sent before a bitrate reduction cannot be withdrawn.

An explicit source-preserving interoperability mode could be evaluated separately on real receivers and local/remote routes, with truthful reporting and a qualified fallback. Silently ignoring selected settings is not recommended as the default repair.

Apple now documents a separate developer-preview HEVC/multiple-tier design with 2K/4K capabilities. It requires substantial protocol and concurrent-encoding work; upgrading HAP alone does not activate it, and the present viewer's support is unverified. It is a future native-resolution project, not a correction to this classic H.264 session. [Apple's HomeKit Secure Video Open Source Compatibility Guide](https://developer.apple.com/download/files/HomeKit-Secure-Video-Open-Source-Compatibility-Guide.pdf).

## 5. Preserve working clock behavior

Independent analysis checked 3,638 video and 7,812 audio output mappings. They retain their source-PTS relationship apart from floating-point noise; video presentation residual is under one microsecond. All 289 video and 31 audio sender reports preserve their declared clock slope within one RTP tick.

This argues against a repeated output-clock reset or wrong timestamp slope. It does not establish absolute source A/V phase or what Apple rendered. Keep the negotiated classic-HAP 90 kHz video/24 kHz audio conventions distinct from source Opus's 48 kHz PTS and newer protocol rules.

Raw Apple jitter feedback and a locally reconstructed estimate differ substantially. Their interpretation remains unresolved. Do not divide live feedback by sixteen, infer physical network delay from that figure alone, or change clocks on that basis. Preserve raw and derived values separately. [RFC 3550](https://www.rfc-editor.org/rfc/rfc3550.html#appendix-A.8).

## Development sequence and acceptance gates

1. **Video worker command:** implement atomic output-encoder replacement; test P-frame transitions, independent IDR decode, exact PTS, B-frame origins, force ordering, bounds, invalid acknowledgements, cancellation, and worker cleanup.
2. **Video adapter and transport integration:** preserve source state and classify transport-only changes. Test rapid reconfiguration, in-flight feed/repair, stop/revoke, source discontinuity, copy→adapt recovery, MTU reduction, and unchanged audio/session identity.
3. **Video qualification and transition evidence:** test FPS/rate behavior on busy scenes and across configuration boundaries. Record request received/applied/acknowledged/first output IDR, changed fields, decoder/encoder generations, and selected versus actual encoder budget. Keep known overshoots visible.
4. **Audio prototype and gated integration:** compare the two release designs through the actual converter/pacer with loss/reorder and synchronized A/V fixtures. Proceed only when continuity improves without extra startup latency, sample processing, or unsafe queue growth.
5. **Receive-boundary diagnostic and controlled test:** add bounded kernel correlation plus source-IDR metadata; capture a short full lifecycle with stable viewer/network context. Branch to actual reference-peer comparison only if receive evidence supports it. Physical Apple playback remains the acceptance test.
6. **Quality policy qualification:** benchmark reserve changes separately after continuity is understood. Treat source-preserving mode or the newer Apple protocol as explicit product decisions requiring their own compatibility work.

These are implementation boundaries, not a claim that six commits will resolve every upstream condition. A failed experiment or unexpected receiver behavior may require a revised plan. Keep each completed change independently tested and reviewable; preserve pairing and keep HKSV off during live-view qualification. No backend swap, production deployment, or Git push occurred in this research pass.

## Reproducibility and retained artifacts

Detailed research and scripts are retained locally under `/home/james/Documents/Codex/2026-10-25/he/work/halo-server-postcapture-research-2026-11-07/`:

- `reconfigure-research.md`, `reconfigure_probe.py/.json`: native feasibility, source hashes, loaded codec versions, rejected FPS prototype, and rate-envelope counterexamples. Runtime was Python 3.14.9, PyAV 17.1.0, libavcodec 62.28.101.
- `arrival-research.md`, `analyze_arrivals.py`, `arrival-analysis.json`: gap correlation, two-second periodicity, lower-stage observations, queue samples, and coverage.
- `analyze_prefetch.py`, `prefetch-counterfactual.json`: observed audio retention versus hypothetical deadline availability.
- `quality-research.md`, `upstream/receipt.json`: fresh pinned reference code, hashes, shipped parser checks, and unresolved controller questions.
- `clock-and-measurement-research.md`, `clock_probe.py/.json`: independent output time mapping and next measurement boundary.

The capture overview is retained at `/home/james/Documents/Codex/2026-10-25/he/outputs/halo-server-diagnostic-session-2026-11-07.md`. These machine-local paths are evidence locations, not runtime dependencies. Camera media and capture records remain outside the repository. Scratch programs become maintained regression tests only in their relevant implementation slices.
