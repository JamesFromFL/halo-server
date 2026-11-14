# Halo Server web interface: design and implementation plan

Date: November 13, 2026 (America/New_York). Plan approved. The browser interface
and backend are implemented locally; final qualification continued November 14.
See [implementation and verification](web-interface-implementation-2026-11-14.md)
for results and the remaining live-server acceptance gates. The planning notes
below retain the original design rationale.

## Product decision

Build a browser-based owner console with the recognizable look and behavior of
Halo GTK. It should make normal setup, device selection, HomeKit pairing,
configuration, and troubleshooting possible from a desktop or phone. The first
release manages the existing camera implementation. Its presentation should
accommodate sensors, chimes, lights, and additional integrations when their
backend support is implemented.

Use the existing daemon as the sole Ring session and device authority. Add a
separate web companion so a failed/stopped daemon can be explained and restarted
through the interface. This requires explicit multi-service release work; it is
not merely a frontend placed over today's Unix socket.

The plan adds a browser frontend and management transport. It does not replace
the Ring, media, HomeKit, credential, or pairing backends. Existing CLI/TUI access
remains useful for recovery. Existing snapshot repair work in the checkout is
outside this plan and must be preserved.

## Halo GTK continuity

The reference is the current local Halo GTK code, supported by its screenshots.
Some screenshots contain obsolete feature descriptions; only their appearance
is used as a reference.

| Element | Web design |
| --- | --- |
| Branding | Reuse the Halo lens artwork and wordmark, with a secondary **Server** label. Review and retain artwork provenance when copying assets. |
| Application shell | Full-height tinted left navigation, 218–248 CSS px on desktop; Settings anchored at the bottom. |
| Header | Compact centered page title, navigation/back on the left, contextual controls on the right. No imitation OS minimize/close buttons. |
| Surfaces | Quiet neutral window and sidebar colors, slightly raised cards, thin separators, shallow shadows. |
| Geometry | 8 px corners on navigation selections and cards; 24/28/36 px desktop content padding; 16 px horizontal padding on phones. |
| Settings | Grouped preference rows with title, optional explanatory subtitle, and one primary trailing control. |
| Accent | Restrained blue default; tinted selected row and accent icon, rather than large saturated panels. |
| Typography | Local `"Adwaita Sans", "Cantarell", system-ui, sans-serif`; 14–16 px body text, scalable browser zoom, restrained headings. |
| Icons | Small monochrome symbolic-style icons. Bundle a bounded, licensed set; GTK system icon names are not browser assets. |
| Feedback | Connection banners below the header, inline errors, pending state beside an action, short success toasts. Important errors persist until resolved. |
| Appearance | Follow system light/dark by default, with System/Light/Dark override per browser. Explicit CSS tokens reproduce the family resemblance; arbitrary installed GTK themes cannot be inherited by a web page. |

Initial theme values should be close to the actual Adwaita references: light
window `#fafafa`, sidebar `#eeeeee`, cards `#ffffff`; dark window `#242424`,
sidebar `#2e2e32`, cards `#353539`; blue suggested actions `#3584e4`. These are
proposed browser tokens, not a claim that Halo GTK hardcodes these colors.
Contrast and focus tests may require adjustments.

Below 900 px the permanent sidebar collapses; navigation becomes an accessible
drawer or full-page navigation on the smallest screens. Settings/device detail
becomes a single navigable page below 600 px. Preserve browser Back, keyboard
focus restoration, Escape dismissal, reduced-motion preferences, and 44 px
touch targets. Validate 320, 390, 768, 1024, and 1440 px widths, both themes, and
200% zoom. The phone layout must retain every management operation.

