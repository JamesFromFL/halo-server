# Video output origins and retirement ownership

R6a gives each output access unit its original source receipt and fences late
native results after retirement. It does not change the native encoder, frame
rate policy, source clock mapping, or HomeKit negotiation.

## Source receipt contract

`AccessUnitAdapter.feed` accepts the additive optional `received_at` argument.
Before awaiting native work, the adapter records the original receipt against
the exact `(capture_epoch, extended_source_pts)` pair. Each `OutputAccessUnit`
returns that source epoch, the adapter's configuration generation, and the
matching original receipt. Delayed, reordered, or multiple outputs from one
native response cannot borrow the receipt of the input that released them.

All origins in a native response are resolved before any are removed. A repeated
unresolved origin preserves its earliest receipt; an unknown receipt remains
unknown. The ledger stores at most 64 metadata entries and no payloads. Old
entries left by cadence selection are evicted at that bound. If a subsequent
output refers to a missing or evicted origin, the adapter raises the fixed
`Video output source receipt is unavailable` error. It never substitutes the
latest receipt or presents an unproved output as fresh.

The capture epoch comes from the source caller. The configuration generation
starts at one and increases only after a successful change to a different
configuration. An epoch change does not itself change this generation. A reset
clears origins owned by the retired native context. Existing direct and
recording callers may omit `received_at`; their output receipt is explicitly
`None`, preserving the old calling convention without claiming qualified live
freshness. The live manager must require a finite receipt and the expected
epoch and configuration generation before publishing an output.

## Retirement contract

A lifecycle generation fences native awaits. Reset snapshots the exact worker,
awaits its retirement, and then checks both the generation and ownership before
clearing the pointer or changing state. Close marks the adapter retired before
awaiting worker cleanup. A delayed reset or feed therefore cannot create a new
worker after close, publish a late result, or erase another worker's ownership.
If cleanup fails, the exact worker remains reachable for a later retry.

The reproduced old race held the first worker close while another adapter close
completed. The old feed then created an unretired replacement worker; with a
serialized worker close, the concurrent close could also erase its pointer. The
regressions cover both orderings, epoch reset and reconfigure, late native
output, and failed cleanup followed by retry.

## Validation and limits

The focused suite exercises native B-frame reordering, a 60 fps source selected
at 30 fps for 170 frames, delayed multi-output batches, bounded ledger eviction,
invalid and duplicate origins, copy compatibility, and recording callers. The
source-time IDR deadline fixture now submits both contributing PTS values before
returning its delayed batch; its actual-IDR and deadline assertions are retained.

This ledger is a finite origin lookup, not a proof of a future encoder's maximum
buffering lifetime. Missing origins fail explicitly. Native transaction changes,
variable frame rate, shared audio/video clock selection, and preserving an
existing Home view after a reconfigure error remain separate work. In particular,
the public HAP implementation closes a session when its reconfigure callback
returns an error; this slice does not promise that an internal rejected change
can keep that Home view open.

No real Ring or Apple device was used for these checks. They establish software
ownership and metadata behavior; live quality still needs a controlled pilot.
