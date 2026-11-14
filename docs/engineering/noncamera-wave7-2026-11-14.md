# Non-camera wave 7: qualified Chime controls

Implemented locally after REST foundation commit `2634d7f`. Deployment, actual
Ring permission and physical-device acceptance remain separate gates. No real
sound test, setting change, HomeKit publication or pairing change was performed
during development.

## Scope and observed state

The exact legacy kinds `chime`, `chime_v2`, `chime_pro`, and `chime_pro_v2` can
receive volume (0–11), bounded snooze/clear, ding/motion sound-test requests and
automatic nightlight configuration. Controls require a unique positive REST ID,
complete inventory, literal ownership, a fresh online source, current account and
provider generations, and an explicit saved per-device grant. Newer kinds remain
visible without presumed write support. The existing authenticated transport is
shared with the monitor; this introduces no second Ring session.

Connectivity comes from `alerts.connection`; volume, snooze remaining seconds and
automatic nightlight are exposed only from their actual source fields. Absent or
malformed values remain unknown. Nightlight configures the ambient-light sensor;
it is not a report of whether the lamp is illuminated. These controls appear in
Halo's interface. There is no fabricated HomeKit sound switch or nightlight tile.

Settings changes require an independent full inventory read that started after
handoff. HTTP success alone does not confirm the physical state. An in-flight
older read cannot settle a newer operation, and snooze confirmation accounts for
elapsed time instead of accepting any positive timer. Per-device serialization,
generation/revision fences, captured admission leases, bounded deadlines and an
uncertain-outcome barrier prevent overlapping or replayed actions.

The monitor caps its ordinary inventory interval at 20 seconds when Chimes are
present, against a 30-second control freshness limit. Ring rate-limit backoff
takes priority over that interval. Slow or unavailable data disables writes; it
does not fabricate a disconnected/healthy measurement.

## Deliberate sound tests and interface

Normal settings permission and sound-test permission are separately disabled by
default. Saving permission is inert. Enabling requires recent owner authentication;
each sound test also requires recent authentication and a deliberate confirmation.
Volume, snooze and nightlight drafts apply only when the owner submits them.

Ring supplies no reliable playback-completion event. An accepted or possibly-sent
sound test therefore leaves a sound-active latch until the owner confirms that
the sound stopped. That acknowledgement is local and never sends another Ring
command. It remains available when the ordinary command gate is blocked by that
latch. The latch is scoped to the running provider; a daemon restart cannot prove
physical playback completion. There is no automatic retry or invented timer-based
completion. The interface distinguishes HTTP acceptance from audible playback.

Private atomic policy state lives at `state/devices/chime.json`, with strict
revision checks, bounded entries and mode 0600. Operation receipts are scoped and
bounded; duplicate requests join the original operation. Losing a browser request
does not cancel the owned operation or imply that Ring did not receive it.

Tone selection remains deferred: upstream requires a reboot and its full settings
lifecycle is not qualified. Newer Chime generations, separate tone volumes and
shared-user write roles require their own evidence rather than model fallthrough.

## Verification

The provider/shared REST/Alarm/Beams regression passed **358 tests**. The Chime
coordinator, authenticated web API and monitor checks passed **28 tests**.
Frontend validation passed **51 unit tests**, type/format/build checks and four
browser suites, including ten Chime-specific checks. UI verification covers actual owner
DTO rendering, strict request validation, separate permissions, pending/uncertain
state, late responses across account changes, keyboard operation and six responsive
dark/light layouts. The prior Devices, Alarm and Light browser checks also pass.

Review added model/hardware/role/connection epoch fences, a known-field readback
marker, and a 3,600-second maximum Retry-After window shared by the REST transport.
A 900-second server delay is now preserved instead of truncated to 300 seconds.
Ruff and diff whitespace checks pass. Existing pinned SDK deprecation warnings
remain; none of these tests contacted Ring or published HomeKit accessories.

A post-commit composed review found that an explicitly acknowledged stopped sound
could leave the coordinator's separate lost-result barrier behind until the next
poll. The follow-up clears only a prior sound-test barrier on the exact confirmed
local acknowledgement receipt. Setting uncertainty and failed/ambiguous receipts
remain blocked. Eight regression cases cover that distinction; the combined
Chime coordinator, web and provider suite passed **74 tests** after this correction.

References: [project comparison](ring-project-comparison-2026-11-14.md), the pinned
Homebridge `ring-chime.ts` contract, Halo GTK's Chime modules, and the independently
qualified [shared REST boundary](noncamera-wave6-2026-11-14.md). Fixture/browser
success does not establish physical compatibility or account entitlement.
