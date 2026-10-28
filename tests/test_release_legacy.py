"""Synthetic Unix peer/process evidence; no systemd or real daemon actions."""

import asyncio
import os
import socket
import struct
import sys
from contextlib import asynccontextmanager

import pytest

from halo_server import release_legacy as module
from halo_server.release_activation import ActivationError
from halo_server.release_legacy import capture_owner, verify_stopped
from halo_server.tui.client import AdminClient, AdminClientError

CHILD = r"""
import asyncio,json,os,sys
from pathlib import Path
async def serve(reader,writer):
 try:
  line=await reader.readline()
  if line:
   request=json.loads(line)
   writer.write((json.dumps({'version':1,'id':request['id'],'ok':True,
    'result':{'service':'running'}})+'\n').encode());await writer.drain()
 finally:
  writer.close();await writer.wait_closed()
async def main():
 path=Path(sys.argv[1]);server=await asyncio.start_unix_server(serve,path=path)
 path.chmod(0o600);print('ready',flush=True)
 reader=asyncio.StreamReader()
 await asyncio.get_running_loop().connect_read_pipe(
  lambda:asyncio.StreamReaderProtocol(reader),sys.stdin)
 while line:=await reader.readline():
  if line==b'unlink\n':
   server.close();await server.wait_closed();path.unlink(missing_ok=True)
   print('unlinked',flush=True)
  elif line==b'exit\n':break
 server.close();await server.wait_closed()
asyncio.run(main())
"""


@asynccontextmanager
async def peer(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "admin.sock"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        CHILD,
        str(path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 3) == b"ready\n"
        yield AdminClient(path, timeout=1), process
    finally:
        if process.returncode is None:
            process.stdin.write(b"exit\n")
            await process.stdin.drain()
        await asyncio.wait_for(process.wait(), 3)
        assert process.returncode == 0, await process.stderr.read()


async def unlink(process):
    process.stdin.write(b"unlink\n")
    await process.stdin.drain()
    assert await asyncio.wait_for(process.stdout.readline(), 3) == b"unlinked\n"


@pytest.mark.asyncio
async def test_real_owner_pidfd_survives_socket_disappearance_until_actual_exit(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(module, "EXIT_SECONDS", 0.03)
    async with peer(tmp_path) as (client, process):
        owner = await capture_owner(client)
        assert owner is not None and owner.pid == process.pid
        assert not os.get_inheritable(owner.descriptor)
        try:
            assert (await client.status())["service"] == "running"
            with pytest.raises(ActivationError) as error:
                await verify_stopped(client, owner)
            assert error.value.code == "active_unmanaged"
            await unlink(process)
            with pytest.raises(ActivationError) as error:
                await verify_stopped(client, owner)
            assert error.value.code == "active_unmanaged" and process.returncode is None
            process.stdin.write(b"exit\n")
            await process.stdin.drain()
            await process.wait()
            await verify_stopped(client, owner)
        finally:
            descriptor = owner.descriptor
            owner.close()
            owner.close()
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.asyncio
async def test_actual_exit_wait_joins_peer_and_never_signals_manual_process(tmp_path):
    async with peer(tmp_path) as (client, process):
        owner = await capture_owner(client)
        try:
            await unlink(process)
            checking = asyncio.create_task(verify_stopped(client, owner))
            await asyncio.sleep(0.02)
            assert not checking.done() and process.returncode is None
            process.stdin.write(b"exit\n")
            await process.stdin.drain()
            await asyncio.wait_for(checking, 1)
        finally:
            owner.close()


@pytest.mark.asyncio
async def test_cancelled_exit_wait_removes_reader_and_retains_caller_owned_pidfd(
    tmp_path, monkeypatch
):
    async with peer(tmp_path) as (client, process):
        owner = await capture_owner(client)
        loop = asyncio.get_running_loop()
        removed, original = [], loop.remove_reader

        def remove(descriptor):
            removed.append(descriptor)
            return original(descriptor)

        monkeypatch.setattr(loop, "remove_reader", remove)
        try:
            await unlink(process)
            checking = asyncio.create_task(verify_stopped(client, owner))
            await asyncio.sleep(0.02)
            checking.cancel()
            with pytest.raises(asyncio.CancelledError):
                await checking
            assert owner.descriptor in removed
            assert os.fstat(owner.descriptor) and process.returncode is None
        finally:
            owner.close()


class Client:
    def __init__(self, code=None):
        self.code = code

    async def status(self):
        if self.code is not None:
            raise AdminClientError(self.code, "synthetic")
        return {"service": "stopped"}


@pytest.mark.asyncio
async def test_only_unavailable_status_accepts_even_without_os_owner_evidence():
    assert await capture_owner(Client()) is None
    await verify_stopped(Client("daemon_unavailable"), None)
    with pytest.raises(ActivationError) as error:
        await verify_stopped(Client(), None)
    assert error.value.code == "active_unmanaged"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["bad_response", "request_failed", "forbidden", "unavailable"])
async def test_ambiguous_admin_errors_cannot_prove_retirement(code):
    with pytest.raises(ActivationError) as error:
        await verify_stopped(Client(code), None)
    assert error.value.code == "stop_unverified"


@pytest.mark.asyncio
async def test_unresponsive_poststop_status_has_a_finite_deadline(monkeypatch):
    monkeypatch.setattr(module, "PROBE_SECONDS", 0.02)

    class Stalled:
        async def status(self):
            await asyncio.Event().wait()

    with pytest.raises(ActivationError) as error:
        await verify_stopped(Stalled(), None)
    assert error.value.code == "stop_unverified"


@pytest.mark.asyncio
async def test_capture_missing_socket_or_directory_is_absent_without_creating_it(tmp_path):
    path = tmp_path / "not-created" / "admin.sock"
    assert await capture_owner(AdminClient(path)) is None
    assert not path.parent.exists()
    path.parent.mkdir(mode=0o700)
    assert await capture_owner(AdminClient(path)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "symlink", "public-directory", "public-socket"])
