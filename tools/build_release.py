#!/usr/bin/env python3
"""Assemble an immutable, host-qualified Halo release entirely offline.

Inputs are a qualified application wheel, the prepared hash-locked wheelhouse,
a built adapter, the official pinned Node archive/signature/keyring, and an
already-populated npm cache. No lifecycle script or compiler runs here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from halo_server.release import (
    ENTRIES,
    READER_VERSION,
    canonical,
    host_qualification,
    verify_release,
)  # noqa: E402

NODE_ARCHIVE = "node-v26.12.1-linux-x64.tar.xz"
NODE_ARCHIVE_SHA256 = "6170b2d474e1c626f43cff3adc118df9fe3b573258a4fc3d850c8dbc6b079bbd"
NODE_KEYRING_SHA256 = "610b8d249da3d5733f5a128def2dd0294dbbf5b5713e6ca2529db8db419dee00"
NODE_SIGNER = "5BE8A3F6C8A5C01D106C0AD820B1A390B168D356"
NPM_ARCHIVE_SHA256 = "228bd64aaef23270dbdbd2cf3d5ef5c467602ef38c4066246cef1984adab9a67"
BOOTSTRAP = """import os,sys
from pathlib import Path
root = Path(__file__).absolute().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(root / "python/site-packages"))
from halo_server.release import managed_release
release = managed_release()
if release is None or release.root != root:
    raise SystemExit("Missing verified managed release")
arguments = sys.argv[1:]
if os.environ.get("HALO_RELEASE_OPERATOR") == "1":
    from halo_server.release_operator import main
    arguments = ["--state-directory", os.environ["HALO_RELEASE_STATE_DIRECTORY"], *arguments]
else:
    from halo_server.cli import main
    if arguments and not arguments[0].startswith("-"):
        arguments = [arguments[0], "--state-directory",
                     os.environ["HALO_RELEASE_STATE_DIRECTORY"], *arguments[1:]]
