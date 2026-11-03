# Bounded H.264 receive assembly

The synthetic receiver baseline exposed two local assembly effects: aiortc's
generic jitter buffer waits for a following RTP timestamp to release a video AU,
and one missing fragment can hold all later frames until its 512-slot window
advances. This change addresses those receiver effects. It does not explain or
repair upstream UDP gaps, and it is not physical HomeKit playback acceptance.

`VideoJitterBuffer` selects the new H.264 assembler only after aiortc's actual
codec lookup, RTX unwrapping, NACK/statistics and depayloading. Other video codecs
retain the generic 512-packet jitter buffer. Audio and the pinned RTP-handler
signature/body remain unchanged, preserving encoded-frame and metrics contracts.

Contiguous single-NAL, STAP-A and FU-A packets can complete an AU at its RTP marker.
FU headers, start/end pairing, NAL type consistency and sequence continuity are
checked before the depayloaded bytes are delivered. A contiguous timestamp
transition is still an end boundary when the marker is absent. This follows
[RFC 6184 section 5.1](https://www.rfc-editor.org/rfc/rfc6184.html#section-5.1),
which identifies marker as an early end indication but cautions against relying
on it alone, and the consecutive-fragment rule in
[section 5.8](https://www.rfc-editor.org/rfc/rfc6184.html#section-5.8).

The pending buffer is bounded to 512 packets and 1 MiB of retained RTP payload
plus depayloaded data. A known sequence hole or incomplete prefix starts a 100 ms
reorder grace. Ordered fragments of a large AU do not start that short timeout.
A separate one-second total AU assembly limit bounds pathological contiguous
input that never completes. This is a finite stale-picture limit, not a delay
added to healthy pictures. A third pending timestamp group or packet/span/byte
bounds also retire unresolved assembly. Time expiry is checked on arrivals,
before a late repair is admitted; idle reception allocates no timer and cannot
grow memory. Healthy complete frames incur no fixed hold time. Retransmission
beyond an already-known hole's grace cannot reopen that damaged picture.

Known damage requests PLI and suppresses dependent pictures until a complete IDR.
If recovery continues, arrivals can request another PLI after a one-second
cooldown, so a lost request need not stall recovery forever. Startup and recovery
also require the first VCL slice to have `first_mb_in_slice=0`; this deliberately
fails closed for arbitrary-slice-order startup, partitioned slices and unsupported
interleaved H.264 packetization. Established contiguous pictures do not acquire
that additional first-slice restriction. A completed IDR provides a timestamp
epoch fence against late packets from the previous GOP, including after a same-
SSRC sequence restart. Sequence and timestamp wrap use modulo arithmetic.

“Complete” here means a structurally complete picture/NAL sequence from the
observed RTP span. When joining or recovering without a previously observed
start boundary, SPS/PPS packets preceding the first observed picture slice may
be missing. Existing downstream parameter-set validation remains authoritative;
this assembler does not invent configuration or bypass decoder compatibility.
No partial FU or known-missing picture slice is promoted to a complete picture.

The pinned aiortc jitter API returns one AU per incoming packet. A repair that
makes multiple buffered AUs ready drains them at one per subsequent arrival;
this bounded corner case has no additional flush timer. It should be considered
when interpreting a replay that ends immediately after such a repair.

`video_assembly` diagnostics expose only finite counters, retained packet/byte
counts and the waiting-for-IDR flag. The session sanitizer rejects invalid values,
omits identities/media, expires stale observations after three seconds, and
clears them on retirement. Counters distinguish completed/discarded AUs, loss
episodes, recovery requests, reorder timeouts and whole-AU assembly timeouts;
a PLI request is not proof the camera received it.

Focused tests run the real pinned aiortc handler, including RTX unwrap and the
encoded queue. Independently decoded generated streams cover regular delivery,
large IDRs, reorder, wrap, delayed next timestamp and recovery at the next complete
IDR. Adversarial tests cover missing FU fragments, conflicting duplicates,
missing markers, startup mid-picture, resource limits, late repair, source/sequence
restart, PLI retry, and non-H.264 fallback. The comparison harness also preserves
the frozen prechange baseline and records the candidate assembler source hash.
