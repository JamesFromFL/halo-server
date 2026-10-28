"""Synthetic child proofs for inherited pairing descriptors and cleanup barriers."""

import asyncio
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest
from test_homekit_runtime_dispatch import _child
from test_homekit_storage_owner import pairing_directory

from halo_server.homekit.runtime import HomeKitRuntime, HomeKitRuntimeError
from halo_server.homekit.storage_owner import PairingDirectoryOwner, PairingStorageError

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or NODE is None, reason="qualified Linux inherited descriptors and Node"
)


def child_entry(tmp_path, directory):
    entry = _child(tmp_path)
    inode = directory.stat().st_ino
    guard = f"""
const fs=require('node:fs');
const pairing=Number(process.env.HALO_PAIRING_FD);
const pairingLock=Number(process.env.HALO_PAIRING_LOCK_FD);
if(!Number.isSafeInteger(pairing)||!Number.isSafeInteger(pairingLock)||pairing===pairingLock)process.exit(90);
const d=fs.fstatSync(pairing), l=fs.fstatSync(pairingLock);
if(!d.isDirectory()||!l.isFile()||d.ino!=={json.dumps(inode)}||
  (d.mode&0o777)!==0o700||(l.mode&0o777)!==0o600||l.nlink!==1)process.exit(91);
fs.writeFileSync(`/proc/self/fd/${{pairing}}/child-proof`, 'pinned-private-child', {{mode:0o666}});
"""
    entry.write_text(guard + entry.read_text())
    return entry


async def no_retirement(_boot):
    pass


@pytest.mark.asyncio
async def test_runtime_passes_both_pinned_descriptors_and_secure_child_umask(tmp_path):
    directory = pairing_directory(tmp_path)
    retired = []
    runtime = None

    async def retirement(boot):
        assert runtime._process.returncode is not None
        with pytest.raises(PairingStorageError):
            PairingDirectoryOwner.acquire(directory)
        retired.append(boot)

    runtime = HomeKitRuntime(
        Path(NODE),
        child_entry(tmp_path, directory),
        build_id="pairing-inheritance",
        pairing_directory=directory,
        on_retired=retirement,
        max_restarts=0,
    )
    try:
        await asyncio.wait_for(runtime.start(), 3)
        boot, process = runtime.boot_id, runtime._process
        assert (directory / "child-proof").read_text() == "pinned-private-child"
        assert stat.S_IMODE((directory / "child-proof").stat().st_mode) == 0o600
        assert runtime._pairing_owner is not None
        with pytest.raises(PairingStorageError):
            PairingDirectoryOwner.acquire(directory)
    finally:
        await runtime.stop()
    assert process.returncode is not None and retired == [boot]
    assert runtime._pairing_owner is None
    replacement = PairingDirectoryOwner.acquire(directory)
    replacement.close()


@pytest.mark.asyncio
async def test_child_retains_inherited_flock_if_parent_descriptor_owner_closes(tmp_path):
    directory = pairing_directory(tmp_path)
    runtime = HomeKitRuntime(
        Path(NODE),
        child_entry(tmp_path, directory),
        build_id="child-retained-lock",
        pairing_directory=directory,
        on_retired=no_retirement,
        max_restarts=0,
    )
    try:
        await runtime.start()
        runtime._pairing_owner.close()
        with pytest.raises(PairingStorageError):
            PairingDirectoryOwner.acquire(directory)
        assert await runtime.request("ping", {}, expected="pong") == {}
    finally:
        await runtime.stop()
    replacement = PairingDirectoryOwner.acquire(directory)
    replacement.close()


@pytest.mark.asyncio
async def test_pairing_owner_survives_failed_retirement_callback_until_exact_boot_retry(tmp_path):
    directory = pairing_directory(tmp_path)
    attempts = []

    async def retirement(boot):
        attempts.append(boot)
        if len(attempts) == 1:
            raise RuntimeError("synthetic retirement failure")

    runtime = HomeKitRuntime(
        Path(NODE),
        child_entry(tmp_path, directory),
        build_id="pairing-retirement-barrier",
        pairing_directory=directory,
        on_retired=retirement,
        max_restarts=0,
    )
    try:
        await runtime.start()
        boot, process = runtime.boot_id, runtime._process
        process.kill()
        async with asyncio.timeout(2):
            while not runtime._supervisor.done():
                await asyncio.sleep(0.005)
        assert runtime.state == "failed" and runtime.boot_id == boot
        assert runtime._pairing_owner is not None
        with pytest.raises(PairingStorageError):
            PairingDirectoryOwner.acquire(directory)
        with pytest.raises(HomeKitRuntimeError):
            await runtime.start()
        await runtime.stop()
        assert attempts == [boot, boot]
        assert runtime._pairing_owner is None
    finally:
        await runtime.stop()
    replacement = PairingDirectoryOwner.acquire(directory)
    replacement.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["socketpair", "spawn"])
