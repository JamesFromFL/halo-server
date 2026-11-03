# Synthetic receiver comparison

This offline diagnostic executes three distinct receive paths against identical,
deterministically generated H.264 RTP traces. It needs no Ring account, camera,
saved media, server deployment, or HomeKit pairing. It does not contact cameras.
Its only runtime network traffic is small diagnostic datagrams on `127.0.0.1`.

The default is fail closed: missing or changed reference source/dependencies are
reported as unavailable, the report is saved, and exit status is 2. No profile or
receiver fixture is substituted. `--allow-unavailable` permits an explicitly
partial report, also with exit status 2. Successful complete execution returns 0.

## Code actually executed

| Receiver | Executed code | Delivery boundary |
| --- | --- | --- |
| Current Halo | Working checkout `ring_media` patches and `install_encoded_frame_adapter`, pinned aiortc 1.15.0 `_handle_rtp_packet`, depayloader and jitter buffer | Encoded AU callback |
| Released Homebridge Ring | Real Ring 14.3.0 `WeriftPeerConnection` class, original release lockfile, Werift 0.22.4 receiver and track callbacks | `onVideoRtp` packet callback |
| Scrypted custom peer | Hash-verified `ret`, `setupAudioTranscevier`, and `setupVideoTransceiver` declarations extracted by the TypeScript AST from pinned `wrtc-to-rtsp.ts`; actual `webrtc-required-codecs.ts` exports; full pinned koush Werift receive implementation | Actual `sendTrack` call in the custom peer callback, with a diagnostic sink |

Scrypted's full plugin host, Ring fork signaling, RTSP framing and HomeKit sender
are **not executed**. The exact source declarations, line locations and hashes
are included in each result. Supplying the small surrounding host services avoids
starting account/plugin discovery. Debug NAL logging and the host's closed-state
wait are substituted; media code in these declarations is unmodified. The
Homebridge camera-source output/encryption layer is also outside this harness.

`profiles.json` pins distinct commits and hashes. Released Homebridge serializes
audio first, H.264 High `640029` plus RTX, BUNDLE disabled. The Scrypted source
serializes audio first, H.264 Baseline `42e01f`, no RTX, and inherited `max-compat`.
Halo serializes its actual current video-first patched codec offer. Offers are
recorded rather than reconstructed from a combined profile.

This is deliberately a **post-SRTP video receiver** comparison. There is no ICE
gathering, remote SDP answer, effective transport selection, encrypted transport,
audio conversion, video adaptation, production dependency/rejection policy,
HomeKit transmission, or playback acceptance test. The synthetic sender selects
H.264 mode 1 at payload type 96 with no transport-wide-CC extension. It does not
establish what a Ring camera would select. Configured STUN is never contacted.

The harness supplies negotiated codec/SSRC state directly to each real receiver.
Halo's decoder-thread readiness flag enables the genuine encoded queue callback
without starting a decoder thread; the downstream decoder is intentionally absent.
Werift's real remote track is installed and its real transceiver track event is
triggered. RTCP writes go to a local observation callback rather than a transport;
there is no simulated successful retransmission. These setup seams are explicit
and should not be confused with a complete WebRTC connection test.

## Prepare exact references

Use isolated checkouts outside the Halo repository. These commands are a separate,
network-enabled preparation step; the runner never installs or updates packages.
Use a Node 20, 22 or 24 runtime supported by Ring 14.3.0. The tested runtime is
Node 24.22.1. npm install scripts are disabled; no production service is started.

```bash
git clone https://github.com/dgreif/ring.git ring-release
git -C ring-release checkout 3bcc624acc35b6f97c391089a12bff582bb6ed4a
(cd ring-release && npm ci --ignore-scripts --no-audit --no-fund)
git clone https://github.com/koush/scrypted.git scrypted-source
git -C scrypted-source checkout 3488836bf8b1b13efbcfd1dfe7b6367af602f8a4
git clone https://github.com/koush/werift-webrtc.git scrypted-werift
git -C scrypted-werift checkout c66a9bde88bb042d21f571f96e2c4d5b06628cac
(cd scrypted-werift && npm ci --ignore-scripts --no-audit --no-fund)
```

