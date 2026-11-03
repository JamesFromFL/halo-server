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
