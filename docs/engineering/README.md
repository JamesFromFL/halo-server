# Halo Server engineering

[Non-camera implementation wave 2](noncamera-wave2-2026-11-14.md) records durable
account-bound bridge identities, import preferences, capacity handling, private
storage isolation and failure/recovery tests. Publication and pairing are next.

[Non-camera implementation wave 1](noncamera-wave1-2026-11-14.md) records the opt-in
read-only discovery/catalog, shared-auth ownership, backend command denial, owner
interface and qualification boundaries. HomeKit bridge publication remains pending.

[Non-camera Halo Bridge research and implementation plan](noncamera-bridge-plan-2026-11-14.md)
records the pair-once bridge topology, standalone camera preservation, device
capability matrix, Python provider reuse, isolated feasibility results, and eight
implementation slices. It is a researched plan, not deployed device support.

[Cross-project Ring comparison](ring-project-comparison-2026-11-14.md) records the
Scrypted, Homebridge, Home Assistant and ring-mqtt implementation/configuration
review and the resulting acceptance requirements.
[Subscription-free Alarm feasibility](ring-alarm-subscription-feasibility-2026-11-14.md)
separates current US restrictions, regional/legacy eligibility and an independent
local Z-Wave alarm alternative.

## HomeKit stream engineering

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

[Post-capture research and proposed repairs](postcapture-research-2026-11-07.md)
records the November 7 full-session analysis, decoder-preserving native experiment,
audio-retention model, receive-boundary measurement plan, and implementation gates.
It is research evidence, not a deployed fix or Apple playback acceptance.

[Fluid stream repair progress](fluid-stream-repairs-2026-11-07.md) records the
subsequent implementation, local qualification and remaining physical test gates.
