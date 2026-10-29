# Halo Server: fresh full-session capture

Captured October 28, 2026, 9:30:26–9:34:12 p.m. EDT (October 29, 01:30:26–01:34:12 UTC).

The complete requested lifecycle was collected on the installed R3 build, commit `4adb0c2181d362f5c52237f21837e3bc7b3cef95`. Product implementation and deployment remained paused. The collection saved diagnostic metadata only; no camera images, audio or video were recorded.

| Phase | Observation |
| --- | --- |
| Closed baseline | Approximately 46 seconds; zero live resources and upstream connections |
| Open | One live start at 9:31:12.929 p.m.; first-frame event about 1.8 seconds after preparation |
| Running | Approximately 2 minutes 33 seconds; one continuing live session |
| Close | Explicit stop-request event at 9:33:45.622 p.m. |
| Cleanup | Approximately 26 additional seconds; live resources and upstream connections returned to zero |

There were 226 one-second status samples, no collection errors, no service restarts and no live or snapshot cleanup failures. The longest status request took 113 ms; the longest sample spacing was 1.064 seconds. Matching journal extraction completed without truncation.

The repeating interruption is measurable before HomeKit delivery. At Halo's routed, decrypted RTP receiver entry, audio had 68 gaps exceeding 500 ms and video had 71, through the last active sample. Maximum gaps were about 1.33 and 1.40 seconds respectively. These are application receiver observations, not a capture of packets arriving at the network card.

Combining the completed 100 ms receiver bins shows 62 periods of at least 500 ms with neither audio nor video packets observed, after excluding the first three seconds. Their median duration was 600 ms; their median start-to-start spacing was two seconds. The longest simultaneous quiet period was 1.2 seconds. This provides direct evidence consistent with the reported repeating picture-and-sound pauses.

The 100 ms receiver timer's maximum measured delay was only 14.84 ms. The observed incoming gaps therefore do not coincide with comparable stalls of that monitored event loop. This does not identify Ring, the LAN, the host's networking, or earlier receive/decryption processing as the root cause. Further attribution must preserve that distinction.

Halo's outgoing audio also loses substantial continuity under this pattern. Final session counters show 150.48 seconds of source audio read, 103.16 seconds submitted for sending, and 45.34 seconds discarded after output became stale. Thus only about 68.6% of the read source duration was sent. The final counters include 69 audio adapter recreations. Local send counts do not prove what Apple decoded or played, but they demonstrate an additional delivery problem inside Halo.

Video started with a negotiated 1280×720, nominal 30 fps, 299 kbps configuration. By the sample at 9:31:18.999 p.m., the selected configuration had changed to 640×360, nominal 30 fps, 132 kbps; the session recorded one reconfiguration. The final active sample showed 161 stale output video units and a maximum access-unit send time of approximately 551 ms. The new capture therefore preserves both an incoming timing problem and outgoing timing/quality changes; it should not be reduced to a single assumed cause.

Seven snapshot failure warnings were captured, all carrying the generic code `snapshot-source-failed`: four before the live session and three after it closed. They demonstrate that the snapshot issue also occurs without an active live view. Snapshot warnings are rate limited, some early failures and successful requests are not logged, and this code does not identify the underlying exception. Seven is the number of observed warnings, not a complete count of failed requests.

The live session ended normally with `stop_request`, not a timeout or service restart. HKSV warm and capture resource counts remained zero. These successful lifecycle observations do not establish smooth playback; this remains a failed media-quality test.

The working evidence directory contains the metadata timeline, sanitized journal events, reproducible summary script and derived summary with SHA-256 hashes:

`work/halo-server-stream-repairs-2026-10-28/evidence/lifecycle/`

- `lifecycle-20261029T013026Z.jsonl`
- `lifecycle-events-20261029T013026Z.json`
- `lifecycle-summary-20261029T013026Z.json`
- `summarize.py`

No fixes, commits, pushes or service changes were made during this collection. Further implementation remains paused.
