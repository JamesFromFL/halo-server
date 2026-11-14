# Non-camera wave 9: release readiness and physical pilot handoff

This wave strengthens release preflight and local diagnostics, preserves mutable
non-camera state through activation/rollback and prepares a reproducible installed
candidate. No production deployment or Git push is part of this wave.

## Changes

The release builder requires nonempty ordinary camera, device and pairing-reset
entries before installing dependencies. Missing, empty, directory or symlink
device entries fail preflight. Manifest v1 entry keys remain unchanged so older
launchers can still verify the envelope; the full immutable file census covers
`device-main.js`.

Local doctor reports the device child and separate device pairing directory.
A recovery marker is detected without reading or removing its contents. Missing
or empty/symlink device entries cannot be called present. An absent pairing
directory before first publication remains explicitly unverified, rather than a
false successful storage check.

Activation/rollback/uninstall regressions now include non-camera monitoring,
Alarm/light/Chime preferences, bridge registry/publication and device pairing/cache
sentinels alongside existing camera state. Code replacement must preserve all of
those bytes. Newer registry mappings may require a compatible reader even when
the outer release manifest is understood by an older launcher.

The [acceptance guide](../noncamera-acceptance.md) separates server runtime checks,
camera coexistence, sensor pilot, per-family controls and model/role limitations.
Panic/emergency dispatch remains excluded. Monitoring, publication and controls
retain independent explicit opt-ins.

## Qualification record

The focused release-input, activation/state-preservation and runtime-doctor
selection passed 35 tests. The final full Python suite passed **3,978 tests**,
with one IPv6-loopback case skipped because this host has no IPv6 loopback
address. Its 83 warnings are upstream asyncio/GLib and Ring SDK deprecations.
Repository-wide Ruff lint, formatting and diff checks passed. Adapter validation
passed **323 tests**; the final interface passed **53 tests** and the browser
checks recorded in wave 8. These selections overlap; they are not additive.

An older camera-control test expected overlapping light requests to queue. The
shared lighting guard intentionally returns `lighting_busy` instead. The revised
test verifies no SDK submission or delayed replay, a deliberate retry after
retirement, and unchanged ordered siren behavior. Its 76-test service/control
selection passed before the successful full-suite rerun. This follow-up changes
tests and documentation only, not the candidate's production code.

Two independent offline application wheel builds matched. Two complete release
builds, each in a different path containing spaces, also matched byte-for-byte:

- Source: `826dd559b315455bb4bd5c4a1b8791c4f9549241`.
- Release: `62aeb0f6f1a81479d6c99b59721f83039f73922eea3fe4ebce8912ee190ec04f`.
- Archive SHA256: `49031bc3b466ce6c6ddb7f39f60ef01edb0c0a39a4aa113b0d9529dcddb35a23`.
- Manifest SHA256: `67b8c12d1723ae4982801373d02068c9658d01a6fc211798e25fb1acc305af41`.

The extracted, sealed package passed **622 device**, **68 web** and **12 media
repair** tests, with no skips or failures. Halo and runtime dependency imports
were verified to come from the package; the development environment could not
supply missing runtime dependencies. Frozen test source was verified before and
after each phase. No native children remained afterward.

Both actual installed HomeKit children answered hello/ping concurrently with
separate pairing owners. Retiring the device child preserved camera responses;
both exited cleanly and released pairing/release locks. Fresh pairing stores
remained empty. The installed daemon also started healthy through the managed
activation path, generated both user-unit definitions, stopped with exit zero,
and released the retirement gate. The injected unit controller never called real
systemctl; the web unit stayed inactive. No real account, pairing/publication or
Ring actuation was used. Native probes inherited a kernel network fence; web and
device fixtures allowed only local test connections.

Qualification repaired work-harness issues without changing the release: missing
offline npm entries were recovered from hash-matching local cache bodies; the
extracted mutable prefix was given its required private mode; a temporary Unix
socket path was shortened; and isolated pytest tooling included its own `py.py`
shim and the read-only system linker lookup needed by ifaddr. Earlier failure
logs and subsequent passing receipts are preserved.

Evidence and reusable local qualification scripts are under:

`/home/james/Documents/Codex/2026-10-25/he/work/halo-server-noncamera-wave9-2026-11-14/`.

The package is qualified **locally only**, against desktop Python 3.14.10 and its
recorded host-library hashes. A desktop-qualified artifact is not a fresh server
ABI proof. Fresh remote qualification, and a target-specific rebuild if needed,
remain required before deployment. Nothing was pushed or deployed.

### SSH follow-up, November 14

The earlier `Permission denied (publickey,password)` checks used the desktop's
default identity, not Halo's existing deployment key. The dedicated key remained
accepted: access had not been lost for that identity. It is referenced by
`work/halo-server-web-deployment-2026-11-14/deploy_web.py` and stored privately at
`work/halo-server-deployment-2026-10-28/deploy_ed25519`; do not copy it into source
or qualification reports.

The desktop SSH configuration now selects that identity with `IdentityAgent none`
and `IdentitiesOnly yes` for `192.168.1.136` and `james-server`, before the existing
global 1Password agent setting. Strict host verification stays enabled; unrelated
hosts retain their existing settings. The original configuration was backed up
privately under `work/halo-server-ssh-recovery-2026-11-14/`.

Fresh BatchMode connections by IP and alias succeeded as `james` on `James-Server`.
The server reports Python **3.14.9**, and both `halo-server.service` and
`halo-server-web.service` are active. This verifies access and service activity,
not the complete target library/ABI proof or physical-device acceptance. No
server configuration, running service or installed Halo package was changed.

The current adapter pins HAP `2.3.0-halo.1`, versus the last recorded deployed
`2.2.3-halo.2`. Both camera and device children are rebuilt and qualified afresh;
the previous static-only release's adapter-equivalence receipt is not reused as
proof. Physical Apple/Ring acceptance and the seven cameras' live coexistence
remain server-side gates after installation.

### Completed target qualification

The subsequent [server-target qualification](noncamera-target-qualification-2026-11-14.md)
completed the fresh runtime proof, fixed two Ring push issues and a device bridge
shutdown race, and verified the final candidate in the server cache. It is ready
for controlled deployment and physical acceptance; the running release remains
unchanged. The earlier local-only artifact above is retained as historical
qualification evidence rather than the current deployment candidate.