raise SystemExit(main(arguments))
"""


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(arguments, *, cwd=None, environment=None, timeout=180):
    return subprocess.run(
        [str(arg) for arg in arguments],
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def clean_environment():
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "SOURCE_DATE_EPOCH": "1793145600",
    }


def copy_files(source, target):
    if source.is_symlink() or not source.is_dir():
        raise ValueError("input directory is not a regular directory")
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("input tree contains symlink")
        relative = path.relative_to(source)
        if path.is_dir():
            (target / relative).mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        else:
            raise ValueError("input tree contains special file")


def node_inputs(source, work):
    archive, keyring = source / NODE_ARCHIVE, source / "nodejs-keyring.kbx"
    signed = source / "SHASUMS256.txt.asc"
    if digest(archive) != NODE_ARCHIVE_SHA256 or digest(keyring) != NODE_KEYRING_SHA256:
        raise ValueError("unqualified official Node input")
    home = work.parent / "gpg-home"
    home.mkdir(mode=0o700)
    result = run(
        ["/usr/bin/gpgv", "--homedir", home, "--keyring", keyring, "--status-fd", "1", signed]
    )
    if "[GNUPG:] VALIDSIG " + NODE_SIGNER + " " not in result.stdout:
        raise ValueError("Node signature signer mismatch")
    prefix = "node-v26.12.1-linux-x64/"
    with tarfile.open(archive, "r:xz") as package:
        for name, destination in [
            ("bin/node", work / "node/bin/node"),
            ("LICENSE", work / "notices/Node-LICENSE"),
        ]:
            member = package.getmember(prefix + name)
            if not member.isfile() or member.size > 256 * 1024 * 1024:
                raise ValueError("Node archive member invalid")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(package.extractfile(member).read())
    node = work / "node/bin/node"
    node.chmod(0o755)
    if run([node, "--version"]).stdout.strip() != "v26.12.1":
        raise ValueError("Node version mismatch")
    for item in (signed, keyring):
        shutil.copyfile(item, work / "notices" / item.name)
    return {
        "kind": "official-signed-archive",
        "version": "v26.12.1",
        "source": "https://nodejs.org/dist/v26.12.1/" + NODE_ARCHIVE,
        "archive_sha256": NODE_ARCHIVE_SHA256,
        "keyring_sha256": NODE_KEYRING_SHA256,
        "signed_checksums_sha256": digest(signed),
        "signer": NODE_SIGNER,
    }


def locked_requirements(wheelhouse):
    records = json.loads((wheelhouse / "wheel-inputs.json").read_text())
    if not isinstance(records.get("wheels"), list) or not 1 <= len(records["wheels"]) <= 256:
        raise ValueError("invalid wheel input count")
    names, requirements = set(), []
    for item in records["wheels"]:
        name, version, checksum = item["name"], item["version"], item["sha256"]
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is None
            or name in names
            or name == "halo-server"
            or not isinstance(version, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", version) is None
            or not isinstance(checksum, str)
            or re.fullmatch(r"[a-f0-9]{64}", checksum) is None
        ):
            raise ValueError("invalid locked wheel identity")
        path = wheelhouse / item["filename"]
        if (
            path.name != item["filename"]
            or path.suffix != ".whl"
            or path.is_symlink()
            or path.stat().st_size != item["size"]
            or digest(path) != checksum
        ):
            raise ValueError("wheelhouse differs from frozen input")
        names.add(name)
        requirements.append(f"{name}=={version} --hash=sha256:{checksum}\n")
    if not {"av", "pylibsrtp", "aiortc", "ring-doorbell", "textual"} <= names:
        raise ValueError("incomplete runtime wheelhouse")
    canonical_requirements = "".join(requirements)
    if (wheelhouse / "runtime-requirements.txt").read_text() != canonical_requirements:
        raise ValueError("noncanonical wheel requirements")
    return canonical_requirements


def install_python(wheel, wheelhouse, work):
    requirements = locked_requirements(wheelhouse)
    requirements += f"halo-server==0.1.0 --hash=sha256:{digest(wheel)}\n"
    locked = work / "notices/runtime-requirements.txt"
    locked.write_text(requirements)
    shutil.copyfile(wheelhouse / "wheel-inputs.json", work / "notices/wheel-inputs.json")
    site = work / ENTRIES["site_packages"]
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "--isolated",
            "install",
            "--no-index",
            "--only-binary=:all:",
            "--require-hashes",
            "--ignore-installed",
            "--no-compile",
            "--no-warn-script-location",
            "--find-links",
            wheelhouse,
            "--find-links",
            wheel.parent,
            "--target",
            site,
            "-r",
            locked,
        ],
        environment=clean_environment(),
    )
    # All application/maintenance entry points use the verified bootstrap.
    # Remove unused generated scripts with build-interpreter shebangs.
    shutil.rmtree(site / "bin", ignore_errors=True)
    shutil.copyfile(wheel, work / "notices" / wheel.name)
    # Import from the target only, without sitecustomize, ambient packages, or
    # LD_LIBRARY_PATH. Wheel-internal loader behavior must work as installed.
    run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            "import sys;sys.path.insert(0,sys.argv[1]);"
            "import av,pylibsrtp,numpy,aiortc,ring_doorbell,textual;"
            'assert av.__version__=="17.1.0";',
            site,
        ],
        environment=clean_environment(),
    )


def validate_adapter_entries(adapter):
    """Reject a stale camera-only build before creating a deployment artifact.

    Keep the existing manifest entry schema readable by installed launchers;
    all child files are already covered by the immutable file census and hashes.
    """
    for name in ("main.js", "device-main.js", "pairing-reset.js"):
        path = adapter / "dist/src" / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError("adapter build is missing a required production entry")


def install_adapter(adapter, npm_tool, npm_cache, work):
    validate_adapter_entries(adapter)
    npm_archive = npm_tool.parent / "npm-12.2.1.tgz"
    if digest(npm_archive) != NPM_ARCHIVE_SHA256:
        raise ValueError("npm archive differs from qualified registry input")
    expected_files = set()
    with tarfile.open(npm_archive, "r:gz") as archive:
        for member in archive:
            if member.isdir():
                continue
            relative = Path(member.name).relative_to("package")
            if not member.isfile() or ".." in relative.parts or member.size > 16 * 1024 * 1024:
                raise ValueError("unsafe npm tool member")
            target_file = npm_tool / relative
            if (
                target_file.is_symlink()
                or target_file.read_bytes() != archive.extractfile(member).read()
            ):
                raise ValueError("npm tool tree differs from qualified archive")
            expected_files.add(relative.as_posix())
    actual_files = {
        path.relative_to(npm_tool).as_posix() for path in npm_tool.rglob("*") if path.is_file()
    }
    if actual_files != expected_files or any(path.is_symlink() for path in npm_tool.rglob("*")):
        raise ValueError("unlisted npm tool files")
    target = work / "adapter"
    target.mkdir()
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(adapter / name, target / name)
    copy_files(adapter / "vendor", target / "vendor")
    copy_files(adapter / "dist/src", target / "dist/src")
    node = work / ENTRIES["node"]
    environment = clean_environment() | {"PATH": str(node.parent) + ":/usr/bin:/bin"}
    user_config, global_config = work.parent / "npm-userconfig", work.parent / "npm-globalconfig"
    user_config.write_text("")
    global_config.write_text("")
    environment.update(
        NPM_CONFIG_USERCONFIG=str(user_config), NPM_CONFIG_GLOBALCONFIG=str(global_config)
    )
    npm_version = run([node, npm_tool / "bin/npm-cli.js", "--version"]).stdout.strip()
    if npm_version != "12.2.1":
        raise ValueError("npm build toolchain mismatch")
    run(
        [
            node,
            npm_tool / "bin/npm-cli.js",
            "ci",
            "--offline",
            "--ignore-scripts",
            "--omit=dev",
            "--no-audit",
            "--no-fund",
            "--cache",
            npm_cache,
        ],
        cwd=target,
        environment=environment,
    )
    shutil.rmtree(target / "node_modules/.bin", ignore_errors=True)
    locked = json.loads((target / "package-lock.json").read_text())["packages"]
    installed = json.loads((target / "node_modules/.package-lock.json").read_text())["packages"]
    expected = {name for name, package in locked.items() if name and not package.get("dev", False)}
    if set(installed) != expected or any(
        installed[name]["version"] != locked[name]["version"] for name in expected
    ):
        raise ValueError("production Node dependency graph mismatch")
    for forbidden in ("typescript", "@types/node", "undici-types"):
        if (target / "node_modules" / forbidden).exists():
            raise ValueError("development Node dependency bundled")
    version = run(
        [
            node,
            "--input-type=module",
            "-e",
            "const h = await import(process.argv[1]); console.log(h.HAPLibraryVersion());",
            (target / "node_modules/@homebridge/hap-nodejs/dist/index.js").as_uri(),
        ],
        environment=environment,
    ).stdout.strip()
    return {
        "hap_version": version,
        "npm_version": npm_version,
        "package_lock_sha256": digest(target / "package-lock.json"),
        "production_packages": len(installed),
        "lifecycle_scripts": False,
        "npm_archive_sha256": NPM_ARCHIVE_SHA256,
        "npm_source": "https://registry.npmjs.org/npm/-/npm-12.2.1.tgz",
    }


def external_libraries(executables, *, internal_root=None):
    files = {}
    environment = clean_environment()
    if internal_root is not None:
        # Inspect vendored libraries independently of their importing extension.
        # This is only an audit aid; installed imports are tested without it.
        directories = sorted(
            {str(path.parent) for path in executables if path.is_relative_to(internal_root)}
        )
        environment["LD_LIBRARY_PATH"] = ":".join(directories)
    for executable in executables:
        output = run(["/usr/bin/ldd", executable], environment=environment).stdout
        if "not found" in output:
            missing = [line.strip() for line in output.splitlines() if "not found" in line]
            raise ValueError(f"missing host library for {executable.name}: {missing}")
        for line in output.splitlines():
            mapped = line.split("=>", 1)[-1].strip()
            match = re.match(r"(/[^\n]+?)\s+\(0x[0-9a-f]+\)", mapped)
            if match:
                path = Path(os.path.realpath(match.group(1)))
                if internal_root is not None and path.is_relative_to(internal_root):
                    continue
                files[str(path)] = {"path": str(path), "sha256": digest(path)}
    return [files[name] for name in sorted(files)]


def installer_source(package):
    verifier = (package / "release.py").read_text()
    guard = (package / "release_guard.py").read_text()
    return (
        '"""Trusted distribution first-install entry; verifies before importing bundled code."""\n'
        "import argparse,os,sys,types\nfrom pathlib import Path\n"
        "parser=argparse.ArgumentParser(add_help=False)\n"
        "parser.add_argument('--manifest-sha256',required=True)\n"
        "parser.add_argument('--state-directory',required=True,type=Path)\n"
        "options,arguments=parser.parse_known_args()\n"
        "if not options.state_directory.is_absolute():\n"
        "    raise SystemExit('State directory must be absolute')\n"
        "for name,source in "
        + repr([("release_guard", guard), ("halo_release_installer_verifier", verifier)])
        + ":\n"
        "    module=types.ModuleType(name)\n"
        "    sys.modules[name]=module\n"
        "    exec(compile(source,'<trusted-distribution-bootstrap>','exec'),module.__dict__)\n"
        "verifier=sys.modules['halo_release_installer_verifier']\n"
        "guard=sys.modules['release_guard'].ReleaseGuard.acquire(options.state_directory,create=True,nonblocking=True)\n"
        "try:\n"
        "    policy=options.state_directory/'release-policy.json'\n"
        "    if policy.exists() or policy.is_symlink():\n"
        "        value=verifier.read_protected_json(policy)\n"
        "        verifier._keys(value,{'version','revision','minimum_reader'})\n"
        "        if type(value['version']) is not int or value['version'] != 1:\n"
        "            raise SystemExit('Unsupported reader policy')\n"
        "        verifier._number(value['revision'])\n"
        "        floor=verifier._number(value['minimum_reader'])\n"
        "        if floor < 14: raise SystemExit('Invalid reader floor')\n"
        "    else:\n"
        "        enrolled=options.state_directory/'.release-policy.json.enrolled'\n"
        "        if enrolled.exists() or enrolled.is_symlink():\n"
        "            raise SystemExit('Enrolled reader policy missing')\n"
        "        floor=14\n"
        "    root=Path(os.path.abspath(__file__)).parent\n"
        "    verified=verifier.verify_release(root,options.manifest_sha256,minimum_reader=floor)\n"
        "    for key in tuple(os.environ):\n"
        "        if key.startswith('HALO_RELEASE_') or key in "
        "{'PYTHONPATH','PYTHONHOME','NODE_OPTIONS','NODE_PATH','LD_PRELOAD','LD_LIBRARY_PATH'}:\n"
        "            os.environ.pop(key,None)\n"
        "    os.environ.update(guard.environment())\n"
        "    os.environ['HALO_RELEASE_OPERATOR']='1'\n"
        "    sys.path.insert(0,str(verified.site_packages))\n"
        "    from halo_server.release_operator import main\n"
        "    if arguments[:1] == ['--']: arguments=arguments[1:]\n"
        "    arguments=['--state-directory',str(options.state_directory),*arguments,\n"
        "               '--manifest-sha256',options.manifest_sha256]\n"
        "    guard.descriptor=-1  # Transfer the descriptor to the dedicated operator.\n"
        "    raise SystemExit(main(arguments))\n"
        "finally:\n"
        "    # Operator closes its inherited descriptor before exclusive activation.\n"
        "    try: guard.close()\n"
        "    except OSError: pass\n"
    )


def seal(work, destination, provenance):
    interpreter = Path(os.path.realpath(sys.executable))
    (work / "bootstrap.py").write_text(BOOTSTRAP)
    package = work / ENTRIES["site_packages"] / "halo_server"
    (work / "install.py").write_text(installer_source(package))
    files = []
    for path in sorted(work.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("release contains unsafe file")
        if path.is_file():
            relative = path.relative_to(work).as_posix()
            mode = 0o555 if relative == ENTRIES["node"] else 0o444
            path.chmod(mode)
            files.append(
                {
                    "path": relative,
                    "size": path.stat().st_size,
                    "mode": mode,
                    "sha256": digest(path),
                }
            )
    elf_files = []
    for record in files:
        path = work / record["path"]
        with path.open("rb") as candidate:
            if candidate.read(4) == b"\x7fELF":
                elf_files.append(path)
    payload = {
        "reader_version": READER_VERSION,
        "launcher_version": 1,
        "qualification": host_qualification(),
        "interpreter": {"path": str(interpreter), "sha256": digest(interpreter)},
        "external_files": external_libraries([interpreter, *elf_files], internal_root=work),
        "entries": ENTRIES,
        "files": files,
        "provenance": provenance,
    }
    identity = hashlib.sha256(canonical(payload)).hexdigest()
    manifest = canonical({"schema": 1, "release_id": identity, "payload": payload})
    (work / "manifest.json").write_bytes(manifest)
    (work / "manifest.json").chmod(0o444)
    for path in sorted((p for p in work.rglob("*") if p.is_dir()), reverse=True):
        path.chmod(0o555)
    work.chmod(0o555)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / identity
    if target.exists():
        raise FileExistsError("release already exists; immutable output never overwritten")
    candidate = work.with_name(identity)
    os.rename(work, candidate)
    verified = verify_release(candidate, hashlib.sha256(manifest).hexdigest(), minimum_reader=14)
    # Linux requires write access to a moved directory when updating '..'. The
    # complete candidate has already verified; restore its readonly mode through
    # the held descriptor even if publication fails. No selection changes here.
    descriptor = os.open(candidate, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(descriptor, 0o755)
        os.rename(candidate, target)
    finally:
        os.fchmod(descriptor, 0o555)
        os.close(descriptor)
    return replace(verified, root=target)


def export_archive(release, directory):
    """Produce byte-reproducible distribution bytes and an external trust receipt."""
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / (release.release_id + ".tar.xz")
    with archive.open("xb") as output, tarfile.open(fileobj=output, mode="w:xz") as package:
        for path in [release.root, *sorted(release.root.rglob("*"))]:
            name = Path(release.release_id) / path.relative_to(release.root)
            info = package.gettarinfo(str(path), arcname=name.as_posix())
            if not (info.isdir() or info.isfile()):
                raise ValueError("unsafe archive entry")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 1793145600
            info.pax_headers = {}
            if info.isfile():
                with path.open("rb") as source:
                    package.addfile(info, source)
            else:
                package.addfile(info)
    receipt = {
        "schema": 1,
        "release_id": release.release_id,
        "manifest_sha256": release.manifest_sha256,
        "archive_sha256": digest(archive),
        "installer_sha256": digest(release.root / "install.py"),
        "qualification": release.payload["qualification"],
        "mutable_state_included": False,
    }
    with (directory / (release.release_id + ".receipt.json")).open("xb") as output:
        output.write(canonical(receipt))
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--node-inputs", type=Path, required=True)
    parser.add_argument("--npm-tool", type=Path, required=True)
    parser.add_argument("--npm-cache", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--archive-directory", type=Path)
    args = parser.parse_args()
    if digest(args.wheel) != args.wheel_sha256:
        raise ValueError("application wheel mismatch")
    args.destination = Path(os.path.abspath(args.destination))
    args.destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".halo-build-", dir=args.destination.parent) as scratch:
        work = Path(scratch) / "release"
        work.mkdir()
        provenance = {
            "builder": 1,
            "source_revision": args.source_revision,
            "python_wheel": {"filename": args.wheel.name, "sha256": digest(args.wheel)},
        }
        provenance["node"] = node_inputs(args.node_inputs.absolute(), work)
        install_python(args.wheel.absolute(), args.wheelhouse.absolute(), work)
        provenance["adapter"] = install_adapter(
            args.adapter.absolute(), args.npm_tool.absolute(), args.npm_cache.absolute(), work
        )
        result = seal(work, args.destination, provenance)
    if args.archive_directory is not None:
        export_archive(result, Path(os.path.abspath(args.archive_directory)))
    print(
        json.dumps(
            {
                "release_id": result.release_id,
                "manifest_sha256": result.manifest_sha256,
                "root": str(result.root),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
