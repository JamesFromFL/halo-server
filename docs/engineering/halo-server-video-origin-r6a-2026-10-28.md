# R6a live admission: original video receipt and generation

A native decoder may release an older picture while processing a newer input.
The live manager previously attributed every output from that feed to the
triggering input's receipt. That could make an old picture appear fresh and use
the wrong receipt for estimated presentation. The source timestamp alone does
not establish when that picture was originally received.

The adapter now accepts the original source `received_at` along with its capture
epoch and returns immutable per-output provenance: `received_at`, `capture_epoch`
and `configuration_generation`. The separately owned adapter implementation
keeps a bounded metadata-only origin ledger and refuses missing origins rather
than substituting a newer receipt. Legacy direct codec callers may omit receipt;
the live production path may not.

Before conversion, the manager passes the actual packet receipt/epoch and
captures the adapter's configuration generation under its existing codec lock.
After the native await it checks the current source epoch, then validates the
whole output batch before admitting any datagram. Every unit must carry the
same exact integer source epoch and configuration generation plus a finite,
nonnegative, nonfuture original receipt. Missing, unknown, wrong-generation or
invalid metadata fails the owned view with a fixed error and normal joined
cleanup. It does not obtain a new source or refresh the timestamp.

Freshness, source-age metrics, presentation estimation and the R5 AU admission
all use each output's own receipt. The copy-to-native retry passes the original
input receipt again and validates all returned origins as well. Actual source
epoch changes still fence old pending output before it can be admitted; output
configuration changes do not turn old output into a current result.

The existing callback fixtures now implement this same adapter contract. A real
native B-frame regression demonstrates that a later input releases a picture
with its earlier receipt, the manager rejects that stale picture, and a fresh
actual IDR resumes independently decodable SRTP output. Other gates cover a
mixed-age batch, exact presentation receipt, whole-batch refusal of missing or
malformed metadata, configuration replacement and copy-to-native retry cleanup.
The prior source-epoch-during-worker regression remains active.

This slice changes neither the source receipt timestamps nor the500 ms freshness
limit. It does not implement a new A/V clock mapper, add playout delay, modify
Home's selected video quality, or fix the measured upstream quiet periods.
Exact provenance is a prerequisite for truthful timing work, not proof of Ring
or Home playback parity. The software-deadline and physical-delivery limitations
of R5 still apply. The original R5 cached-repair/feedback-reader follow-up remains
separate from this provenance change.
