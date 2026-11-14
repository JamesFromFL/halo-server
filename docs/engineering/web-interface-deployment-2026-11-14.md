# Web interface deployment — 2026-11-14

The GTK-style owner console is deployed on `james-server` at
`https://192.168.1.136:18444`. Both managed user units are active and enabled;
the server account already has lingering enabled. Owner web enrollment remains
for the user to complete privately in their SSH terminal and browser.

## Final deployed identity

- Selection revision: **22**.
- Release: `0acc0cf9ff7a21a8ee8398b77bdb702c59a02eeb5409438b444cf56d37ff6743`.
- Manifest SHA256: `e553c1934b6adc1d2044aaf46a1ce6ec6ca05ae4c009f9332737a30c1d46f157`.
- Archive SHA256: `72c2bcdf8c06580d9b1800b9cd644b9ba1b21b9207210b4cdb10e244fab63155`.
- Previous retained web release: `de3758c6f4dbad27ba2b6aab0b00a861c9885ce92fc51a9604c5acbe69ef8e09`.
- Original snapshot-fixed release is also retained:
  `1516fa3d2033c4dc8d3e76b21b9818ae5c93ff366169107d69a30c7265797278`.

This deployment preserves the live `2.2.3-halo.2` HAP adapter and its snapshot
queue correction. It does not qualify the newer HAP version in the development
dependency declaration. The frozen release source and source manifest record
the production dependency selection explicitly.

## Qualification and live checks

- Confirmed the server's Python 3.14.9 and all 13 interpreter/external file hashes
  still match the target runtime before building and again before the follow-up.
- Both independent builds of each deployed candidate were byte-identical.
- Initial candidate: 272 adapter tests, 68 installed web/security/TLS tests,
  and 12 installed snapshot/audio regression tests passed. Installed Argon2,
  asset validation, an actual daemon with an injected two-unit controller, and
  synthetic diagnostic capture/replay/export also passed. Those isolated checks
  used no real Ring account or camera stream.
- The existing 4,528 adapter/Node/dependency files and media delivery code were
  preserved. The read-only warm-budget projection is documented in the payload
  qualification receipt.
- A live visual check caught a frontend password hint/minimum of 12 while the
  backend requires 15. Corrected Halo password forms to shared 15–128 bounds.
  Fifteen frontend tests, 60 browser layout cases, and a real HTTPS browser check
  that rejects 14-character setup before submitting passed. Ring password fields
  retain their separate requirements.
- The final correction changed only static assets and wheel checksum metadata;
  all backend, adapter, dependency, and installer bytes match the first web
  release. Its unchanged backend qualification is explicitly reused by hash.
- Both live services report the final release, active/running/enabled, zero
  automatic restarts, and no pending web operations. Socket peer UID/PID matches
  the corresponding systemd process.
- All seven cameras remain selected, paired, and published. Pairing files,
  configuration, and recording preferences are byte-identical across upgrades.
  HKSV and owner warming remain off. Media/cleanup resource counts are zero.
- The console's actual owner-state API reports a complete seven-camera inventory,
  an authenticated Ring connection, and connected Ring events.
- Desktop-to-server HTTPS checks validate the hostname and SSH-authenticated
  certificate fingerprint. Unauthenticated private API access, foreign Origin,
  and incorrect Host requests are rejected. Security headers and secure session
  cookie attributes pass.
- The final live setup page renders at desktop and phone sizes without overflow
  or JavaScript errors. Its password fields enforce 15 characters. The isolated
  browser pins the verified certificate key and makes only same-origin GETs;
  it opens no camera media and creates no owner credentials.

The first local lifecycle harness attempt used an incorrectly public scratch
prefix and was correctly rejected by the installer. Correcting that test-only
directory to mode 0700 allowed the qualification to pass. No product change or
server mutation resulted from that attempt.

## Private owner setup and remaining acceptance

In the owner's existing SSH terminal:

```sh
~/.local/share/halo-server/bin/halo-server web access
~/.local/share/halo-server/bin/halo-server web setup
```

The first command reports the URL and certificate fingerprint. The second issues
a ten-minute, single-use enrollment code. Open the console after issuing the
code, verify any local-certificate warning against the trusted terminal output,
then enter the code and choose a Halo web password in the browser. Do not put the
code or password in chat, logs, screenshots, or a URL. The existing Ring session
is already connected and requires no new login.

Physical Firefox/iPhone certificate onboarding, authenticated live-user workflow,
Apple Home viewing after this deployment, and an actual server reboot have not
been exercised in this deployment pass. Automatic startup is configured and
verified, not proven by rebooting the user's machine.

## Evidence and recovery

Detailed local evidence is under
`/home/james/Documents/Codex/2026-10-25/he/work/halo-server-web-deployment-2026-11-14/`:
`candidate/evidence/final-receipt.json`,
`candidate-form-fix/evidence/final-receipt.json`, both candidates' `remote-evidence/`,
`web-check-form-fix.json`, `live-https-checks-form-fix.json`,
`live-browser-checks-form-fix.json`, and the final desktop/phone screenshots.

The existing mutable state was never restored from backups. Both immutable
rollback releases remain available. Use the currently selected managed release
operator for any rollback so it drains and retires both services and preserves
their independent startup intent. Consult `docs/web-interface.md` before
rolling back to a release without the web companion.

No commit or push was part of this deployment request.
