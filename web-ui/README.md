# Halo Server owner console

A static Svelte 5 / TypeScript frontend for the separate authenticated Halo web
companion. It follows Halo GTK's navigation ribbon, header bars, grouped rows,
8 px cards, restrained blue accent, and system light/dark appearance. It starts
no camera streams, snapshots, recording sources, or advanced probes on load.

## Build and checks

Use the repository's supported Node 26 toolchain. The exact package versions and
transitive integrity hashes are in `package-lock.json`. TypeScript stays at
6.0.3 because the pinned Svelte checker declares support for major 5 and 6.

```sh
npm ci --ignore-scripts
npm run check
npm test
npm run build
```

The production bundle is generated into `src/halo_server/web/static` and served
by the Python companion. No runtime Node process, CDN, remote fonts, or remote
assets are required. The build includes dependency notices and a deterministic
`build-manifest.json`: schema 1, sorted SHA256 records for every web-ui source
file except node_modules/.git, a SHA256 of compact JSON source records, and sorted
SHA256 records for every output asset except the manifest itself. Build artifacts
must be regenerated after changing any included input, including tests or this
README. Source maps are disabled in production.

The six routes use the real `/api/web/v1` contract. Auth and secrets remain only
in component memory; only appearance is stored in localStorage. Native same-origin
HttpOnly cookies hold session identity. Read failures mark data stale. Mutations
are never automatically retried; inspect their original operation receipt when
an outcome is uncertain. Configuration drafts retain their base revision.

## Browser qualification

`npm run test:browser` requires a fresh isolated loopback HTTPS companion and
synthetic owner data. Set `HALO_TEST_URL`, `HALO_TEST_PASSWORD`, optional
`HALO_SETUP_CODE`, `HALO_BROWSER_OUTPUT` (absolute task output directory), and
`PLAYWRIGHT_BROWSERS_PATH` to an isolated test-browser cache. Never point the test
at a real user's server or pass Ring credentials. The runner tests the installed
HTTP endpoints, auth cookies, actual production assets/CSP, six pages at five
widths in both themes, long/empty search, device history navigation, reversible
fixture light commands, canceled confirmation focus restoration, configuration
draft/conflict handling, 200% CSS zoom, reduced motion, drawer keyboard handling,
logout, and absence of media requests. It writes screenshots and a credential-free report outside the source
tree. Accepting the test certificate in Playwright is confined to that test
context; it is not verification of iPhone trust enrollment or real Ring/HomeKit.

From the repository root, `python scripts/check_web_browser.py --output /absolute/evidence/path`
creates and cleans up the isolated HTTPS/owner fixture and supplies test credentials
without printing them. Use `--installed` to test the installed wheel instead.

Physical iPhone Safari certificate onboarding and live seven-camera regression
checks remain separate deployment acceptance gates.

`npm run test:devices` starts its own loopback-only synthetic HTTP server and
tests the built non-camera device interface. It requires the same isolated
Playwright browser cache; `HALO_BROWSER_OUTPUT` optionally records screenshots
and a receipt. It checks location/type filtering, stale and unknown observations,
the scoped monitoring disable operation, the absence of device actuation controls,
and that non-camera detail pages never request camera administration. It also
checks 24 desktop/mobile/theme layouts. This fixture is not evidence of real Ring
device support or live HomeKit behavior.
