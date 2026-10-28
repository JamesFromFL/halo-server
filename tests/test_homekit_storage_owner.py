"""Actual filesystem/descriptor proofs for exclusive pinned pairing ownership."""

import json
import os
import select
import stat
import subprocess
import sys

import pytest

from halo_server.homekit.storage_owner import PairingDirectoryOwner, PairingStorageError

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="qualified Linux directory handles")
LOCK = ".pairings.owner.lock"


def pairing_directory(tmp_path):
    parent = tmp_path / "homekit"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    directory = parent / "pairings"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_unsafe_ownership_lock_is_refused_without_changing_target(tmp_path, kind):
    directory = pairing_directory(tmp_path)
    lock = directory.parent / LOCK
    target = tmp_path / "unrelated"
    target.write_bytes(b"unrelated-owner-data")
    target.chmod(0o600)
    if kind == "symlink":
        lock.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, lock)
    elif kind == "fifo":
        os.mkfifo(lock, mode=0o600)
    else:
        lock.mkdir(mode=0o700)
    before = target.stat()
    with pytest.raises(PairingStorageError):
        PairingDirectoryOwner.acquire(directory)
    assert target.read_bytes() == b"unrelated-owner-data"
    assert target.stat().st_mode == before.st_mode
    assert target.stat().st_nlink == before.st_nlink


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o400, 0o000, 0o1600])
def test_unsafe_existing_lock_modes_are_not_silently_repaired(tmp_path, mode):
    directory = pairing_directory(tmp_path)
    lock = directory.parent / LOCK
    lock.write_bytes(b"existing")
    lock.chmod(mode)
    try:
        with pytest.raises(PairingStorageError):
            PairingDirectoryOwner.acquire(directory)
        assert stat.S_IMODE(lock.stat().st_mode) == mode
    finally:
        lock.chmod(0o600)


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777, 0o2700])
def test_pairing_directory_requires_exact_private_mode(tmp_path, mode):
    directory = pairing_directory(tmp_path)
    directory.chmod(mode)
    try:
        with pytest.raises(PairingStorageError):
            PairingDirectoryOwner.acquire(directory)
        assert stat.S_IMODE(directory.stat().st_mode) == mode
    finally:
        directory.chmod(0o700)
    owner = PairingDirectoryOwner.acquire(directory)
    owner.close()  # Rejected opens did not retain the lock.


def test_symlink_directory_is_rejected_without_reading_or_chmodding_target(tmp_path):
    directory = pairing_directory(tmp_path)
    directory.rmdir()
    target = tmp_path / "external"
    target.mkdir(mode=0o700)
    (target / "private").write_bytes(b"unchanged")
    directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(PairingStorageError):
        PairingDirectoryOwner.acquire(directory)
    assert (target / "private").read_bytes() == b"unchanged"
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_private_parent_and_every_ancestor_must_be_real_directories(tmp_path):
    directory = pairing_directory(tmp_path)
    directory.parent.chmod(0o755)
    try:
        with pytest.raises(PairingStorageError):
            PairingDirectoryOwner.acquire(directory)
        assert not (directory.parent / LOCK).exists()
    finally:
        directory.parent.chmod(0o700)
    alias = tmp_path / "linked-parent"
    alias.symlink_to(directory.parent, target_is_directory=True)
    with pytest.raises(PairingStorageError):
        PairingDirectoryOwner.acquire(alias / "pairings")
    assert not (directory.parent / LOCK).exists()


def test_fresh_lock_is_private_even_with_fully_restrictive_umask(tmp_path):
    directory = pairing_directory(tmp_path)
    previous = os.umask(0o777)
    owner = None
    try:
        owner = PairingDirectoryOwner.acquire(directory)
        assert stat.S_IMODE(os.fstat(owner.lock_fd).st_mode) == 0o600
        assert stat.S_IMODE((directory.parent / LOCK).stat().st_mode) == 0o600
        assert not os.get_inheritable(owner.lock_fd)
        assert not os.get_inheritable(owner.directory_fd)
    finally:
        os.umask(previous)
        if owner is not None:
            owner.close()


def test_competing_owner_is_refused_until_idempotent_release(tmp_path):
    directory = pairing_directory(tmp_path)
    first = PairingDirectoryOwner.acquire(directory)
    try:
        with pytest.raises(PairingStorageError):
            PairingDirectoryOwner.acquire(directory)
    finally:
        first.close()
        first.close()
    assert first.directory_fd == first.lock_fd == -1
    replacement = PairingDirectoryOwner.acquire(directory)
    replacement.close()


def test_pinned_pairing_directory_survives_parent_rename_and_path_replacement(tmp_path):
    directory = pairing_directory(tmp_path)
    owner = PairingDirectoryOwner.acquire(directory)
    original_inode = os.fstat(owner.directory_fd).st_ino
    archived = tmp_path / "renamed-homekit"
    try:
        directory.parent.rename(archived)
        directory.parent.mkdir(mode=0o700)
        directory.mkdir(mode=0o700)
        descriptor = os.open(
            "proof", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=owner.directory_fd
        )
        try:
            os.write(descriptor, b"original pinned directory")
        finally:
            os.close(descriptor)
        assert (archived / "pairings/proof").read_bytes() == b"original pinned directory"
        assert not list(directory.iterdir())
        assert os.fstat(owner.directory_fd).st_ino == original_inode
        with pytest.raises(PairingStorageError):
            PairingDirectoryOwner.acquire(archived / "pairings")
    finally:
        owner.close()


CHILD = """
import json, os, stat, sys
pairing, lock = map(int, sys.argv[1:])
d, l = os.fstat(pairing), os.fstat(lock)
assert stat.S_ISDIR(d.st_mode) and stat.S_ISREG(l.st_mode)
assert stat.S_IMODE(d.st_mode) == 0o700 and stat.S_IMODE(l.st_mode) == 0o600
print(json.dumps({'directory_inode': d.st_ino, 'lock_inode': l.st_ino}), flush=True)
sys.stdin.buffer.read(1)
"""


def test_inherited_directory_and_lock_keep_exclusion_until_child_is_reaped(tmp_path):
    directory = pairing_directory(tmp_path)
    owner = PairingDirectoryOwner.acquire(directory)
    expected = {
        "directory_inode": os.fstat(owner.directory_fd).st_ino,
        "lock_inode": os.fstat(owner.lock_fd).st_ino,
    }
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", CHILD, str(owner.directory_fd), str(owner.lock_fd)],
        pass_fds=(owner.directory_fd, owner.lock_fd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        readable, _, _ = select.select([child.stdout], [], [], 2)
        assert readable, "inherited descriptor child did not initialize"
        assert json.loads(child.stdout.readline()) == expected
        owner.close()
        with pytest.raises(PairingStorageError):
            PairingDirectoryOwner.acquire(directory)
        stdout, stderr = child.communicate("q", timeout=2)
        assert child.returncode == 0, stderr
        assert stdout == ""
    finally:
        owner.close()
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=2)
    replacement = PairingDirectoryOwner.acquire(directory)
    replacement.close()


def test_repeated_rejected_lock_paths_do_not_leak_descriptors(tmp_path):
    directory = pairing_directory(tmp_path)
    target = tmp_path / "target"
    target.write_bytes(b"safe")
    (directory.parent / LOCK).symlink_to(target)
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(30):
        with pytest.raises(PairingStorageError):
            PairingDirectoryOwner.acquire(directory)
    assert len(os.listdir("/proc/self/fd")) == before