async def test_launch_failure_releases_pairing_lock_without_leaking_descriptors(
    tmp_path, monkeypatch, failure
):
    directory = pairing_directory(tmp_path)
    retired = []

    async def retirement(boot):
        retired.append(boot)

    runtime = HomeKitRuntime(
        Path(NODE),
        child_entry(tmp_path, directory),
        build_id="pairing-launch-failure",
        pairing_directory=directory,
        on_retired=retirement,
        max_restarts=0,
    )

    if failure == "spawn":

        async def fail_spawn(*_args, **_kwargs):
            raise OSError("synthetic spawn failure")

        monkeypatch.setattr(
            "halo_server.homekit.runtime.asyncio.create_subprocess_exec", fail_spawn
        )
    else:

        def fail_pair(*_args, **_kwargs):
            raise OSError("synthetic private socket failure")

        monkeypatch.setattr("halo_server.homekit.runtime.socket.socketpair", fail_pair)
    before = len(os.listdir("/proc/self/fd"))
    try:
        with pytest.raises(HomeKitRuntimeError):
            await asyncio.wait_for(runtime.start(), 1)
        assert runtime.state == "failed" and runtime.pid is None
        assert runtime._pairing_owner is None
        assert len(retired) == (1 if failure == "spawn" else 0)
    finally:
        await runtime.stop()
    assert len(os.listdir("/proc/self/fd")) == before
    replacement = PairingDirectoryOwner.acquire(directory)
    replacement.close()


@pytest.mark.asyncio
async def test_cancelled_start_joins_late_child_before_releasing_pairing_owner(
    tmp_path, monkeypatch
):
    directory = pairing_directory(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    spawned = []
    real_spawn = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        child = await real_spawn(*args, **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr("halo_server.homekit.runtime.asyncio.create_subprocess_exec", delayed_spawn)
    runtime = HomeKitRuntime(
        Path(NODE),
        child_entry(tmp_path, directory),
        build_id="pairing-late-spawn",
        pairing_directory=directory,
        on_retired=no_retirement,
        max_restarts=0,
    )
    starting = asyncio.create_task(runtime.start())
    try:
        await entered.wait()
        with pytest.raises(PairingStorageError):
            PairingDirectoryOwner.acquire(directory)
        starting.cancel()
        await asyncio.sleep(0)
        assert not starting.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(starting, 2)
        assert len(spawned) == 1 and spawned[0].returncode is not None
        assert runtime._pairing_owner is None
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)
        await runtime.stop()
    replacement = PairingDirectoryOwner.acquire(directory)
    replacement.close()


@pytest.mark.asyncio
async def test_failed_pairing_close_keeps_exact_boot_and_owner_until_retry(tmp_path, monkeypatch):
    directory = pairing_directory(tmp_path)
    runtime = HomeKitRuntime(
        Path(NODE),
        child_entry(tmp_path, directory),
        build_id="pairing-close-retry",
        pairing_directory=directory,
        on_retired=no_retirement,
        max_restarts=0,
    )
    attempts = []
    try:
        await runtime.start()
        boot = runtime.boot_id
        owner = runtime._pairing_owner
        real_close = owner.close

        def close_once_failed():
            attempts.append(True)
            if len(attempts) == 1:
                raise OSError("synthetic descriptor retirement failure")
            real_close()

        monkeypatch.setattr(owner, "close", close_once_failed)
        runtime._process.kill()
        async with asyncio.timeout(2):
            while not runtime._supervisor.done():
                await asyncio.sleep(0.005)
        assert runtime.state == "failed" and runtime.retirement_pending
        assert runtime.boot_id == boot and runtime._pairing_owner is owner
        with pytest.raises(PairingStorageError):
            PairingDirectoryOwner.acquire(directory)
        await runtime.stop()
        assert len(attempts) == 2
        assert runtime.boot_id is None and runtime._pairing_owner is None
        assert not runtime.retirement_pending
    finally:
        await runtime.stop()
    replacement = PairingDirectoryOwner.acquire(directory)
    replacement.close()


@pytest.mark.asyncio
async def test_preboot_pairing_cleanup_failure_blocks_start_and_is_retryable(tmp_path, monkeypatch):
    directory = pairing_directory(tmp_path)
    real_acquire = PairingDirectoryOwner.acquire
    attempts = []

    def acquire_with_failed_close(path):
        owner = real_acquire(path)
        real_close = owner.close

        def close_twice_failed():
            attempts.append(True)
            if len(attempts) <= 2:
                raise OSError("synthetic preboot owner retirement failure")
            real_close()

        monkeypatch.setattr(owner, "close", close_twice_failed)
        return owner

    def fail_pair(*_args, **_kwargs):
        raise OSError("synthetic preboot socket failure")

    monkeypatch.setattr(
        "halo_server.homekit.runtime.PairingDirectoryOwner.acquire", acquire_with_failed_close
    )
    monkeypatch.setattr("halo_server.homekit.runtime.socket.socketpair", fail_pair)
    runtime = HomeKitRuntime(
        Path(NODE),
        child_entry(tmp_path, directory),
        build_id="pairing-preboot-barrier",
        pairing_directory=directory,
        on_retired=no_retirement,
        max_restarts=0,
    )
    try:
        with pytest.raises(HomeKitRuntimeError):
            await runtime.start()
        assert runtime.boot_id is None and runtime.retirement_pending
        with pytest.raises(PairingStorageError):
            real_acquire(directory)
        with pytest.raises(OSError):
            await runtime.stop()
        assert runtime._supervisor is None
        assert runtime.retirement_pending and runtime.boot_id is None
        with pytest.raises(HomeKitRuntimeError, match="retirement is incomplete"):
            await runtime.start()
        await runtime.stop()
        assert len(attempts) == 3 and not runtime.retirement_pending
    finally:
        await runtime.stop()
    replacement = real_acquire(directory)
    replacement.close()
