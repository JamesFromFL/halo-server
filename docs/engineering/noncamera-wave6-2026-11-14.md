# Non-camera wave 6: bounded shared-session REST writes

Date: November 14, 2026. Implemented locally after lighting commit `cd0b51c`.
This transport foundation does not enable any device or change a saved permission.
No live Ring command, deployment, pairing change or push occurred.

## Contract

`RingDeviceRestGateway` borrows the existing monitor gateway's authenticated
`RingJsonTransport`. It neither creates another Ring login/session nor closes
the shared transport when the feature retires. Typed methods cover legacy Chime
volume, ding/motion test requests, bounded snooze/clear, automatic nightlight
configuration, and finite lighting-group requests. Reads cover inventory,
health/settings and groups. Tone selection is deferred because upstream changes
require a reboot and the full setting lifecycle is not qualified.

The transport independently validates the complete HTTPS host, route, method and
minimal payload. There is no arbitrary settings or actuator escape hatch. Numeric
booleans, foreign hosts, extra fields, malformed/ambiguous identifiers and unknown
routes are rejected before I/O. Every write requires current synchronous admission.
The caller's payload is copied before queueing and the final admission is checked
after shared authentication preparation and immediately before the HTTP handler.

The total deadline includes queueing, token preparation and bounded response
reading. Token refresh may occur before handoff through the existing serialized
refresh/persistence coordinator. A response-side 401, redirect, network failure or
timeout never causes a submitted write to be retried. Explicit rejection stays
distinct from an uncertain possibly-sent operation. Cancellation carries the
handoff metadata to its owner. A bounded complete 2xx reply means HTTP acceptance,
not physical device state; the next feature layer must obtain actual readback.

## Hidden retry found during review

The installed aiohttp client retries an idempotent PUT on a failed persistent
connection. That would violate Halo's no-replay contract even with no retry in
Halo's own code. A per-request innermost middleware now permits only one handler
invocation. It preserves other session middleware and does not mutate the shared
session's global retry policy. A localhost regression using real aiohttp first
demonstrated two unguarded PUT submissions, then verified one guarded submission
and an uncertain receipt after the same response disconnect.

## Verification

The final dedicated REST suite passed **110 tests**, including the real-client
retry regression. The broader shared-client, authentication, snapshot, hub and
provider regression passed **411 tests** before the final dedicated rerun.
Existing Ring SDK BasicAuth deprecation warnings remain. Detailed results are retained in
`/home/james/Documents/Codex/2026-10-25/he/work/halo-server-noncamera-wave6-2026-11-14/`.

Coverage includes exact route/body rejection, caller mutation, queue capacity and
recovery priority, deadlines before and after handoff, cancellation, ownership
revocation after token refresh, durable refresh-before-write, invalid/oversized
replies, feature close versus shared core ownership and sanitized failure receipts.
Fixtures and localhost requests establish transport behavior, not Ring permissions
or physical compatibility. Chime and group providers will add their own model,
ownership, source-freshness and confirmation gates in subsequent waves.

Sources: the existing pinned [project comparison](ring-project-comparison-2026-11-14.md),
Halo GTK `chime_rest.py` / `light_groups_rest.py`, and the installed aiohttp request
loop inspected and exercised by the regression test. Upstream bodies are behavior
references; the new transport retains Halo's stricter admission and uncertainty
rules.
