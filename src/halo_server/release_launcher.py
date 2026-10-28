"""Install the small trusted launcher separately from replaceable release code.

The activation owner must hold the canonical exclusive release gate. Existing
launcher bytes are never silently replaced. This helper changes no selection,
reader policy, Ring state, service or account.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shlex
import stat
from pathlib import Path

from halo_server.release import ReleaseError, _open_directory, _read_at, canonical

LAUNCHER = """import argparse,hashlib,os,stat,sys,types
from pathlib import Path
base = Path(__file__).absolute().parent
for name, expected in __MODULE_HASHES__:
    path = base / (name + ".py")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o444
                or info.st_size > 1024 * 1024):
            raise SystemExit("Unsafe stable launcher module")
        source = os.read(descriptor, 1024 * 1024 + 1)
        if len(source) != info.st_size or hashlib.sha256(source).hexdigest() != expected:
            raise SystemExit("Changed stable launcher module")
    finally:
        os.close(descriptor)
    module = types.ModuleType(name)
    module.__file__, module.__package__ = str(path), ""
    sys.modules[name] = module
    exec(compile(source, str(path), "exec"), module.__dict__)
from halo_release_runtime import launch_selected
parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--operator", action="store_true")
parser.add_argument("--state-directory", type=Path)
options, arguments = parser.parse_known_args()
state = options.state_directory
if state is None:
    base = Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state"))
    state = base / "halo-server"
state = Path(os.path.abspath(state.expanduser()))
prefix = Path(__file__).absolute().parent.parent
try:
    launch_selected(prefix, state, arguments, operator=options.operator)
except Exception as error:
    print("halo-server: managed release unavailable: " + str(error), file=sys.stderr)
    raise SystemExit(1)
"""


def _directory(parent, name):
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
        created = True
        os.chmod(name, 0o700, dir_fd=parent, follow_symlinks=False)
    except FileExistsError:
        pass
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    info = os.fstat(descriptor)
    if created:
        os.fchmod(descriptor, 0o700)
    if info.st_uid != os.getuid() or stat.S_IMODE(os.fstat(descriptor).st_mode) not in {
        0o700,
        0o555,
    }:
        os.close(descriptor)
        raise ReleaseError("unsafe_launcher")
    return descriptor


def _install(directory, name, body, mode):
    def already_installed():
        try:
            existing, _ = _read_at(directory, name, limit=len(body), mode=mode)
        except FileNotFoundError:
            return False
        if existing != body:
            raise ReleaseError("launcher_update_required") from None
        return True

    if already_installed():
        return
    temporary = "." + name + "." + os.urandom(12).hex() + ".tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(body)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("launcher write failed")
            view = view[count:]
        os.fsync(descriptor)
        try:
            os.link(
                temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False
            )
        except FileExistsError:
            if not already_installed():
                raise ReleaseError("launcher_update_required") from None
        os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


def install_launchers(prefix: Path, *, interpreter: Path) -> dict:
    prefix = Path(os.path.abspath(prefix))
    interpreter = Path(os.path.realpath(interpreter))
    source = Path(__file__).parent
    script = prefix / "libexec/launcher.py"
    files = {
        "halo_release_runtime.py": (source / "release.py").read_bytes(),
        "release_guard.py": (source / "release_guard.py").read_bytes(),
    }
    module_hashes = [
        (name, hashlib.sha256(files[name + ".py"]).hexdigest())
        for name in ("release_guard", "halo_release_runtime")
    ]
    files["launcher.py"] = LAUNCHER.replace("__MODULE_HASHES__", repr(module_hashes)).encode()
    for name, operator in [("halo-server", False), ("halo-release", True)]:
        command = (
            f"exec {shlex.quote(str(interpreter))} -I -S -B "
            f"{shlex.quote(str(script))}" + (" --operator" if operator else "") + ' "$@"\n'
        )
        files["bin/" + name] = (
            "#!/bin/sh\n"
            "unset PYTHONPATH PYTHONHOME NODE_OPTIONS NODE_PATH LD_PRELOAD LD_LIBRARY_PATH\n"
            + command
        ).encode()
    hashes = {name: hashlib.sha256(body).hexdigest() for name, body in sorted(files.items())}
    files["launcher-manifest.json"] = canonical(
        {"version": 1, "interpreter": str(interpreter), "files": hashes}
    )
    directory = _open_directory(prefix)
    descriptors = []
    try:
        metadata = os.fstat(directory)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ReleaseError("unsafe_prefix")
        libexec = _directory(directory, "libexec")
        descriptors.append(libexec)
        bin_directory = _directory(directory, "bin")
        descriptors.append(bin_directory)
        for name, body in files.items():
            parent, filename, mode = (
                (bin_directory, name[4:], 0o555)
                if name.startswith("bin/")
                else (libexec, name, 0o444)
            )
            _install(parent, filename, body, mode)
        for descriptor in descriptors:
            os.fchmod(descriptor, 0o555)
            os.fsync(descriptor)
        os.fsync(directory)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
        os.close(directory)
    return {"version": 1, "files": hashes, "interpreter": str(interpreter)}
