"""Use the verified installed Python graph for native children and local health."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from halo_server.release_guard import ReleaseGuardError, inherited_guard_fds

READER_VERSION = 14
_RELEASE_KEYS = (
    "HALO_RELEASE_ROOT",
    "HALO_RELEASE_MANIFEST_SHA256",
    "HALO_RELEASE_ID",
    "HALO_RELEASE_MINIMUM_READER",
)
_WORKERS = {"transcode_worker.py", "audio_worker.py", "snapshot_worker.py", "recording_worker.py"}
_BOOTSTRAP = (
    "import runpy,sys;sys.path.insert(0,sys.argv[1]);"
    "path=sys.argv[2];sys.argv=[path];runpy.run_path(path,run_name='__main__')"
)
_PROBE_BOOTSTRAP = (
    "import sys;sys.path.insert(0,sys.argv[1]);"
    "exec(compile(sys.argv[2],'<local-codec-probe>','exec'))"
)


def managed_context():
    if not any(key in os.environ for key in _RELEASE_KEYS):
        return None
    from halo_server.release import managed_release

    release = managed_release()
    if release is None:
        raise ReleaseGuardError("Managed release context is incomplete")
    return release


def python_worker_command(worker: Path) -> list[str]:
    release = managed_context()
    if release is None:
        return [sys.executable, "-I", str(worker)]
    inherited_guard_fds()
    expected = release.site_packages / "halo_server" / "media" / worker.name
    if worker.name not in _WORKERS or worker != expected:
        raise ReleaseGuardError("Native worker is outside the managed release")
    return [
        str(release.interpreter),
        "-I",
        "-S",
        "-B",
        "-c",
        _BOOTSTRAP,
        str(release.site_packages),
        str(worker),
    ]


def python_probe_command(code: str) -> list[str]:
    release = managed_context()
    if release is None:
        return [sys.executable, "-I", "-c", code]
    inherited_guard_fds()
    return [
        str(release.interpreter),
        "-I",
        "-S",
        "-B",
        "-c",
        _PROBE_BOOTSTRAP,
        str(release.site_packages),
        code,
    ]


def release_status() -> dict:
    release = managed_context()
    if release is None:
        return {"managed": False, "reader_version": READER_VERSION}
    return {
        "managed": True,
        "release_id": release.release_id,
        "manifest_sha256": release.manifest_sha256,
        "minimum_reader": int(os.environ["HALO_RELEASE_MINIMUM_READER"]),
        "reader_version": release.reader_version,
    }


def validate_runtime_state(state_directory: Path) -> None:
    """Respect a saved reader floor before starting source or managed resources."""
    inherited_guard_fds(state_directory=state_directory)
    release = managed_context()
    policy_exists = False
    for name in ("release-policy.json", ".release-policy.json.enrolled"):
        try:
            (state_directory / name).lstat()
        except FileNotFoundError:
            continue
        policy_exists = True
    if release is None and not policy_exists:
        return
    from halo_server.release_activation import PolicyStore

    policy = PolicyStore(state_directory).load()
    if (
        policy.minimum_reader > READER_VERSION
        or release is not None
        and release.reader_version < policy.minimum_reader
    ):
        raise ReleaseGuardError("Current code cannot read this mutable release state")


def validate_operating_mode(configuration) -> None:
    """The isolated managed graph has no ambient GI compatibility backend."""
    if managed_context() is not None and configuration.compatibility_enabled:
        from halo_server.configuration import ConfigurationError

        raise ConfigurationError(
            "Managed releases support homekit or ring-only mode; "
            "compatibility and dual require the source installation"
        )
