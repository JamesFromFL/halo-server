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
selection passed 35 tests. Repository-wide Ruff checks passed; an existing REST
fixture received formatting-only cleanup. Final source regression, deterministic
build and installed-test results will be recorded here when completed. Evidence
and reusable local qualification scripts
are under:

`/home/james/Documents/Codex/2026-10-25/he/work/halo-server-noncamera-wave9-2026-11-14/`.

The desktop currently runs Python 3.14.10; the previous server observation was
3.14.9. A desktop-qualified artifact is not a fresh server ABI proof. SSH currently
rejects noninteractive access; the owner is restoring it. This does not block
local builds/tests, but fresh remote qualification is required before deployment.

The current adapter pins HAP `2.3.0-halo.1`, versus the last recorded deployed
`2.2.3-halo.2`. Both camera and device children are rebuilt and qualified afresh;
the previous static-only release's adapter-equivalence receipt is not reused as
proof. Physical Apple/Ring acceptance and the seven cameras' live coexistence
remain server-side gates after installation.
