# Snapshot failure diagnosis

The complete R3 Home lifecycle contained seven `snapshot-source-failed` warnings,
including failures outside the live view. Existing logs contained no underlying
service exceptions. A snapshot failure therefore must be investigated separately
from audio/video playback cadence.

The confirmed observability defect was that `SnapshotReader.fetch` converted
expected SDK, HTTP, timeout and image-validation failures to `None` without a
diagnostic. The HomeKit layer then exposed one generic `source-failed` code.
The actual Ring snapshot failure remains unconfirmed.

The later complete R4b pilot confirmed four cached-image HTTP404 failures.
The bounded mains-only recovery and its remaining live qualification are
documented in [snapshot cache-miss recovery](snapshot-cache-miss-recovery-2026-10-29.md).

## Fixed operational diagnostic

Each fetch owns its diagnostic state in the existing per-operation ContextVar.
Failures can emit this fixed record, at most once per client per five seconds:

```text
Ring snapshot result phase=cached stage=image reason=cached_missing http_status=404 error_class=ClientResponseError fresh_requested=False elapsed_ms=42
```

Only fixed phase, stage, reason and exception-class values, a bounded HTTP status,
a boolean and a bounded elapsed duration are emitted. There are no camera or
account identifiers, URLs, tokens, headers, images, raw exception messages or
tracebacks. A failed logging handler cannot alter snapshot results or cleanup.
Warning counts remain lower bounds because of throttling. Successful operations
are quiet. Cached404 means that request found no cached image; it does not prove
that the camera is permanently unsupported.

The diagnostic distinguishes cached404,401,403,429, rejected redirects, generic
HTTP failures, timeout/network/TLS failures, encoding/size/budget rejection,
invalid JPEG and retired/capacity/closed admission. A401 retains the original
image response's provenance even if the SDK refreshes OAuth and receives200.
Concurrent requests cannot exchange failure reasons or HTTP status.

The existing bytes-or-None API, deadlines, byte caps, admission checks, token
refresh/persistence, TLS verification, no-redirect rule and cached fallback are
preserved. No endpoint, Ring backend, HomeKit graph or privacy setting changes.

## Qualification and next step

The pinned SDK/Auth and synthetic HTTPS tests pass55 cases. Added regressions
exercise exact HTTP distinctions,401 refresh provenance, parallel operations,
timeouts, invalid image/encoding/length, retired/capacity admission, secret-free
metadata, throttling and failed diagnostic handlers. No real account/device was
accessed and no media was saved during these tests.

After the qualified build is intentionally installed, the next Home snapshot
request can supply the missing fixed reason through the existing journal. The
bounded journal collector must explicitly allow this prefix and field schema;
do not collect arbitrary Ring or aiohttp error text.

Upstream issue527 reports unreliable old snapshot triggering, especially for
battery cameras; open PR528 proposes another endpoint and API. These are relevant
research leads, not proof that this mains-powered camera has the same fault.
The proposal has not been adopted. Confirm the real failure class before changing
endpoints, authentication scope or any security/time bound.
[Upstream issue527](https://github.com/python-ring-doorbell/python-ring-doorbell/issues/527),
[upstream PR528](https://github.com/python-ring-doorbell/python-ring-doorbell/pull/528).
