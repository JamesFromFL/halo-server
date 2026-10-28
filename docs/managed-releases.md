# Managed offline releases

A managed release contains the Python application and its locked dependencies,
the production JavaScript adapter and dependencies, and Node 26.12.1. Startup
uses absolute verified paths. It does not run npm, pip, a compiler, or a download.
The first qualified distribution is specific to its recorded Linux host ABI,
Python interpreter, libc, and host shared-library hashes. It is not a portable
Linux binary distribution. An OS update may require rebuilding and qualifying a
new release.

## Trust and installation

Obtain the archive and its receipt through a trusted distribution channel. Check
the archive SHA-256 against that receipt **before executing its installer**. The
receipt also gives the expected manifest and installer hashes. These are local
integrity checks; an unsigned receipt is not publisher authentication. No code
can authenticate its own installer against a checksum supplied by the same
untrusted download.

Extract the archive under a private absolute prefix:

```sh
install -d -m 700 '/absolute/halo prefix' '/absolute/halo prefix/releases'
tar -xJf RELEASE_ID.tar.xz --no-same-owner --same-permissions -C '/absolute/halo prefix/releases'
```

The archive has one directory named by its 64-character release ID. It contains
no account, token, privacy, recording, or pairing state. First installation uses
the included standalone `install.py`; it does not require a source checkout,
existing selection, existing reader policy, Node, npm, or Python site packages.
Use the exact host interpreter recorded in the receipt/manifest:

```sh
/usr/bin/python3.14 -I -S -B \
  '/absolute/halo prefix/releases/RELEASE_ID/install.py' \
  --manifest-sha256 MANIFEST_SHA256 \
  --state-directory '/absolute/canonical/halo-state' \
  --prefix '/absolute/halo prefix' \
  activate --release-id RELEASE_ID --expected-revision 0 --install-unit \
  --initial-mode homekit
```

`--initial-mode homekit` explicitly initializes a missing configuration. Existing
saved modes are preserved; managed releases support `homekit` and `ring-only`.
A saved `compatibility` or `dual` configuration needs an explicit change through
the source daemon before migration, since this GI-free release does not bundle
GStreamer RTSP. No unit command silently overrides the saved mode.

`--install-unit` is an explicit request to prepare the single user unit and reload
it. A different existing unit is refused. Omit it when managing the unit yourself.
After a successful activation, `systemctl --user enable halo-server.service`
explicitly enables startup at login. Installation does not enable lingering or
automatically change login startup.

The operator stops the old service, proves that its adapter/native children have
retired through the canonical lifetime lock, installs the stable launchers,
durably records the reader floor, and atomically selects the release. It starts
the unit and checks local software health. Local health does not qualify a real
Ring account, Apple Home pairing, or Apple recording delivery.

The installer verifies every release file and the current protected reader floor
before importing bundled code. Only this explicit first-install entry may begin
without a policy, and only when the durable enrollment marker is also absent.
An enrolled state directory with a missing policy fails closed. Normal managed
launch always requires the selection and policy.

The immutable release files must retain their recorded owner and modes:
directories `0555`, ordinary files `0444`, Node `0555`. Keep the prefix private.
Do not edit a release in place. A changed, extra, missing, linked, or unsafe file
makes verification fail.

## Upgrades, rollback, and removal

Extract the new archive beside the old release. Then use the existing stable
operator and the **current** selection revision:

```sh
'/absolute/halo prefix/bin/halo-release' \
  --state-directory '/absolute/canonical/halo-state' \
  activate --release-id NEW_RELEASE_ID --manifest-sha256 NEW_MANIFEST_SHA256 \
  --expected-revision CURRENT_REVISION
```

`rollback --expected-revision CURRENT_REVISION` selects the recorded previous
code only if it supports the current reader floor. It never restores old mutable
state or lowers that floor. `uninstall --expected-revision CURRENT_REVISION`
disables the managed unit and removes the selection while preserving release
files and mutable state. The lifetime guard also covers source-launched services
and inherited adapter/native/helper children; a vanished parent PID is not
retirement proof. A managed launcher cannot stop someone manually executing an
unmanaged legacy program outside this path.

## Rebuilding offline

Preparation is a separate, explicit network-enabled task. The preparer reads
`uv.lock`, selects only compatible runtime wheels, verifies locked hashes/sizes,
and writes `wheel-inputs.json` plus hash-required requirements. The source-only
`http-ece==1.1.0` exception needs a separately reviewed build receipt matching the
locked sdist hash, two byte-identical offline wheel builds, and the wheel hash.
The assembler never builds this sdist. Record the build tool versions and
`SOURCE_DATE_EPOCH=1793145600` in that receipt.

Prepare the official Node archive, signed SHASUMS, and official release keyring.
The assembler pins the archive and keyring hashes and requires the expected
GnuPG `VALIDSIG` fingerprint. Node's bundled npm is not the selected build tool:
prepare the separately pinned npm 12.2.1 archive/tree and populate its cache with
the exact production lock using lifecycle scripts disabled.

Build the adapter with its pinned development toolchain **before** assembly.
The assembler takes the resulting `dist/src`, production package lock, vendored
HAP archive, application wheel and its explicit SHA-256, prepared wheelhouse,
Node inputs, npm tool, and populated cache:

```sh
BUILD_PYTHON -B tools/build_release.py \
  --wheel APP.whl --wheel-sha256 APP_SHA256 --wheelhouse WHEELHOUSE \
  --adapter BUILT_ADAPTER --node-inputs NODE_INPUTS \
  --npm-tool NPM_12_2_1_TREE --npm-cache PREPARED_NPM_CACHE \
  --destination '/absolute/staging/releases' \
  --archive-directory '/absolute/staging/archives' --source-revision REVISION
```

Assembly uses `pip --no-index --only-binary=:all: --require-hashes` and
`npm ci --offline --ignore-scripts --omit=dev`. It verifies the exact production
package graph, rejects development packages, tests native imports without a
loader-path override, audits bundled versus external ELF libraries, seals every
file, and verifies the complete result before publishing the staging directory.
The archive normalizes ownership and timestamps; repeat builds from identical
qualified inputs produce the same release ID, manifest, and archive bytes.
Neither building nor staging changes the current selection or running service.

Installed release verification checks a complete bounded manifest, exact host
qualification, interpreter and external-library hashes, no-follow regular file
ownership/modes, all file bytes, and a complete file census. Runtime worker
admission uses the immutable startup-verified context and the inherited lifetime
lock; it does not rehash the whole installation for every frame or request.