async def test_capture_rejects_unsafe_paths_before_connecting(tmp_path, kind):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "admin.sock"
    raw = None
    try:
        if kind == "file":
            path.write_text("preserved")
        elif kind == "symlink":
            path.symlink_to(tmp_path / "outside")
        else:
            raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            raw.bind(str(path))
            path.chmod(0o666 if kind == "public-socket" else 0o600)
            if kind == "public-directory":
                directory.chmod(0o755)
        with pytest.raises(ActivationError) as error:
            await capture_owner(AdminClient(path))
        assert error.value.code == "stop_unverified"
        if kind == "file":
            assert path.read_text() == "preserved"
    finally:
        if raw:
            raw.close()


@pytest.mark.asyncio
async def test_capture_refused_private_socket_does_not_fabricate_owner(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "admin.sock"
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.bind(str(path))
    path.chmod(0o600)
    raw.close()
    assert await capture_owner(AdminClient(path)) is None


def fake_peer(tmp_path, monkeypatch, *, pid, uid):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "admin.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
        raw.bind(str(path))
        path.chmod(0o600)

    class Peer:
        closed = False

        def setblocking(self, _value):
            pass

        def getsockopt(self, _level, _option, _size):
            return struct.pack("3i", pid, uid, os.getgid())

        def close(self):
            self.closed = True

    observed = Peer()
    monkeypatch.setattr(module.socket, "socket", lambda *_args: observed)
    return path, observed


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_uid", [True, False])
async def test_untrusted_or_self_peer_never_opens_pidfd(tmp_path, monkeypatch, wrong_uid):
    path, observed = fake_peer(
        tmp_path,
        monkeypatch,
        pid=os.getpid(),
        uid=os.getuid() + (1 if wrong_uid else 0),
    )

    async def connect(_peer, _path):
        return None

    def no_pidfd(_pid):
        raise AssertionError("an untrusted peer must not gain owner evidence")

    monkeypatch.setattr(asyncio.get_running_loop(), "sock_connect", connect)
    monkeypatch.setattr(os, "pidfd_open", no_pidfd)
    with pytest.raises(ActivationError) as error:
        await capture_owner(AdminClient(path))
    assert error.value.code == "stop_unverified" and observed.closed


@pytest.mark.asyncio
async def test_capture_cancellation_closes_socket_and_pinned_directory(tmp_path, monkeypatch):
    path, observed = fake_peer(tmp_path, monkeypatch, pid=os.getpid(), uid=os.getuid())
    entered = asyncio.Event()
    descriptors = []

    async def connect(_peer, address):
        descriptors.append(int(address.split("/")[4]))
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio.get_running_loop(), "sock_connect", connect)
    task = asyncio.create_task(capture_owner(AdminClient(path)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert observed.closed and len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
