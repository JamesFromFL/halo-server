# Web interface implementation and verification

The approved GTK-style owner console is implemented in the Halo Server checkout.
It uses the daemon's existing owner operations through a separate HTTPS process.
The live seven-camera server was not modified during implementation. No commit,
push, new Ring connection, or recording enablement was performed.

## Delivered

| Area | Implemented behavior |
| --- | --- |
| Interface | Overview, Devices, Integrations, Activity, Diagnostics, Settings; adaptive desktop/phone navigation; system/light/dark appearance. |
| Administration | Existing Ring session, session-bound MFA connection, HomeKit selection, protected PIN/QR reveal, per-camera reset/resume, light/siren controls, warm-source preference, saved/applied configuration and CAS conflict recovery. |
| Browser access | Explicit local setup/recovery; Argon2id password verifier; private HTTPS certificate; opaque secure cookies; session revocation; recent-auth checks; exact Host/Origin and CSRF enforcement. |
| Diagnostics | Allowlisted doctor results and support download; daemon-down access; durable operation outcomes; fixed managed-daemon restart. |
| Ownership | Shared validated CLI/TUI/web client; compact account-fenced state; bounded recent activity; one shared state reader; separate companion health and lifetime ownership. |
| Lifecycle | Both-unit activation/rollback/uninstall; independent enabled/active state; suppression of incompatible pre-web startup; preserved startup intent; fixed restart with no uncertain replay. |
| Packaging | Offline static assets, pinned frontend lockfile, deterministic source/asset manifest, dependency notices, build-time freshness checks, runtime static allowlist, installed-wheel browser CI. |

The console does not automatically request snapshots or streams. Recording
preferences remain separate from warm-source opt-in and Apple Home recording
choices. Discovery, selection, publication, pairing, and source availability are
distinct observations; missing evidence stays unknown.

## Changes made during review

- Ring login/logout now optionally carry an observed account epoch. The daemon
  rechecks it under its account-change lock, so a queued browser action cannot
  mutate a successor account.
- Activity drops retired-account camera targets. It records uncertain storage or
  cleanup failures as unknown rather than claiming the command was rejected.
- Local web configuration shares the release guard. The companion additionally
  holds an atomic, noninheritable exclusive lock for its state, preventing two
  simultaneous startups from reconciling the same mutation journal.
- Disconnected HTTP requests cannot cancel already-owned writes or free a KDF
  work slot early. Authentication reloads are coalesced and revision fenced.
- Pairing caches include account, adapter, and registry authority. PIN reveal is
  concealed after navigation/background/logout/authority changes; late results
  cannot enter a different camera's panel.
- Reserved daemon capacity keeps normal detail reads from making state polling
  report a false outage. Passive SSE does not extend the authentication idle timer.
- Certificate information distinguishes the serving certificate from newly saved
  settings. Changing the configured name needs explicit renewal or trusted import.
- Maintenance that cannot drain stays visibly paused and preserves pending
  work. It leaves both units and the release selector unchanged. Recovery is a
  later release retry, or local companion restart after pending work reaches zero;
  there is no automatic restart that could interrupt a possibly-sent write.
- Warm-source capacity comes from the actual recording owner and counts reserved
  slots, including acquisition/cleanup. The UI does not invent live usage.
- Real browser tests corrected settings-conflict messaging and keyboard focus
  restoration after closing the adaptive navigation drawer.

## Verification

The full Python regression suite passed **2,965 tests with one skip**. The skip is
the existing IPv6-loopback live-transport test: this host lacks IPv6 loopback.
The suite used the pinned Node runtime and the existing isolated GStreamer RTSP
overlay; no system packages were changed. Latest web/security/ownership changes
were checked again after the full suite's collection: the final focused run
passed **149 tests with the same IPv6 skip**. Ruff, formatting, and whitespace
checks pass.

Frontend strict checking reports zero errors/warnings, and all **14** API/model
tests pass. Production rebuilds produce identical browser manifests.

The real HTTPS browser harness uses the actual daemon administration boundary
with seven synthetic cameras. It checks all six pages in both themes at 320,
390, 768, 1024, and 1440 CSS pixels: **60 combinations**. Additional checks cover
secure enrollment/logout, device search and history, light on/off, confirmation
cancellation and focus, saved-configuration conflicts/draft preservation/restore,
200% CSS zoom, reduced motion, and drawer Escape behavior. It verifies no browser
errors, CSP violations, auth material in web storage, or media requests. Backend
counts show only the two explicit light commands, zero snapshots, and zero media
sessions. Desktop and phone screenshots were also visually reviewed.

Two offline builds produced byte-identical wheels and source distributions. The same browser harness is
run from an isolated environment containing the installed wheel, with all runtime
dependencies installed offline from the hashed lock export. Assets are validated
from the installed package, not served from the source checkout.

Evidence for this implementation is retained outside the repository under
`/home/james/Documents/Codex/2026-10-25/he/work/halo-server-web-implementation-2026-11-13/`.
The `evidence/browser-source/` and `evidence/browser-installed-final/` directories hold
synthetic screenshots and reports. Temporary enrollment credentials are never
included in the browser evidence or package.

## Remaining deployment acceptance

Slices 1–6 are implemented. Slice 7 has local source and installed-package
qualification; the final server-target immutable release still needs its existing
sealed-release/target-runtime gate. Slice 8 remains a controlled deployment and
physical-device pilot, not a synthetic test claim.

Before enabling the companion on the server: preserve the current release and
pairing/configuration hashes, qualify the exact target artifact, configure the
explicit LAN address, establish certificate trust through the owner terminal,
and enroll the Halo web password directly in the browser. Then verify Firefox
and physical iPhone Safari, all seven existing pairings/publications, snapshots
and live viewing under several dashboard tabs, independent service restart,
reboot recovery, and phone background/reconnect. HKSV remains off unless separately
requested and qualified.

The first companion implementation is Linux-qualified. It does not claim macOS
TLS compatibility, ARM qualification, browser video playback, sensors, chimes,
alarms, or additional ecosystems. The recent activity feed is bounded session
history, not a durable audit. Mutation/restart journals deliberately stop accepting
new IDs at their bounds rather than evicting uncertain outcomes and risking replay.
