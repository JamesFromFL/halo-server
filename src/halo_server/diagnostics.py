"""Read-only local health checks; never open account or pairing contents."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import json
import os
import signal
import stat
import sys
from typing import TYPE_CHECKING

from halo_server.media.manager import settle_owned
from halo_server.paths import PathSecurityError, ServerPaths, open_private_directory

if TYPE_CHECKING:
    from halo_server.homekit.bridge import AdapterResources

QUALIFIED = {
    "aiortc": "1.15.0",
    "av": "17.1.0",
    "pylibsrtp": "1.0.0",
    "ring-doorbell": "0.9.14",
}
QUALIFIED_NODE = "v26.12.1"
PROBE_OUTPUT_LIMIT = 4096
PROBE_TIMEOUT = 8.0
_NATIVE_PROBE = """
import json, os, resource
os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
resource.setrlimit(resource.RLIMIT_AS, (768*1024*1024, 768*1024*1024))
resource.setrlimit(resource.RLIMIT_CPU, (5,5))
import av, pylibsrtp
names = ('libx264','aac','libopus','mjpeg')
codecs = {name: av.codec.Codec(name, 'w').is_encoder for name in names}
assert all(codecs.values())
assert pylibsrtp.Policy.SRTP_PROFILE_AES128_CM_SHA1_80
print(json.dumps({'codecs': codecs}, separators=(',',':')))
"""


async def _probe(command: list[str]) -> bytes:
    """Bound both pipes, the deadline, and actual process ownership on every exit."""
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"LANG": "C.UTF-8", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
            start_new_session=True,
            limit=PROBE_OUTPUT_LIMIT + 1,
        )
    )
    try:
        process = await asyncio.shield(spawn)
    except BaseException:
        # Cancellation during process creation cannot transfer ownership to nobody.
        with contextlib.suppress(Exception):
            process = await settle_owned(spawn)
            await settle_owned(asyncio.create_task(_reap_probe(process)))
        raise

    async def read(stream):
        try:
            await stream.readexactly(PROBE_OUTPUT_LIMIT + 1)
        except asyncio.IncompleteReadError as error:
            return error.partial
        raise ValueError("diagnostic output limit")

    readers = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr))]
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            stdout, _ = await asyncio.gather(*readers)
            if await process.wait() != 0:
                raise ValueError("diagnostic worker failed")
            return stdout
    finally:
        await settle_owned(asyncio.create_task(_reap_probe(process)))
        for reader in readers:
            if not reader.done():
                reader.cancel()
        await settle_owned(asyncio.create_task(_join_readers(readers)))


async def _join_readers(readers):
    await asyncio.gather(*readers, return_exceptions=True)


async def _reap_probe(process):
    # A probe may inherit descendants; the isolated process group is its owner.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


async def local_doctor(
    paths: ServerPaths | None = None,
    *,
    probe: bool = False,
    resources: AdapterResources | None = None,
) -> dict:
    """Inspect only local metadata; explicit probes do no Ring, HAP or network IO.

    A passing local check does not qualify Apple interoperability. Pairing state,
    tokens, media, filesystem paths and environment contents are never returned.
    """
    if type(probe) is not bool:
        raise ValueError("probe must be boolean")
    paths = paths or ServerPaths.from_environment()
    checks = []
    versions = {"python": ".".join(map(str, sys.version_info[:3]))}

    def check(name, status, code):
        checks.append({"name": name, "status": status, "code": code})

    supported = sys.platform == "linux" and (3, 11) <= sys.version_info[:2] < (3, 15)
    check("platform", "pass" if supported else "fail", "qualified" if supported else "unsupported")
    for name, expected in QUALIFIED.items():
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
        good = versions[name] == expected
        check(name, "pass" if good else "fail", "qualified" if good else "version-mismatch")
    for name, path in (
        ("state-directory", paths.state_directory),
        ("runtime-directory", paths.runtime_directory),
        ("pairing-directory", paths.homekit_pairing_directory),
    ):
        try:
            path.lstat()
        except FileNotFoundError:
            check(name, "unverified", "not-created")
            continue
        except OSError:
            check(name, "fail", "unsafe-path")
            continue
        try:
            descriptor = open_private_directory(path)
        except (OSError, PathSecurityError):
            check(name, "fail", "unsafe-path")
        else:
            os.close(descriptor)
            check(name, "pass", "private")
    if resources is None:
        try:
            from halo_server.homekit.bridge import development_resources

            resources = development_resources()
        except (OSError, ValueError):
            pass
    available = resources is not None
    if available:
        try:
            for path in (resources.node, resources.entry):
                if not stat.S_ISREG(path.stat().st_mode):
                    raise ValueError("invalid resource")
            if not os.access(resources.node, os.X_OK):
                raise ValueError("invalid runtime")
        except (OSError, ValueError):
            available = False
    check(
        "adapter-resources",
        "pass" if available else "fail",
        "present" if available else "unavailable",
    )
    if probe:
        if available:
            try:
                version = (await _probe([str(resources.node), "--version"])).decode("ascii").strip()
                good = version == QUALIFIED_NODE
                versions["node"] = version if good else "unqualified"
                check(
                    "node-probe",
                    "pass" if good else "fail",
                    "qualified" if good else "version-mismatch",
                )
            except (OSError, ValueError, TimeoutError, UnicodeError):
                check("node-probe", "fail", "probe-failed")
        else:
            check("node-probe", "unverified", "unavailable")
        if supported:
            try:
                body = json.loads(await _probe([sys.executable, "-I", "-c", _NATIVE_PROBE]))
                good = body == {
                    "codecs": dict.fromkeys(("libx264", "aac", "libopus", "mjpeg"), True)
                }
                check(
                    "native-codec-probe",
                    "pass" if good else "fail",
                    "available" if good else "probe-failed",
                )
            except (OSError, ValueError, TimeoutError):
                check("native-codec-probe", "fail", "probe-failed")
        else:
            check("native-codec-probe", "unverified", "unsupported")
    return {
        "schema": 1,
        "kind": "local",
        "status": "ready" if all(c["status"] == "pass" for c in checks) else "degraded",
        "checks": checks,
        "versions": versions,
        "capabilities": {
            "adapter_resources_available": available,
            "real_ring_apple_qualified": False,
            "live_camera_probed": False,
        },
        "probe": probe,
    }
