# Diagnostic-build baseline, October 28, 2026

Code `47ffd769e75da2523ad362b960cfcda2c0d0d81e`, server release
`b9bc11843546f3df5c275379da4bc94d0e0493bd71dac898903bcedc2ea0cfee`.
The paired floodlight was opened by the owner. This is real stream metadata,
not recorded camera media. Twenty-six snapshots span 125.077 seconds; the
existing session remained active. Pairing and exact HKSV-off preferences were
preserved. The observation alone does not qualify visible or audible playback.

- Halo read 124.38 seconds of original audio and sent 70.66 seconds: 56.81%.
  The interval includes 100 converter recreations, 240 source packets discarded
  with an expired prefix, 182 recovery skips and 382 unprocessed packets.
- Source ingress had 62 new pauses longer than 500 ms. Its largest observed
  pause was 1,048.562 ms, despite almost all source timestamp steps being
  60 ms (one measured 70 ms step). Encoded-frame arrival is after jitter/frame
  assembly, so this does not identify the network/cloud stage responsible.
- The source receiver reported zero lost packets. Home's final video receiver
  report showed zero cumulative loss; its audio report showed two. These
  reports do not account for audio Halo discarded before sending, and do not
  prove Apple's rendering was continuous.
- Mean legacy encode cost in the interval was 2.179 ms; the lifetime maximum
  was 26.898 ms. Source-loop wakeup lag peaked at 14.238 ms. Authority lock wait
  peaked at 0.216 ms. This supports lock isolation as robustness work without
  identifying it as the main cause of this session's dropouts.
- The requested video contract at both sampled ends was 640x360, 30 fps,
  132 kbit/s. One reconfiguration had occurred before the first snapshot; the
  capture does not contain its preceding contract or establish why Home chose
  the new one. Source/output rates must not be inferred from requested FPS.
- A completed video AU took up to 552.481 ms to send; source-time sent IDR
  spacing reached 5.613 seconds. The delivery/recovery and timing slices remain
  warranted. The frame-cadence counterexample from research is not established
  as this session's dominant problem.
- Service cgroup CPU increased by about 0.613 core-seconds per elapsed second;
  final memory was about 181 MiB. This is not proof that the host could never
  stall. Status collection took up to 2.526 seconds, which is separate from
  measured source-loop/lock timing and should be considered observer overhead.

The summary JSON contains only bounded numeric media/diagnostic properties.
Full snapshots and packaging/deployment receipts remain in local test evidence.
Audio recovery remains the first functional repair. Do not widen receipt-age
limits, force video quality, add filtering or infer fault from loss counters.

The owner subsequently confirmed the diagnostic build still plays both video
and audio for about two seconds, then pauses both for about one second,
repeatedly. This is a failed physical baseline. No functional repair had yet
been deployed. Joint pauses require a shared-path/timing review; the metadata
does not justify treating a connection that stays open as successful playback.
