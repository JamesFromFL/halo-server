# Local web owner login — 2026-11-14

The owner requested a stable local access port and a username/password login
before activating the new device release. Halo already has an HTTPS companion
and password authentication. This change makes its single-owner account explicit
with the fixed username `admin` and retains port 18444 as the default.

## Verified running-server baseline

The read-only check performed during this work confirmed the configured URL
`https://192.168.1.136:18444`. The HTTPS certificate matched the fingerprint
obtained through the trusted SSH connection. The anonymous session response
reported `setup_required: true`, and its cookie had the expected secure session
attributes. This confirms that the running installation still needs private
owner password enrollment; it does not establish an authenticated user session.

No enrollment code or password was created or collected. No server configuration,
release selection, running service, or device control was changed during these
checks. The staged device candidate remains inactive.

## Implementation contract

- Keep HTTPS port 18444 as the configurable default. Keep loopback as the initial
  bind address and require explicit owner configuration for LAN access.
- Require the exact lowercase username `admin` for enrollment and sign-in. The
  account name is public and fixed; no default password is provided.
- Let the owner choose a 15–128-character password privately in the browser,
  using the existing local CLI command's ten-minute, single-use enrollment code.
- Retain Argon2id password storage, authentication attempt limits, bounded KDF
  work, secure session cookies, session expiry, same-origin checks, CSRF
  protection, and recent password confirmation for sensitive operations.
- Check the password even when the supplied login username is wrong, and use
  the same generic authentication failure response. Create no authenticated
  session unless both username and password match.
- Keep reauthentication password-only for an already authenticated session.
  Reset and password changes continue to revoke other browser sessions.

The password remains separate from Ring authentication. Discovery, browser
sign-in, and web enrollment do not open camera streams, change alarm modes, or
publish devices.

## Existing credentials and recovery

Authentication storage remains schema version 1. The existing password verifier
is preserved, so an enrolled owner uses `admin` plus the existing Halo password
after upgrading. Initial enrollment remains necessary only when no password has
been configured. No username migration, additional identity store, or automatic
credential reset is introduced.

The managed reader version and floor remain unchanged. This matters because the
current operator advances the global reader floor before selecting and checking
a new release. A floor increase for a new authentication schema would prevent
rollback to the retained reader-14 camera release even before username setup.
The fixed account name avoids that unnecessary recovery restriction.

An explicit rollback to a retained older web release restores its password-only
sign-in behavior. The current password remains required and unchanged. Returning
to the new release again requires `admin` plus that password. Credentials,
camera pairings, and other mutable state are never rolled back with code.

If the password is lost, the local owner runs `halo-server web reset --yes` and
uses the resulting enrollment code to choose a new password for `admin`. The
code and password must stay in the owner's terminal and browser, never chat,
logs, screenshots, or URLs.

## Validation status

Independent review found no actionable authentication or security defects.
Completed checks:

- Full source suite: 4,025 passed, one existing IPv6 environment skip, and 83
  upstream deprecation warnings. Runtime: 316 seconds.

- 38 focused Python tests covering real HTTPS, authentication, and CLI behavior.
  Every registered private HTTP route rejects both visitors without a cookie and
  visitors with an anonymous cookie, without invoking owner controls. Tests cover
  wrong and missing usernames, password verification and throttling on a wrong
  username, enrollment rejection, password rotation, reset, and authenticated
  password confirmation.
- 56 frontend tests, clean formatting, no Svelte errors or warnings, and a fresh
  production bundle with an exact source manifest.
- Real isolated HTTPS browser acceptance: successful enrollment, failed and
  successful sign-in, logout, password-manager field semantics, 60 responsive
  theme/page/viewport combinations, and zero browser errors or CSP violations.
  Blank desktop and phone setup/sign-in screenshots were visually inspected.
  The synthetic fixture made no snapshot requests and opened no media sessions.
- Six cross-version credential compatibility checks using the prechange
  authentication module from commit `3a1ea8651ddbe4eb84e59304a7cba771fe29e26b`.
  Old enrollment works with the new username requirement without rewriting the
  credential file. Password rotation preserves v1, and the retained old module
  accepts the rotated password without a file rewrite. This is module-level
  compatibility evidence with unchanged dependencies, not a full older release
  deployment test.

Evidence is under
`work/halo-server-web-owner-login-2026-11-14/` in the development workspace,
including `browser/browser-smoke.json`, `browser/fixture-media.json`, and
`legacy-auth-compatibility.json` with its reproduction script. All credentials
used in these checks were synthetic; the owner's real password was not collected.
The full source suite passed. Installed package qualification is still in progress.

## Release boundary

The previously qualified and staged inactive release is
`2b4a3ef49620b6c062cb5c171be04fdeca7040e8350fc9d742e9e2862c500600`.
It predates this owner-login change. A new release must be built and qualified
before activation can include the new sign-in behavior. No activation is part
of this implementation pass, and the running server continues using its existing
release until a later controlled activation.