The patterns follow GNOME's [sidebars](https://developer.gnome.org/hig/patterns/nav/sidebars.html),
[header bars](https://developer.gnome.org/hig/patterns/containers/header-bars.html),
[boxed lists](https://developer.gnome.org/hig/patterns/containers/boxed-lists.html),
and [adaptive layout](https://developer.gnome.org/hig/guidelines/adaptive.html)
guidance, translated into semantic browser controls.

Local design references:

- [Halo GTK window and navigation](../../../halo-gtk/src/halo_gtk/window.py), especially `_NAV_ITEMS`, `_build_ui`, and the 900 px breakpoint.
- [Halo GTK style definitions](../../../halo-gtk/src/halo_gtk/ui_style.py).
- [Settings groups](../../../halo-gtk/src/halo_gtk/settings_page.py) and [adaptive settings window](../../../halo-gtk/src/halo_gtk/settings_window.py).
- [Dashboard reference](../../../halo-gtk/doc/images/halo-dashboard.webp) and [settings reference](../../../halo-gtk/doc/images/halo-settings.webp).

## Navigation and first-release capabilities

| Destination | Content and behavior |
| --- | --- |
| **Overview** | Daemon state, Ring account connection, event connection, HomeKit publication and pairing counts, issues needing attention, recent activity. Every observation has freshness; disconnection never leaves a stale green status. |
| **Devices** | Searchable device list; name, source, supported capabilities, observed state, and integration exposure. Current implementation lists cameras. Detail contains HomeKit access, pairing, supported light/siren controls, and recording information. |
| **Integrations** | HomeKit selection and pairing summary; current operating mode. Existing compatibility mode appears only when relevant. Additional ecosystem panels appear when implemented, not as working-looking placeholders. |
| **Activity** | Bounded recent motion, doorbell, connection, and owner-action activity with device/type filters. Explicitly label this as recent session activity; it is not a complete durable archive. |
| **Diagnostics** | Halo Doctor, readable component findings, sanitized support download, software/release identification, and daemon-down status. Advanced probes require a deliberate action. |
| **Settings** | Appearance, Ring account connection, Halo web password/sessions, saved/applied server settings, restart requirement, explicit managed-daemon restart, and local access/certificate information. |

The initial management dashboard uses device icons and state rows. It does not
automatically open streams or refresh seven thumbnails. There is currently no
owner snapshot endpoint, and another background media consumer could recreate
the workload just repaired. A later explicit still preview requires its own
scope/privacy checks, shared budgets, freshness label, cancellation, and battery
wake policy. Browser live monitoring and local media playback are outside v1.

Also outside v1: software installation/activation through the browser, general
file browsing, raw log download, remote internet exposure, user roles, cloud
accounts, alarm/sensor/chime implementation, and advanced media capture/download.
Existing CLI capture workflows continue to be available separately.

## Important interaction flows

### First access and Ring connection

1. The local installer/owner CLI enables the companion and creates an expiring,
   single-use setup code. An arbitrary first visitor cannot claim the server.
2. The owner opens the verified local HTTPS address and uses that code to create
   a Halo administrator password. Halo web authentication is separate from Ring.
3. The interface shows the existing Ring connection when present. It must not
   demand a new Ring login simply because the web companion was installed.
4. If connection is needed, a secure form submits Ring email/password. When Ring
   requests MFA, an expiring, session-bound challenge continues the operation.
   Credentials remain only in bounded transient memory and are cleared on
   completion, cancellation, expiry, logout, and companion shutdown. Never put
   credentials in browser storage, URLs, diagnostics, or journals.

The current login RPC accepts email/password/OTP together. The challenge wrapper
is new work, not an existing feature. Limit one active Ring login flow per owner
account and reject stale account epochs. Transport uncertainty must not silently
restart a login or send the same control again.

### Selecting and pairing devices

The UI distinguishes **discovered**, **source available/unknown**, **selected**,
**published**, **paired/unknown**, and **recording enabled**. They are different
facts. Publication does not prove a successful stream. Missing observations are
not represented as false.

A device's HomeKit toggle uses the latest account scope and registry revision.
Today's select operation replaces the whole selection; the facade must preserve
the other cameras and reject a stale revision instead of dropping their choices.

Reveal PIN/QR requires a deliberate action and recent Halo authentication. Render
QR locally from the returned setup URI. Conceal after 60 seconds, navigation,
disconnect, account/device/adapter change, deselection, reset, and logout. Hiding
a code does not rotate it. Reset requires confirmation naming the device, explains
the temporary adapter interruption, and resumes an existing pending reset by its
original operation ID. It never resets other camera pairings implicitly.

### Controls, settings, and recovery

Only verified capabilities expose controls. Ring actions show **pending**,
**accepted**, **rejected**, or **outcome unknown**; accepted does not imply an
observed physical state. Keep the existing 30-second siren bound. Do not retry
an uncertain siren, reset, or other mutation automatically.

Configuration forms retain their base revision and unsaved edits. Show saved and
running values separately, with a clear restart-required banner. A conflict
preserves the draft and offers reload/review. Do not silently overwrite newer
settings or restart merely because a setting was saved.

Recording UI keeps Apple Home's recording/audio preferences separate from Halo's
owner warm-recording opt-in. Show the existing mains-only eligibility evidence
and global warm-source capacity. Installing the UI does not enable recording.

An explicit **Restart Halo** operation affects only the managed daemon, with a
confirmation explaining temporary camera interruption. The separate companion
shows progress and verifies the expected daemon release/boot identity afterward.
Unsupported/custom installations show why that action is unavailable. A stopped
daemon does not prevent signing in to the web console or viewing local recovery
information.

## Architecture and technology choice

```text
Desktop / phone browser
  local static frontend + same-origin authenticated HTTPS
                         |
                  halo-server web
         web auth / typed routes / projections
         bounded operations / shared state feed
                         |
              private owner Unix socket
                         |
                existing Halo daemon
           Ring session / events / media / HomeKit

Independent release operator controls both service lifecycles.
```

Recommended frontend: **Svelte 5 + TypeScript**, built with **Vite**, with a small
Halo-specific component/style library. These are new frontend/build dependencies;
they do not replace the Python backend or HAP adapter. Pin exact compatible
versions and lockfiles during the first slice. Avoid a general UI component kit
whose default appearance would fight the GTK design. Use static deployment, no
SvelteKit server, no runtime frontend Node process, and no runtime CDN/font calls.

Svelte supports typed components and compiler accessibility diagnostics, but
those do not replace manual accessibility/browser tests. Vite can build static
assets suitable for the existing release artifact. Sources:
[Svelte TypeScript](https://svelte.dev/docs/svelte/typescript),
[Svelte compiler warnings](https://svelte.dev/docs/svelte/compiler-warnings),
[Vite production builds](https://vite.dev/guide/build).

Serve API/assets using the already-required **aiohttp** in a separate Python
process. Handler cancellation, task tracking, deadlines, and shutdown need
explicit operation ownership; a disconnected browser must not turn a possibly
sent write into an automatic retry. See
[aiohttp server lifecycle and cancellation](https://docs.aiohttp.org/en/stable/web_advanced.html).

The companion runs as the existing local owner and keeps Ring/media work in the
daemon. Process separation improves scheduling and recovery; it is not a security
boundary against arbitrary code execution under that same Unix account.

### Planned files and contracts

| Area | Placement / change |
| --- | --- |
| Frontend | `web-ui/`: pages, shared GTK-like components, theme tokens, typed API client, route/state handling, browser tests and exact lockfile. |
| Companion | `src/halo_server/web/`: `server`, `routes`, `auth`, `models`, `state`, `operations`, `settings`, and TLS/bootstrap helpers. |
| Shared owner client | Generalize `tui/client.py` into an owner-client module with compatibility imports. Retain its scope, pairing, pagination, and configuration validation. |
| Daemon projections | Add compact owner state/activity methods in `owner_admin.py` and small service projection helpers; keep current CLI/TUI methods stable. Add an `owner_activity.py` publisher at actual owner-operation/connection boundaries. |
| Web private state | Separate versioned web config, password verifier, TLS files, and operation journal under the existing private state root. Do not add unknown keys to today's strict daemon configuration schema. |
| Packaging/lifecycle | Include static assets and web entry point in immutable manifests; extend `release_service.py`, `release_activation.py`, installer, launcher validation, and installed tests for two units. |

Proposed browser namespace: `/api/web/v1`. Explicit endpoints cover session,
state/feed, devices, HomeKit selection/pairing, supported controls, warm preference,
settings, Ring connection/challenges, activity, doctor, support export, and daemon
restart/operation status. Final schemas are frozen in slice 1. There is no raw
RPC passthrough, arbitrary method name, server file path, command, unit name,
release URL, or unrestricted proxy endpoint.

Device view models carry an opaque scoped identity, source, device kind,
capabilities, observed values, observation time, and integration state. Normalize
the current cameras into that envelope without rewriting `NativeCamera` or
pretending non-camera support exists. Future contact/motion/chime backends can
populate the same presentation contract; camera media policy remains specialized.

### State, events, and resource budgets

Raw daemon status is owner-private and can include large media diagnostics. Build
a compact projection inside the daemon; do not construct/forward raw status to
every browser. Device names are untrusted text and must always be escaped.

Initial limits to test and tune:

- At most 32 HTTP connections, 8 authenticated sessions, 8 SSE subscribers,
  four concurrent daemon RPCs, and one serialized configuration/control mutation.
- One shared compact state read every two seconds while visible clients need it;
  coalesce reads across tabs. Suspend or slow hidden clients and stop fast polling
  when no active readers remain. Preserve CLI/TUI capacity within the existing
  16-connection owner endpoint limit.
- Pairing inspection is a separate cached operation on panel entry and relevant
  changes, not part of the fast poll. Existing pairing pages allow four parallel
  adapter checks and a 25-second deadline. Invalidate on account/adapter changes.
- JSON requests at most 64 KiB, daemon responses at most the current 1 MiB bound,
  and a substantially smaller explicit compact-state target (initially 128 KiB).
  Sensitive reveal responses are outside generic state/cache/feed paths.
- An authenticated SSE feed carries projected changes and recent activity, with
  daemon/account epoch, monotonic cursor, bounded replay (initially 256 entries),
  overflow/resync indication, bounded per-client queues, and slow-client retirement.
  Mark lost coverage rather than inventing a complete history.
- Zero stream leases, snapshots, warm sources, or camera wakeups from opening the
  management dashboard. Media limits remain unchanged.

All timeouts and capacities are visible in tests. Disconnect/stale-generation
behavior, multiple tabs, and reconnection are first-class state-machine cases.

Activity requires new instrumentation as well as a projection. Today's recent
action list records media incident actions, not a general owner audit, and event
connection changes are current state rather than retained transitions. Publish
bounded, allowlisted activity summaries at actual daemon owner-operation and
connection boundaries so CLI, TUI, and browser actions have consistent coverage.
Record operation category, opaque target, timestamp, and outcome, never arguments
or secrets. Fence by account epoch and retire old-account entries. This remains
a bounded recent activity feed, not a durable security audit.

## Local access, authentication, and secrets

Default binding remains loopback until explicit local-network enablement. LAN
access uses HTTPS and an allowlisted canonical hostname/IP. Generate an
installation-specific server key/certificate or accept an owner-supplied trusted
certificate. The bootstrap command displays the canonical URL and certificate
fingerprint through the trusted local/SSH channel.

**Open deployment gate:** self-signed first-use trust must be tested in Firefox
and iPhone Safari before promising easy onboarding. No custom domain or reverse
proxy should be mandatory. Do not silently install a broadly trusted CA or train
the user to bypass unexplained certificate errors. If certificate exceptions are
not an acceptable/reliable phone workflow, return with a tested trust-enrollment
alternative before enabling LAN access. Apple's manual certificate trust process
is documented [here](https://support.apple.com/en-ie/102390).

Use the existing cryptography dependency's Argon2id support for the Halo password
verifier, with a calibrated initial 64 MiB/three-pass cost and bounded verification
workers outside the request loop. Qualification must confirm support in the exact
packaged runtime. Store only the salted verifier in a private, versioned atomic
file. Recovery is an owner CLI action that resets web authentication and revokes
sessions while preserving Ring credentials and HomeKit pairing/state.
[Cryptography KDF documentation](https://cryptography.io/en/stable/hazmat/primitives/key-derivation-functions/),
[OWASP password storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).

Use random, opaque, server-side sessions and a `__Host-` cookie with Secure,
HttpOnly, SameSite=Strict, Path=/, and no Domain attribute. Initial policy: 30-minute
idle / 12-hour absolute expiry; reauthenticate for secret reveal/reset and web
password changes. Rotate session identifiers after authentication, rate-limit
failed attempts, cap KDF work, and invalidate sessions on password recovery.
In-memory sessions are sufficient initially; companion restart requires sign-in.
[OWASP session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).

All mutations, including setup and sign-in, use anti-CSRF protection and exact
Origin/Host checks. Do not trust forwarded headers by default. Same-origin access
only, no credentialed CORS, no GET mutations, and no auth secrets in localStorage.
Use a restrictive CSP, local assets, frame protection, and no-store for sensitive
responses. Logs never contain passwords, OTPs, cookies, CSRF tokens, setup codes,
QR payloads, or request bodies.
[OWASP CSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).

## Managed service and release lifecycle

Add `halo-server-web.service`, independent of the daemon's running state. Do not
use `BindsTo`/`PartOf` relationships that remove the recovery UI when the daemon
stops. Give it its own owner-only health socket reporting selected release and
boot identity. It must retain the canonical shared lifetime lease like other
managed Halo processes.

The independent release operator must:

1. Acquire the canonical maintenance lock and record both units' state.
2. Drain web operations, stop both units, and prove all old owners/children have
   retired before acquiring the existing exclusive release gate.
3. Verify/select compatible immutable code, then restore intended operation in
   daemon-before-companion order and verify both health contracts.
4. Preserve pairing, Ring credentials, recording preferences, web settings and
   each service's enabled/active state independently. A disabled but manually
   running service may need its running state restored. Do not enable a service
   because it was active, or start an intentionally inactive service merely
   because it is enabled for boot.
5. Handle activation failure and uninstall across both units. Recognize known
   generated unit migrations; never overwrite unknown user customizations.

**Rollback to pre-web releases:** older code has no `web` entry point. Stop the
web unit and suppress its future startup, including reboot, by a qualified
capability-aware launch guard or explicit disablement. Retain its desired
enablement separately for a later compatible upgrade, along with private web
settings. Simply stopping an enabled unit is insufficient. Recover camera
operation without a failing companion service loop. Do not roll mutable
Ring/HomeKit data backward.

Ordinary browser restart is a different, narrow operation. The companion takes
the canonical maintenance lock nonblockingly, validates the managed installation,
exact daemon unit/launcher and selection, records an operation ID plus starting
invocation, and runs a fixed `systemctl --user --no-ask-password restart
halo-server.service` argv without a shell. No client-controlled command arguments
are accepted. Verify new boot/invocation and the same selected release afterward.
Persist and reconcile possibly-sent outcomes on interruption; never resend merely
because a browser timed out. Do not implicitly enable, unmask, reinstall, or update
a service. Browser software activation remains outside this release.

## Implementation slices and acceptance gates

| Slice | Deliverable | Required validation |
| --- | --- | --- |
| **1. Contracts and design system** | Freeze page flows, typed web DTOs, scope/freshness/error semantics, capability-based device envelope, theme tokens, component inventory, exact frontend toolchain. | Contract fixtures for connected/stale/offline/unknown/conflict states; empty and many-device layouts; both themes and mobile review. |
| **2. Companion and release ownership** | Separate web process/local health socket; shared owner client; both-unit installer, activation, rollback and fixed restart operation. No LAN browser control yet. | Restart versus activation race, stalled child retirement, custom-unit refusal, daemon-down recovery, disabled-unit preservation, old-release rollback, operation reconciliation. |
| **3. Secure browser access** | HTTPS/bootstrap, Halo password/session/CSRF, local recovery, typed allowlisted HTTP boundary, budgets and private stores. | Host/Origin attacks, unauthenticated access, session fixation/expiry, KDF limits, secret-free logs, certificate onboarding on Firefox/iPhone, exact-runtime crypto checks. |
| **4. GTK-style shell and read views** | Responsive navigation, Overview, Devices, Integrations, compact shared state and recent activity feed. | Keyboard/touch/zoom, reduced motion, large/untrusted names, unknown/stale truth, cursor overflow/resync, many tabs sharing bounded daemon work, zero camera activation. |
| **5. Account and device administration** | Ring MFA flow, HomeKit selection, reveal/QR/reset, verified camera controls, warm preference, draft-aware settings. | Account/generation changes, CAS conflicts, no selection loss, reveal concealment, MFA expiry, accepted versus observed state, uncertain-write recovery, recording opt-in unchanged. |
| **6. Diagnostics and recovery UX** | Doctor, safe support download, actionable errors, saved/applied/restart banners and progress screens; web sessions/password administration. | Daemon unavailable, interrupted restart, report redaction/download bounds, pending reset recovery, no arbitrary path/command, no unrequested probe/media work. |
| **7. Installed artifact qualification** | Static assets in sealed release; offline reproducible build, licenses, CI and source/install browser tests. | Actual bundled assets and browser API contract, restrictive CSP, dependency pins, both-unit service tests, existing media/pairing suites, no runtime dependency downloads. |
| **8. Controlled seven-camera pilot** | Install with current pairings and recording preferences preserved; exercise desktop and phone workflows, then observe normal Home use. | Baseline/post-install hashes; all seven still paired/published; snapshot/live-view comparison under multiple web tabs; reboot and web/daemon restart; phone background/reconnect; owner usability acceptance. |

Slices 1 and fixture-based design work can proceed without touching the live
server. No deployed management controls precede the lifecycle and access gates.
Test and review each slice before continuing. Do not treat mockup interactions,
synthetic fixtures, or automated browser emulation as proof of physical Safari,
Ring, or Apple Home behavior.

## Current code evidence and remaining decisions

- [Owner administration](../../src/halo_server/owner_admin.py) supplies configuration,
  controls, diagnostics, and pairing actions; [owner guide](../owner-administration.md)
  documents their semantics.
- [Private admin transport](../../src/halo_server/api/admin.py) verifies the Unix
  peer UID and bounds requests/connections. It is not an HTTP authentication layer.
- [TUI client](../../src/halo_server/tui/client.py) contains important validation
  that the shared client must retain.
- [Camera summaries](../../src/halo_server/ring/devices.py) currently reject
  non-camera families. [HomeKit models](../../src/halo_server/homekit/models.py)
  remain camera-specific; this plan does not claim sensor support.
- [Pairing administration](../../src/halo_server/homekit/pairing_admin.py),
  [recording settings](../../src/halo_server/homekit/recording_settings.py),
  [release guard](../../src/halo_server/release_guard.py),
  [release activation](../../src/halo_server/release_activation.py), and
  [service integration](../../src/halo_server/release_service.py) define invariants
  the web interface must preserve.

No user decision is required to complete this plan. Proposed defaults are one
Halo administrator, local-network access, native-looking adaptive UI, separate
web companion, and no automatic media or recording changes. The first implementation
checks must settle actual certificate onboarding, exact frontend pins, measured
resource budgets, and packaged Argon2id availability. Any broader infrastructure
requirement or backend replacement should be reported before adoption.

## Design concept

An isolated interactive concept accompanies this plan in the conversation. It
uses the existing Halo lens artwork, sample values and purely local navigation.
Overview, device details, integrations, activity, diagnostics and settings can
be explored; Settings offers light/dark and spacing previews. It does not connect
to Ring, HomeKit, the daemon, or any server and performs no real actions. Production
accessibility, auth, lifecycle and packaging work remain the slices above.

Planning review checked the proposed backend mapping and corrected explicit
pre-web rollback/boot behavior and new activity instrumentation. The concept's
source can be checked locally; automated rendered-browser verification was not
available in this pass (no installed headless browser, and the in-app browser
blocks local-file URLs). This is not a claim of completed browser acceptance.
