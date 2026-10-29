# Source-time live video encoding and bounded delayed output

This slice corrects rate-control underallocation when Ring provides fewer real
frames than Home's selected maximum. It preserves the existing source cadence
ceiling and 90 kHz PTS; it never invents live frames or fills source gaps.

The prior zerolatency tune selects x264 FPS-only rate control. At a selected
30fps with a real15fps source, passing accurate source PTS did not make rate
control use the real duration. The new fixed `force-cfr=0` setting uses source
timestamps. This introduces one pending frame in the qualified native pipeline.
That delay is explicit and qualified; changing the option alone was unsafe.

## Native ownership and protocol

The private child request header is `!cQQI`: operation, original source PTS,
force-request token (zero means none), and payload byte count. A response has
`V`, output count, pending-encoder-frame count, then up to four entries containing
`!QQI` (actual packet PTS, matching force token, byte count) and encoded bytes.
Total encoded payload is at most2MiB; the response envelope is bounded by
2MiB+128bytes. The existing tuple-of-PTS/payload return API remains. The parent
reads the immutable `last_output_force_requests` tuple immediately under its
adapter operation lock, with no intervening await.

The native process retains at most64 input/encoder origin metadata entries,
at most8 encoder-pending origins and at most8 queued force requests. Actual
returned packet PTS must match an encoded-frame origin. Multiple packets for
one picture are grouped only by their actual PTS; frames returned together do
not acquire the triggering input's timestamp. Missing or ambiguous origins fail
explicitly. Delayed outputs retain R6a's original receipt, capture epoch and
configuration generation.

Each force revision is dispatched once. A request received while the source
decoder emits no frames waits in the bounded FIFO for an eligible real decoded
frame. An old P picture released by a newer forced input cannot acknowledge that
request. The adapter accepts an acknowledgment only for a known token and an
actual IDR containing current SPS/PPS. An older acknowledgment cannot clear a
newer request received during an await. Unresolved requests have an eight-token
admission bound and a convergence limit of32 additional native inputs or two
seconds of source time. All pending origin/force state is fenced and cleared on
retirement; the exact worker remains owned until its actual cleanup succeeds.

The capability probe is terminal: one synthetic frame is encoded and explicitly
flushed, independently decoded, then its worker is closed. A probed worker
cannot subsequently feed a live stream. Live STOP does not flush a fabricated
frame or claim its final pending picture was delivered. The bounded final tail
is discarded with its worker, and tests account it explicitly. Production
recording uses its independent RecordingWorker and encoder/finish lifecycle;
this slice does not enable VFR for that recording encoder.

## Codec budget and measured quality

The classic negotiated bitrate describes codec output averaged over a second.
It does not define the physical protected-wire serialization rate. Network
pacing, finite burst credit, retransmission and transport deadlines are separate
work. The encoder now uses a measured90% codec target with250ms VBV. This reserve
is justified by native codec-window measurements, not by charging RTP overhead
against codec bits. It is empirical, not a universal one-second bound; the
independent codec-window monitor remains necessary.

Scratch evidence compared actual isolated native workers using six seconds of
the same720p moving gradients/checkers/rectangle source at15/30fps, selected30.
Reference quality is decoded source scaled to the same output geometry. Metrics
use only common output PTS, excluding the final candidate pending tail. No real
media or images were stored. Representative15fps results were:

| Selected output | Frozen R6a CFR86% mean codec kbps / Y-PSNR | New source-time90% mean codec kbps / Y-PSNR | New peak covered1s codec kbps |
| --- | --- | --- | --- |
|640x360,132kbps |57.108 /20.866dB |95.275 /23.458dB |106.880 |
|1280x720,299kbps |121.173 /16.388dB |240.247 /18.940dB |269.664 |

Non-overlapping8x8 luminance SSIM improved from0.478 to0.543 and0.488 to0.524.
This is the explicitly named block metric, not a claim of MS-SSIM or subjective
parity. Native30fps controls also improved modestly, with covered1s peaks
128.976/284.328kbps. All four representative cases fit their selected windows.
Full100% codec-target candidates exceeded them: the30fps peaks were143.280 and
308.416kbps. The configured ABR/VBV target alone therefore cannot certify a
one-second maximum for arbitrary content. Longer runs and live monitoring may
still expose peaks requiring a separately reviewed rate-control policy.

The comparison baseline is the **undeployed R6a/R5 staging encoder**, not the
installed R4d server. R4d uses full selected target and1s VBV; these measurements
do not attribute its reported stutter to R5's undeployed86%/250ms settings.

## Color and aspect handling

Known matrix, primaries, transfer and full/limited-range metadata from the first
real decoded frame are carried to the scaled frame and encoder context before
opening it. Unknown metadata stays unknown. A later incompatible metadata change
fails instead of silently using a wrong fixed context. Known non-square source
pixels are normalized to the requested square-pixel geometry while maintaining
display aspect ratio. Letterbox black is0 for full range and16 for limited or
unspecified range. No denoise, sharpening or other content filter is added.

Tests use RGB color bars converted with an explicit BT.709 matrix, full and
limited range, and2:1 source pixel aspect. The real encoded output independently
decodes with correct color metadata, square pixels, matching bar colors and
black borders. The shared letterbox helper retains its positional API for the
recording caller; recording codec/clock regression tests remain required.

## Qualification limits

The native tests cover all39 advertised profile/level/geometry combinations,
real B-frame input reorder, rollover and jittered PTS, source15/selected30 and
native30 cadence, delayed/multiple output grouping, force during a pending feed,
decoder-empty force FIFO, finite nonconvergence, STOP/late worker cleanup,
original receipts, independent IDRs, codec-window/PSNR thresholds for the moving
fixture, and public HAP/IPC/SRTP composition. A synthetic independently decoded
picture is not Apple playback acceptance. No Ring image/audio parity, physical
Home stability, remote network behavior or universal scene-quality guarantee is
claimed. The owner must still assess a controlled Home pilot.