No Scrypted submodule initialization or plugin installation is needed. Source
checkouts must remain clean at the pinned commits. Both original lockfiles are
hash checked. Every installed dependency file is fingerprinted into a tree digest
for the report; retain the original lockfiles alongside results. The TypeScript
loader/compiler comes from the pinned Scrypted fork lockfile even for Homebridge.
Do not replace its fork with an npm Werift version that has a similar version name.

Run from a Halo Python environment with the project's pinned aiortc/PyAV and
native FFmpeg dependencies:

```bash
python tools/receiver_comparison/run.py \
  --homebridge /absolute/path/ring-release \
  --scrypted /absolute/path/scrypted-source \
  --werift /absolute/path/scrypted-werift \
  --node /absolute/path/to/node \
  --output /absolute/path/receiver-comparison.json
```

`--cases regular large_idr` selects a subset. Each child has a 20-second default
deadline, configurable from 1 to 60 seconds. The parent kills the whole child
process group on timeout. Receivers close their peer, timers and UDP sockets
normally; a leaked timer prevents natural child exit and therefore fails the run.
The `cancel` scenario is early session closure after three AUs, not a signal
injected into arbitrary third-party code. The regression suite also exercises
forced timeout termination. `pytest tests/test_receiver_comparison.py` tests
generator, trace perturbations, failure guards, real Halo assembly and cleanup.

## Measurement interpretation

The libx264 generator creates 12 pictures in memory and verifies all 12 with an
independent PyAV H.264 decoder. No encoded media is saved. Regular, bursty, one
missing FU-A fragment, adjacent fragment reordering, >128-packet IDR, delayed next
timestamp, sequence/timestamp wrap, and early close traces share deterministic
source/trace hashes. Both traces and emitted AUs are bounded.

Arrival times are actual handler-entry times; the intended schedule is not
assumed to have been met. Halo AU hashes are checked against the generated
source by unique content hash. Each callback retains its raw aiortc mapped
timestamp; the oracle independently checks it against the clock origin inferred
from the first identifiable released AU. After initial loss, that origin can be
a later IDR rather than source AU 0. If the first callback is damaged, a later
valid callback can establish the origin, but the damaged callback never counts
as complete. Reports include the source origin AU and per-callback content and
timestamp validation. Repeated identical source payloads are resolved only after
a unique payload anchors the clock, and must still match the expected AU hash.
For packet references, an independent reassembler tracks all expected
sequence numbers, handles FU-A, and checks the resulting AU hash before reporting
complete-AU availability. Reassembly uses the known synthetic trace as an oracle;
it is instrumentation, not Scrypted/Homebridge buffering behavior. A complete
dependent AU after lost IDR data does not imply it can be decoded independently.

`forwarded_units` has an explicit unit: AU for Halo, RTP packet for the references.
Compare complete-AU availability separately. There is intentionally no extra
flushing packet: the legacy baseline retains its final AU, while the marker-aware
candidate can deliver it at the marker. Missing AUs are reported
as not complete; they are not relabeled as production-policy rejections.

The common UDP sink receives a four-byte diagnostic token per delivery callback,
not video packets. It measures callback-to-local-egress scheduling and confirms
receipt; it is not an implementation of HomeKit packetization, SRTP or bandwidth.
The reference socket has an explicit receive buffer to collect a whole synthetic
IDR burst on the same event loop, and its effective size is recorded. Do not use
that setting as a production buffering recommendation. Egress is the sender API
boundary/callback, and sink time is userspace receipt, not kernel wire timestamps.

Regular and large-IDR runs repeat with timestamp collection disabled and require
identical output digests. This checks timestamp instrumentation noninterference,
not absence of all observer overhead or equality of wall-clock timings. A stock
128-slot Halo jitter buffer is an explicit negative control; the actual current
Halo path retains its 512-slot setting. Timing values are machine/load dependent;
repeat relevant cases before treating small timing differences as findings.
