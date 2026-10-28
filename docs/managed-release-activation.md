# Managed release activation

A release contains code and qualified runtime artifacts. Credentials, camera
identities, privacy preferences, enrolled markers, pairing records and pending
deny journals stay in the canonical XDG state directory. Activation and rollback
never restore a state-directory backup.

`prefix/selection.json` is a private revision-checked reference to immutable
`prefix/releases/<release-id>/manifest.json`. The canonical
`state_directory/release-policy.json` is a private, monotonically increasing
minimum code-reader version. An enrollment marker prevents deletion of an
existing policy from becoming an implicit first install. Managed launch refuses
missing or invalid policy before importing selected code.

The independent `halo-release` operator verifies code under the launcher's
shared lifetime gate, then releases only its own inherited gate before starting
any service, event loop, thread or child. Daemon administration cannot activate
code. The operator first verifies staged files, asks the single user unit to
stop, and acquires the canonical exclusive lifetime gate. A live daemon,
adapter, helper or native worker retains a shared descriptor and blocks the
switch even if the original parent PID has exited. Different installation
prefixes use this same canonical state gate. A separate canonical-state `release-install.lock` serializes the entire
operator lifecycle across prefixes, including restart and health checks; a
prefix install lock adds local serialization. The lock order is canonical
operator, prefix operator, then exclusive lifetime gate after stopping the
service. These locks do not deadlock the running service.

Before stopping the unit, the operator captures a responding local daemon's
authenticated peer PID when available. After the stop it requires the admin
socket to be unavailable and the captured process to have exited. A manually
started older daemon is reported as `active_unmanaged`; the operator does not
kill it or change selection or the reader floor. Releases predating the shared
lifetime guard cannot prove orphan-child retirement with that guard. Fully exit
such manual installations and their children before the first managed install.

The policy is durable before the active selector can point at new code. A
second policy read under the exclusive gate catches changes from another
prefix. Selection writes use atomic replace, file fsync, directory fsync and
revision CAS. An uncertain write reports uncertainty; it never claims the old
selector survived. A canceled caller waits for the owned operation to settle.

After the gate is released, health checks require the exact selected release
identity and local mode/resource/adapter readiness. Ring login, cloud reachability,
camera availability, recording warm demand and Home hub connectivity are not
upgrade health checks. A local failure may restore previous code only after it
is verified against the latest reader floor. A newer incompatible state reader
leaves the service stopped instead of launching old code. Mutable files are
never rolled back.

Operator commands after staging a verified release:

```console
PREFIX/bin/halo-release --prefix /absolute/prefix activate --release-id SHA256 --manifest-sha256 SHA256 --expected-revision 0
PREFIX/bin/halo-release --prefix /absolute/prefix rollback --expected-revision 2
PREFIX/bin/halo-release --prefix /absolute/prefix --state-directory /absolute/state unit
PREFIX/bin/halo-release --prefix /absolute/prefix uninstall --expected-revision 3
```

For a first installation without an existing selection, or recovery when the
selected code cannot start its operator, invoke the audited distribution/source
installer rather than weakening managed launch's missing-policy checks:

```console
python tools/install_release.py --prefix /absolute/prefix --state-directory /absolute/state activate --install-unit --initial-mode homekit --release-id SHA256 --manifest-sha256 SHA256 --expected-revision 0
```

The installer requires an already staged offline artifact. `--install-unit`
explicitly installs the exact single private user unit and reloads the manager,
refusing to overwrite an existing different unit. Stable launchers are installed
under the exclusive lifetime gate before first selection. It performs no
dependency downloads. The generated unit binds this exact state directory so
the daemon uses the same reader floor and mutable data as its operator.
`--initial-mode homekit` saves the initial HomeKit mode only when configuration
is absent, under the same exclusive gate and private configuration transaction.
It never overwrites saved settings. A fresh install without that explicit choice
returns `initial_mode_required` before selecting code. Existing `homekit` or
`ring-only` settings are preserved; `compatibility` and `dual` require an owner
mode change through the existing source administration before managed migration.
The isolated managed bundle does not include PyGObject/GStreamer RTSP and cannot
read system GI packages. The unit obeys saved mode and has no mode override.
The `unit` command prints a unit without installing, enabling or starting it.
`uninstall` stops and disables that unit and removes active selection. It keeps
mutable state and immutable release files for explicit recovery. It does not
purge credentials, pairing keys or privacy/deny records.

Installation starts the unit without implicitly enabling login startup. To opt
into starting it with the user service manager, run explicitly:

```console
systemctl --user enable halo-server.service
```

This enables user-session startup. Running before login or surviving logout
requires separate owner-approved lingering setup and acceptance testing.

The unit uses `/usr/bin/env --` with an absolute quoted launcher argument, a
private umask and group-wide stop. No shell processes its command line.
Dollar and percent signs in paths are escaped using systemd command syntax.
These choices follow the primary [systemd service command rules](https://www.freedesktop.org/software/systemd/man/261/systemd.service.html)
and [systemd process-group lifecycle rules](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html).
Synthetic/disposable-prefix tests do not qualify a real reboot or Apple/Ring
interoperability; those remain separate acceptance checks.

Managed TUI and client processes also retain the canonical lifetime lock. Close
them before activation, rollback or removal. A remaining owner produces a busy
refusal; retry after it exits. Do not bypass or remove the lock to force an upgrade.
