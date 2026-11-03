# Comparative media repairs — 2 November 2026

Implementation follows the fresh Halo/Scrypted/Homebridge audit. Baseline:
`fac9b1838fa7d3a9550599e911ce3ebff41f7fac`. The owner authorized implementation,
testing and local slice commits; no push is included.

## Boundaries

The last complete physical capture found recurring joint gaps at the incoming
UDP callback before frame assembly. None of the following local regressions
establishes that those gaps or actual Apple playback are fixed. Do not substitute
successful synthetic decoding or content counters for physical acceptance.

Retain raw environmental sound, current pairing and privacy boundaries. Add no
fixed playback reservoir. Preserve bounded leases, attempt ownership, process
cleanup, and genuine source timestamps. A replacement receiver requires an
actual differential result and its own migration decision.

## Slice 1: lifecycle and transport-only reconfiguration

The Ring SDK now uses its documented `keep_alive_timeout=None` option. Halo's
existing source attempt, bounded consumer leases, progress deadlines and joined
close already own its lifetime. A second independent watchdog must not silently
stop pings while those consumers remain authorized. This adds no detached
renewal task and changes no lease maximum.

The regression runs the actual SDK pinger with simulated time, real lease
coordination and receiver cleanup. Two overlapping consumers expire at 600 and
850 seconds respectively; pings continue past five minutes and the first lease,
then the receiver joins cancellation after the last consumer expires.

The media buffer retains one bounded, validated SPS/PPS configuration separately
from rolling picture history. New readers receive the current configuration with
a real retained IDR; contiguous readers can continue without an old boundary.
Configuration changes and capture retirement invalidate old state. Prefix and
packet history remain bounded. Real generated H.264 independently decodes through
32 seconds, including readers joining after the original headers expire.

An RTCP-interval-only update now preserves both adapter and manager video state.
Actual copy and native-conversion paths independently decode all eleven fixture
pictures; real UDP/SRTP delivery retains sequence/crypto, recovery and timeline
state. Genuine format, rate and MTU changes retain their reset behavior.

Combined slice gate: **298 passed, 1 skipped** across receiver, buffer, session,
capture epoch, controller, recording source, conversion, live UDP/SRTP and
reconfiguration tests. The skip is the existing unavailable host IPv6 case.
The test runtime uses explicit pytest-asyncio loading because an unrelated
installed pytest entry point is unavailable. Local socket permissions are needed
for actual loopback transports and thread wakeups. No full-suite or physical
acceptance claim is made by this focused gate.

## Slice 2: regular RTP control identity

Audio and video now send regular compound RTCP SR+SDES reports with one random,
view-scoped CNAME. It contains no account, device or network identity, is omitted
from diagnostics, and survives reconfiguration/source recovery within the view.
Existing RTP timestamps, sender counters and feedback accounting are unchanged.
The generic serializer retains its bare-SR utility form for callers that do not
supply CNAME; both production live report owners explicitly supply it.

Tests inspect the actual serialized compound boundaries, SDES padding and name
bounds, SRTCP authentication round trips, and both real audio/video report loops
over UDP. The combined report/timeline/live/audio/reconfiguration gate passed
**142 tests with one existing IPv6 skip**; lint/format checks passed. This closes
a generic RTP conformance gap and does not establish a stutter diagnosis—Scrypted
also emits bare reports in its reviewed native sender.

## Slice 3: allocate audio work for the configured consumers

Native HomeKit and Ring-only modes no longer allocate the legacy PCM decoder,
legacy audio owner or normalized-Opus encoder. Original Opus, source-clock
observations, native live/recording audio and the intercom microphone remain
connected. Compatibility and dual modes retain the existing legacy lane. This
choice follows the immutable server mode and applies to both initial capture and
retry; it does not dynamically change a running capture's consumer policy.

The focused gate passed **150 tests**, including all four modes, retries, the
actual Ring receiver's encoded adapter, independently decoded original Opus,
recording demand, and intercom PCM. Ruff and whitespace checks passed. An
additional composed/admin check initially encountered three missing-adapter-build
failures; these must be rerun after the HAP adapter is built. Avoid claiming an
observed CPU or physical-playback improvement from this allocation change alone.

## Slice 4: HAP 2.3.0 with retained Halo guarantees

The vendored dependency is now `2.3.0-halo.1`, rebased on official HAP 2.3.0
commit `bd7a01b889cf1c74cbfcc4a4e5efdfa53fc3532b`. It retains Halo's bounded
writer/admission, physical-close ownership, HKSV acknowledgement fences and
durable pairing-storage/fatal-intent APIs. Upstream inbound validation, encrypted
control-frame fixes and ESM support remain intact. Native Node pairing crypto is
an upstream change; an installed-artifact regression signs with a legacy stored
seed/public-key fixture without rewriting it.

Two clean builds are byte-identical, all 283 installed files match the archive,
and lock integrity is verified. Both isolated and current-repository adapter
validation passed **268 tests**, with zero guarded network attempts, plus build
and typecheck. Upstream qualification passed **1,036 tests with seven failures**
from this host's unavailable IPv6 loopback; the supported-host subset passed
1,030 tests and a temporary IPv4 HTTP variant passed 13. The original IPv6 tests
remain in the published patch and the builder does not silently skip them.

