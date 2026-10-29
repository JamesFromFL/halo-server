# Mains snapshot cache-miss polling repair — R7d

Status: isolated implementation and focused qualification complete; physical success is unverified. This change addresses the prematurely short polling window shown by a reproducible counterexample. It does not establish why the actual Ring timestamp failed to advance.

## Observed failure

R7b recorded a snapshot warning at `2026-10-29T07:56:35Z`: `phase=fresh`, `stage=timestamps`, `reason=source_empty`, HTTP 200, no exception, approximately 2.4 seconds elapsed, `fresh_requested=false`. This was before live video began, not the pending snapshot at the capture's final instant. R4d had the same failure shape.

The exact path is an initial cached-image GET returning 404, followed by the existing mains-only fresh fallback. Pinned `ring-doorbell==0.9.14` exposes public `async_get_snapshot(retries=3, delay=1, filename=None)`. Halo passed `retries=2`. The SDK sends one timestamp-trigger POST, then sleeps and polls once per second. It only requests the image when a returned timestamp is later than its locally recorded request wall time. Otherwise it returns `None` after the configured polls.

Therefore this warning says **no newer timestamp was observed in two polls**. It does not say that an image GET returned an empty JPEG. It also does not distinguish delayed camera preparation from a disabled source, stale upstream timestamp, or a clock discrepancy. Those possibilities remain unverified; the change does not relax the SDK's timestamp comparison or label an older image fresh.

## Narrow implementation

Only the existing cached-404 recovery uses `MISSING_CACHE_POLL_ATTEMPTS=5`. It passes the supported public SDK `retries` argument, keeping its one-second delay and one trigger. The explicit-fresh path remains unchanged at two polls.

The recovery still requires the reviewed mains-only camera kind, positive video capability, and explicit no-battery capability. Battery-capable, mixed, unknown devices and other HTTP failures do not gain automatic refresh. There are no new endpoints, camera streams, tasks, SDK patches, filters, or backend changes.

The original eight-second whole-operation timeout remains the outer limit. Time spent on the initial cache miss, token refresh, trigger, polling and image body all counts against it. Up to five polls are possible; the deadline may cut that short. The shared 8 MiB image budget and 256 KiB metadata budget are unchanged, as are admission before every HTTP request, final authority checks, two-request capacity, cancellation, and close/join ownership. A large sequence of timestamp responses can exhaust the metadata budget before the fifth poll and must fail closed.

## Evidence and tests

A new test runs through actual pinned SDK `RingDoorBell.async_get_snapshot`, `Ring/Auth`, and the bounded HTTP facade, with synthetic responses only. The first two polls are old; the third becomes new and a valid image is then returned. Against the unchanged baseline, that test fails with the same fixed `source_empty/timestamps/200` shape. The candidate succeeds with exactly one trigger.

Additional regressions cover the existing whole-operation timeout during additional polling, admission revoked before a third poll, close/cancel/join while an additional response is blocked, and bounded refusal after all five timestamps stay old. Existing tests cover battery exclusion, non-404 errors, HTTPS validation, body limits, shared budgets and authority transitions.

Focused qualification: **158 passed** across Ring snapshot, polling, HomeKit snapshot manager, broker and native JPEG codec tests. Ruff check/format passed for all changed Python files. The initial unprivileged run had six localhost-bind failures; the authorized localhost-only rerun passed. No real Ring account, device, external endpoint, server or service was accessed.

## Separate capture-summary correction

`clean_stop_resources=false` in the R7b generic summary is an all-resource final-instant result. It is not a live-stream cleanup failure. Live video, upstream and bulk resources were zero for 25 consecutive rows after STOP; a new snapshot resource appeared only in the last row. The final snapshot's completion lies outside that capture. Collector classification should distinguish clean live retirement from an independent snapshot still pending; it must not silently turn the all-resource final-instant result into true. This is a work-only collector concern, separate from this product patch.
