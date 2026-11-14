# Web UI audit and upgrade recovery — 2026-11-14

## Scope and reproduced failure

Audit the complete browser entry point, setup/sign-in/re-authentication, live
state feed, navigation, all six pages, camera/noncamera controls, settings,
diagnostics, shared dialogs, error recovery, and static serving. Retain the GTK
visual language and the existing explicit permissions for physical operations.
This work does not claim to complete the separate Ring feature-parity backlog.

The running daemon and companion were healthy with zero service restarts. Fresh
Chromium and native Firefox 160 profiles rendered the public HTTPS sign-in page.
The real owner projection was 105,741 compact JSON bytes, with seven cameras and
28 noncamera records, and passed the frontend validator.

The previous and current installed index files were different but both were
556 bytes with mtime `1793145600000000000`. aiohttp FileResponse gave both the
ETag `18e2886c368e0000-22c`. A public request using that old validator returned
304; its referenced old JavaScript returned 404. A returning browser could
therefore retain an HTML page whose scripts no longer existed after deployment.
Earlier fresh-profile checks did not cover this upgrade path.

## Repairs and usability changes

| Finding | Result |
| --- | --- |
| Cached index survives an upgrade with removed script names | Always return the current index with `no-store`, ignoring old conditional validators. Stable nonhashed assets use manifest content hashes. |
| A startup or component exception can leave no usable screen | Static startup/reload fallback, a root error boundary, and page-level recovery preserve a usable path back. No CSP relaxation or inline script is required. |
| Old HTTP state can replace a newer pushed observation or survive sign-out | Fence and cancel superseded reads; update state only within the current session. |
| Password/setup completion can race an earlier authentication read | Join the older read, then verify the persisted revision with a fresh read before issuing the session. |
| Password changes rotate the cookie while the old state feed remains active | Hand off the returned session/CSRF immediately and reconnect the feed; never replay an uncertain mutation. |
| Outages can look like an empty inventory or deleted device | Distinguish loading/unavailable/empty/filter-no-match states and expose clear refresh/reset actions. |
| Late selection, operation or diagnostic responses can land on another account/page/device | Fence page responses by scope and request identity; do not overwrite a successor operation. |
| A pending Settings reload can erase newly typed changes | Preserve edits made after the reload began; keep conflicts explicit. |
| Canceled Ring login can receive a late MFA challenge, or a successful account change can leave the old form open | Invalidate late challenges and close the old form when the new account arrives, including an SSE update before the login response. |
| A stale optional observation hides unrelated current sensor readings | Display each already-normalized current value independently; stale values remain unknown. |
| Partial monitoring is described as an offline source | Explain that some observations are incomplete or out of date. Alarm phase/signal qualification remains pending. |
| Mobile navigation remains blocking after widening the window | Close the drawer when returning to desktop width. |
| Dialog headings share one DOM identifier | Give each confirmation a unique accessible label while retaining native modal focus behavior. |

The audit also reviewed existing keyboard focus, minimum control sizes, light
and dark themes, reduced motion, responsive layouts, pairing-code concealment,
and uncertain-operation handling. Changes preserve monitoring-only access,
pairing identities, recording opt-ins and physical-control protections.

## Verification model

The browser acceptance scripts can now run in both Chromium and pinned Firefox
with `HALO_BROWSER=chromium|firefox`. The cache-upgrade check keeps the same
browser cache while replacing the server with a different bundle whose index
has identical size and timestamp, then uses an ordinary reload. It exercises
the real static-serving implementation over isolated HTTPS. Both engines passed.

The shell and page regressions include a sanitized fixture preserving the real
seven-camera/28-record response shape; personal labels and identifiers are
replaced. Synthetic controls never reach the real Ring account. Firefox 159
is the Playwright-pinned regression engine; native desktop Firefox 160 is
checked separately against the deployed public interface.

Source results, installed-package qualification, deployment preservation checks
and final live HTTP/browser results are retained under
`work/halo-web-ui-audit-2026-11-14/`. The live Alarm phase/signal acceptance gap,
physical device command tests, and Apple Home HKSV clip acceptance remain
distinct from this web UI audit. No physical command or recording enablement
is part of the repair.