Old vendored artifacts remain for provenance and rollback review. The complete
new patch, reproducibility hashes, toolchain, dependency delta and qualification
limits are recorded in `homekit-adapter/vendor/hap-nodejs-2.3.0-halo.1.provenance.json`.
Physical pairing/controller acceptance and power-cut durability remain untested.
This dependency maintenance is not evidence of correcting incoming media gaps.

## Slice 5: repeatable receiver comparison

`tools/receiver_comparison` now executes Halo's pinned real receive/encoded-queue
path, released Homebridge Ring's actual Werift peer, and hash-verified Scrypted
custom-peer declarations with its pinned Werift fork. Eight identical generated
H.264 traces cover regular/bursty delivery, loss, reordering, a 282-packet IDR,
delayed next timestamp, wrap and early close. The source independently decodes;
complete AU hashes, handler timing, callback scheduling and joined cleanup are
reported. Changed/missing references fail closed. No Ring credentials or saved
camera media are used. The harness tests passed as part of a **113-test** codec,
native-audio and comparison gate, including all advertised audio packet durations.

Frozen pre-assembly-change baseline: normal Halo delivery released 11 of 12 AUs,
with the last held for another timestamp; the references made all 12 available.
Delaying the next frame moved Halo's first complete AU to about 214 ms while
reference availability remained about 3–4 ms. One missing initial fragment held
all subsequent Halo AUs within the bounded trace. The reference packet paths
forwarded the remaining packets; complete dependent pictures after a lost IDR
are not necessarily decodable. The 128-packet negative control also demonstrated
truncation of the large IDR; Halo's existing 512-packet limit is retained.

This is post-SRTP **video receive-path** evidence, not live Ring negotiation,
audio, RTSP, output adaptation or HomeKit acceptance. It identifies a separate
local assembly defect; it does not explain away previously measured gaps before
assembly. Reference/profile hashes and the measurement limitations are in the
runner README and JSON reports. Baseline evidence is retained outside the repo
in the task's `outputs/halo-receiver-comparison-2026-11-02-baseline.json`.

## Slice 6: prompt complete-picture delivery and bounded recovery

The H.264-only assembler replaces the generic video jitter buffer at the existing
pinned aiortc boundary. It releases contiguous validated pictures at the RTP
marker, retains timestamp-transition fallback, and preserves aiortc codec lookup,
RTX, NACK, statistics and timestamp mapping. Other video codecs retain the generic
512-packet buffer; audio and negotiation are unchanged. The pending H.264 data
is limited to 512 packets and 1 MiB. Known holes have 100 ms repair grace; total
AU assembly has a separate one-second bound. Both expire before late repair is
admitted. Healthy ordered assembly can take longer than the reorder grace.

Loss requests an IDR, suppresses dependent pictures, and retries recovery with a
one-second cooldown while packets arrive. It adds no fixed playout reservoir.
Finite assembly/loss/timeout observations reach session status without payloads
or source identifiers. Details, supported packetization and recovery limits are
in [the assembler notes](h264-receiver-assembly.md).

Independent review caught two pre-release edges: completion could bypass expiry,
and a healthy large IDR could incorrectly hit the reorder timeout. A first repair
arriving ten seconds late is now rejected, while an actual 282-packet IDR paced
over 500 ms is delivered with its exact source hash. The receiver backreference
was removed to avoid adding a cycle that retains buffered media. The final
receiver/harness/diagnostic gate passed **135 tests**, with independent focused
review and Ruff/format checks passing.

A separate integration review found that session progress still used *new-reader*
readiness. An existing reader can continue after the last retained IDR expires.
The session now uses established decoder continuity for useful-video progress,
while cold/configuration/epoch/capacity failures remain gated. A 34-second
long-GOP regression independently decodes every picture through the real receiver
watchdog; reverting the change reproduces `video_stalled`. Its broader gate
passed **135 tests**. The recording-only lease test now explicitly retires live
continuity before checking recording authority; **35 recording/progress tests**
pass. The SDK activation fixture also now expects the owned, unbounded-by-SDK
watchdog option; its **13 tests** pass.

The comparative oracle was corrected to account for aiortc normalizing timestamps
to the first released frame after loss. Identity requires source-byte hashes and
independent timestamp-delta validation; duplicate fixture hashes are accepted
only after a unique frame anchors the mapping. The damaged 128-slot negative
control remains damaged rather than being relabeled successful.

## Pilot scope and remaining investigations

The media pilot will restore the complete pre-upgrade HAP adapter from immutable
commit `689d6b706832f0176138bc2029714b75bacba5e8` in an isolated source archive.
Its per-file source manifest and hash become part of the release identity. The
main source retains the separately qualified HAP 2.3 upgrade. This keeps unrelated
dependency changes out of the first physical media experiment. No pairing reset,
HKSV enablement, automatic backend replacement or noise processing is included.

Do not yet change late-SR clock mapping, the copy/conversion bitrate policy,
recording-buffer demand transitions or multicamera worker admission. The clock
limitation and rate-window counterexamples remain documented; speculative
correction could add latency, timestamp jumps, extra conversion or frame loss.
The new receiver comparison found a concrete local defect without establishing
that a different Ring negotiation/transport would remove incoming gaps. Serial
physical Ring/Apple or reference-peer comparison remains necessary if those gaps
persist. Software completion is distinct from acceptance of smooth Apple playback.
