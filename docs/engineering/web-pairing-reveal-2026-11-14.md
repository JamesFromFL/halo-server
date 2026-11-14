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
