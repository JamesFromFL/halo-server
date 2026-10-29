# R4: source-time video cadence and IDRs

This slice corrects local cadence and managed-encoder keyframe timing. It does
not claim that these defects explain every observed Ring/Home playback pause.
The measured Ring source is about 15 fps while Home's selected maximum is 30 fps;
source arrivals, audio pacing, wire admission and stable RTCP mapping are separate
repair slices. No Ring connection, account or physical Home test is used here.

## Resulting behavior

`media/cadence.py` selects at most one existing source frame per nearest rational
nominal phase. The origin and phase are integers in the 90 kHz source domain; a
half-period tie stays in the earlier phase. Alternating2,999/3,001 tick spacing at
30 fps therefore keeps every frame, rather than the previous greedy filter's
50% loss. Exact 60 fps to 30 fps selects original timestamps 0, 3,000, 6,000... . A
finite window can include one rounded boundary phase; its count is bounded by
floor(elapsed*fps/90000+0.5)+1 rather than accumulating drift. Frames are neither
duplicated nor retimestamped to pretend a faster source exists.

The compressed-copy gate uses the same phase evidence. One tick of timestamp
jitter keeps compatible bytes exact. A genuinely repeated phase switches to
waiting for an actual source IDR before adaptation; it does not skip a dependent
P frame and then resume its successors as if nothing happened. Source/capture/
configuration resets reset the phase. The first already-extended source timestamp
is preserved, and raw 32-bit wrap is still unwrapped through the existing delta
check. B-frame input is decoded in presentation order with unknown DTS left
unknown, as before.

`transcode_worker.py` now supplies actual source PTS and a 1/90000 encoder timebase.
The fixed selected framerate is retained. Managed output forces an IDR when two
seconds of admitted source time have passed since its prior keyframe, or on an
explicit request. Missing input cannot produce an image: the first available,
cadence-admitted source frame on/after that deadline is used. Thus the bound is
the deadline plus the next available source-frame gap, rather than a wall-clock
promise during source outages. Sparse 15 fps input selected at 30 fps no longer
stretches a frame-count GOP to four seconds.

The encoder explicitly enables public `forced-idr`, repeats parameters and refuses
unused top-level fixed AV options. The parent parses actual NAL 5/SPS/PPS, checks
parameter linkage and selected format, and rejects a forced/deadline P frame or
an IDR missing its current parameters. Keyframe flags alone are not proof.
Native tests independently decode each resulting IDR. Compatible copy remains
limited by Ring's real source IDR cadence; R5 owns current-lease upstream PLI,
decoder warmup and bounded dependency recovery.

## Deliberately separate rate-control issue

`zerolatency` still uses x264's FPS-only rate control. Changing encoder PTS alone
preserves timestamps but does not make variable input consume its full selected
bit budget. Twelve-second 15 fps source/30 fps selection native probes measured protected
rates 60.78→108.57kbps at selected 132, and 128.85→244.15 at selected 299 when public
`force-cfr=0` was enabled. That option also introduces one source frame of output
lookahead and changes SPS fixed-rate information to variable/unknown. R4 does not
silently add that buffering or change VUI assertions. R6 will pair that option
with an original-source-PTS receipt ledger, stable timelines and qualification.
The selected FPS remains a maximum, not a promise of invented source frames.

Primary contract/source checks:
- [x264 timestamp/FPS rate-control contract](https://github.com/mirror/x264/blob/master/x264.h#L485-L491)
- [zerolatency forces CFR](https://github.com/mirror/x264/blob/master/common/base.c#L642-L649)
- [force-cfr option](https://github.com/mirror/x264/blob/master/common/base.c#L1339-L1340)
- [FFmpeg public libx264 wrapper](https://github.com/FFmpeg/FFmpeg/blob/n8.0/libavcodec/libx264.c)

## Validation

`tests/test_video_source_cadence.py` covers rational phase boundaries, quantized
and fractional nominal rates, byte-exact copy, persistent copy dependency fencing,
actual native 15/24/30 fps and slower/faster input, extended timestamps/wrap,
discontinuity/capture resets, six-second source GOPs with only their first IDR,
independent output IDR decode and malformed worker keyframe/parameter responses.
Existing complete catalog, B-frame, worker capacity/kill/reap, native thread and
protected UDP/SRTP tests remain gates. HKSV recording code and history are unchanged.
