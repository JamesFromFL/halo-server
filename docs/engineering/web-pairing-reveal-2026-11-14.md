# Pairing reveal session handoff — 2026-11-14

## Failure and reproduction

The owner reported that Reveal code accepted the password, became unresponsive,
then refreshed without showing the pairing code. This is a web session lifecycle
bug, independent of HomeKit media or Ring connectivity.

`Authentication.authenticate()` deliberately revokes the prior browser session
and creates a new session and CSRF token. An existing `/events` connection keeps
the cookie from its original HTTP request. Its next observation therefore fails
authentication and closes the stream. The UI marked the observation stale and
waited five seconds to reconnect. Camera and bridge pairing correctly suppress
or clear credentials when their state is stale, so the reveal disappeared or
never appeared even though the password and reveal endpoints succeeded.

The retained pre-fix bundle reproduced this using real HTTPS, password hashing,
cookie rotation and SSE with synthetic owner state. The old feed failed about
200 ms after the first observations and reconnected five seconds later. No Ring
account, real pairing credentials, camera capture or HAP listener was used.

## Repair

- Retire the old feed before submitting password confirmation and invalidate its
  callbacks.
- Consume the new session and CSRF token directly from the successful password
  response, avoiding a second session fetch between confirmation and action.
- Open a replacement event stream and wait for a fresh, valid observation before
  allowing the original action to continue. Bound that wait to 15 seconds and
  report a connection problem if it cannot complete.
- Abort obsolete reconnect session reads and reject their late responses before
  they can overwrite the new CSRF token or replace its feed.
- Tie confirmation to the original dialog, so a delayed response from a canceled
  prompt cannot approve a replacement prompt.
- Restore the original feed after a rejected password. Existing revocation,
  navigation, visibility, authority and stale-state protections remain enforced.

## Scope and verification

The source fix is on current main. The production repair is also backported into
an isolated copy of the currently deployed release, retaining its existing
password-only sign-in, Python backend, camera adapter, media path and policies.
The pending username/device release remains separate. Deployment uses the
managed release installer and preserves the enrolled password, TLS identity and
camera pairings.

Fresh source checks: Svelte type checking, 58 frontend tests, 46 HTTPS/security/
lifecycle tests, 60 real HTTPS dashboard layout combinations, and the bridge,
alarm, light and Chime browser suites. The dashboard check reported no browser
errors, CSP violations or media requests. The
browser mocks now return the complete real reauthentication response contract.
A dedicated real-HTTPS regression checks code visibility across multiple real
state updates, revocation of the old cookie, hidden/navigation/logout clearing,
and cancellation while password verification is in flight.

Local evidence: `work/halo-server-pairing-reveal-2026-11-14/` and
`work/halo-web-pairing-regression-2026-11-14/` in the Codex task workspace. See the
final deployment receipt for the shipped backport identifier and preservation
checks. No GitHub push is part of this repair.

## Deployed result

Source repair committed as `2396944088bc82ea774e323fe3fcde4b6678ba51`.
The focused backport was activated on james-server at selection revision 23:

- Release: `52e72da89552bc0efe3c652c9d15406233f5ca543bf829eb299e6d792988e402`.
- Manifest SHA256: `5c9b8069b47410a102927281a51170b508a200a974d32369dec45a0701638b43`.
- Archive SHA256: `3f5735c0df323ac1e2d9b9d0f107ee8408029f306955de751e899998f46fb8ae`.

Fresh backport verification passed 17 frontend tests, type/format checks and the
actual installed package's real HTTPS/session/SSE regression. Main's final
regression also passed. Backend, media, third-party dependencies, adapter and
runtime match the previous active release byte for byte. Broader backend tests
were inherited only for those unchanged bytes, not reported as rerun.

Deployment prepare, stage, activation and postcheck passed. All seven camera
pairings, enrolled owner authentication, policies, recording settings, TLS
identity and startup intent were preserved; HKSV remains off. The previous
release remains available for recovery. The public HTTPS index, JavaScript and
CSS exactly match the qualified package, the owner remains enrolled, and an
unauthenticated pairing request returns 401. No live password or pairing code
was collected. The owner's final retry in their browser remains unobserved.

Two preliminary package attempts were retained: the first preceded the delayed
reconnect fix; the next exposed unrelated installed RECORD metadata differences
from a different builder path. Reusing the original builder path restored exact
non-static identity without relaxing the accepted runtime changes.

The task workspace's `work/halo-server-pairing-reveal-2026-11-14/deployment-summary.json`
binds the deployed result to its local package, HTTPS and SSH phase evidence.
