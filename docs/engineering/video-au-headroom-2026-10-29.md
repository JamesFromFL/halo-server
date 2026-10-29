# Native video headroom for complete access-unit admission

The R5 sender must finish an entire protected access unit before its original
receipt plus 500 ms. The complete cost is
`max(now, next_packet_at) + sum(len(RTP_packet) + 10) * 8 / selected_bits_per_second`.
It includes native conversion delay and existing transport backlog. Starting an
IDR that cannot finish this budget leaves a damaged reference chain; dropping
only later P frames cannot repair that chain.

The actual frozen R4b `VideoWorker` was therefore tested with six-second 720p
synthetic source streams at 15 and 30 fps. A static flat scene was the control.
The moving scene contained color gradients, checker texture and a moving colored
rectangle. Source packets passed through the actual isolated worker, native
decoder, scaler, encoder, packetizer and SRTP protection. Every actual IDR had
NAL 5 plus SPS/PPS and decoded in a fresh independent native decoder. Every worker
was joined; no account, network or real image was used or saved.

The old encoder assigned the entire selected rate to payload with a one-second
VBV buffer. This produced infeasible representative IDRs even when source data
was newly received. The work-only candidate retained the same encoder and fixed
zerolatency/CFR settings but used 86% of the selected rate for payload and a 250 ms
VBV buffer, rounded up to whole kilobits. The native comparison was:

| Source fps / selected output | Old max IDR protected wire ms | New max IDR protected wire ms | Old max modeled finish age, fresh ms | New max modeled finish age, fresh ms | New finish age with 250 ms source age, ms |
| --- | ---: | ---: | ---: | ---: | ---: |
|15 /640x360 at 132 kbps |458.242 |147.091 |538.234 |220.201 |470.201 |
|15 /1280x720 at 299 kbps |506.809 |126.234 |699.177 |187.710 |437.710 |
|30 /640x360 at 132 kbps |458.061 |150.606 |765.221 |221.854 |471.854 |
|30 /1280x720 at 299 kbps |502.823 |119.813 |862.804 |168.364 |418.364 |

The models used the actual cold/steady feed latency for each AU and serialized
all outputs in order. New moving-scene steady feed maxima were 18.421/23.266 ms
at 15 fps and 14.091/17.311 ms at 30 fps. Cold feeds, including child startup, were
73.110/88.713 ms and 71.248/72.872 ms respectively. Mean protected output at 30 fps
was 114.743 kbps and 240.889 kbps, below the selected 132/299 kbps ceilings. Flat
controls fit the original and candidate policies. All 16 probe cases completed;
all 48 IDRs independently decoded. These are host-specific synthetic measurements.

Backlog is a separate cost. The corrected work model initializes its pending
transport frontier after the first AU's ready time plus the stated independent
backlog. Earlier draft wording described an absolute frontier from source-time
zero, which overlapped source age; that wording is superseded. A 250 ms old AU
with another 250 ms of pending transport cannot admit any positive wire cost.
Those cases correctly remain infeasible. No 500 ms deadline was relaxed.

The product change only applies the measured 86% payload target and 250 ms VBV
setting in `transcode_worker.open_encoder`. It preserves source PTS, resolution,
profile/level validation, source-time two-second real IDRs, SPS/PPS repetition,
single-thread isolation, fixed encoder options and existing process bounds.
No VFR option, codec/backend, frame duplication or source observer was changed.
R6 remains responsible for any separately qualified cadence/rate-control change.

The new native regression sends moving 15/30 fps streams through both selected
outputs. It checks every dependent frame with a continuous decoder, each IDR
with a fresh decoder, exact input/output PTS, protected 1200-byte packet limits,
representative IDR wire cost below 200 ms, finite aggregate wire rate and worker
retirement. Its deterministic serialization example explicitly budgets 250 ms
source age plus 50 ms conversion, and separately rejects 250 ms age plus 250 ms
backlog. It does not assert a wall-time guarantee on arbitrary hosts.

These encoder settings leave practical transport headroom; they are not a
universal bound for every scene, very low selected bitrate, or lost/delayed source
packet. Actual complete-AU admission remains authoritative. Repair traffic and
backlog must still be charged there, and an over-budget unit must enter the
bounded IDR recovery path. A real Apple/Ring pilot remains necessary to assess
picture quality, uninterrupted playback and Home compatibility after deployment.
