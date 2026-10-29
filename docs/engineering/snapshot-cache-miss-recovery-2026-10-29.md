# Snapshot cache-miss recovery

The complete R4b pilot identified four cached Ring image HTTP 404 responses before
and after live viewing. Each fixed diagnostic reported `phase=cached`,
`stage=image`, `reason=cached_missing`, `fresh_requested=False`. These matched
four HomeKit snapshot failures. This confirms missing cached images for those
requests; it does not prove Ring snapshot capture itself is unavailable.

HomeKit periodic and unknown reasons continue to request the cache first. A
cached-image 404 now permits one existing SDK fresh-snapshot sequence only for a
reviewed mains-only hardware kind with explicit SDK video capability and an
explicit false battery capability. False or missing battery metadata alone is
insufficient. The immutable kind allowlist is shared with the existing recording
power policy. No kinds were added. Mixed-power, battery and unknown hardware
remain cache-only unless the caller already requested fresh capture.

The recovery uses pinned `async_get_snapshot(retries=2, delay=1)`: one timestamp
trigger, at most two bounded timestamp polls, and an image GET if Ring reports a
new timestamp. It stays inside the original eight-second operation and shared
image/metadata byte budgets and the same two-job concurrency limit. It creates
no media lease or WebRTC connection and changes no Ring endpoint, authentication
scope, TLS validation, redirect rule, privacy setting or HomeKit graph.

Current authority is checked before recovery, at each HTTP request and after
the fresh image completes. Cancellation and close retain the original job until
its response cleanup joins. A 401/403/429, timeout, invalid image, rejected
endpoint, byte-limit failure or any error other than an actual cached-image 404
does not enable automatic fresh capture. Recovery is never recursive. If the
fresh timestamp or image remains unavailable, the original request fails within
its existing budget. A recovered 404 is not logged as a final request failure;
an unsuccessful recovery reports only its final fixed failure provenance.

Retrieval time still does not claim a camera capture timestamp. The HomeKit
`fresh_requested` field continues to describe caller intent, so it remains false
for an automatic missing-cache recovery even though the Ring diagnostic phase
can become `fresh`.

Real SDK/Auth response fixtures cover cache-hit silence, exact 404 recovery,
other HTTP errors, battery/mixed/unknown models, contradictory SDK power
evidence, cancellation, authority revocation, shared deadline/byte limits,
invalid/absent fresh images and bounded retry exhaustion. Existing synthetic
HTTPS tests preserve the transport qualification. No account/device was accessed
and no real media was saved. A new Apple Home pilot is still required to confirm
that this camera now supplies its tile image.

The released SDK implementation is the source contract:
[python-ring-doorbell 0.9.14 snapshot method](https://github.com/python-ring-doorbell/python-ring-doorbell/blob/0.9.14/ring_doorbell/doorbot.py#L402-L429).
The separate upstream next-snapshot endpoint proposal remains unadopted.
