# HomeKit stream engineering

The [repair plan](halo-server-live-stream-repair-plan-2026-10-28.md) records the
research baseline and seven-slice implementation proposal approved by the owner
on October 28, 2026. The [real-device audit](halo-server-live-stream-audit-2026-10-28.md)
records the failing camera session; it remains a baseline rather than proof
that an installed release is reliable.

`stream-repair-research-2026-10-28/` contains focused plans and metadata-only
synthetic receipts. Reproductions become maintained regressions in their
implementation slice; machine-specific scratch programs are not runtime tools.
The original research changed no application source. Existing device-test fixes
were already committed through `6677086`. No real camera media, credentials or
paired state are included.

Implementation progress and verification are tracked in `stream-repair-progress.md`.

[Bounded stream diagnostics](bounded-stream-diagnostics.md) describes the opt-in
packet/media recorder and offline audio replay added for the next controlled test.
