"""Offline distribution entry verifies bytes and reader authority before imports."""

import hashlib
import importlib.util
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_release import make_release, writable_fixture_directories  # noqa: F401

from halo_server.release import canonical

BUILDER_PATH = Path(__file__).parents[1] / "tools/build_release.py"
spec = importlib.util.spec_from_file_location("release_builder_fixture", BUILDER_PATH)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def distribution(tmp_path):
    source = Path(__file__).parents[1] / "src/halo_server"
    operator = (
        b"import json,os\n"
        b"from pathlib import Path\n"
        b"def main(arguments):\n"
        b"    Path(os.environ['IMPORT_MARKER']).write_text('verified import')\n"
        b"    print(json.dumps({'arguments':arguments}))\n"
        b"    return 0\n"
    )
    extras = {
        "install.py": builder.installer_source(source).encode(),
        "python/site-packages/halo_server/__init__.py": b"",
        "python/site-packages/halo_server/release_operator.py": operator,
    }
    root, digest = make_release(tmp_path / "prefix with spaces", extra_files=extras)
    return root, digest


def invoke(root, digest, state, marker):
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(root / "install.py"),
            "--manifest-sha256",
            digest,
            "--state-directory",
            str(state),
            "--prefix",
            str(root.parent.parent),
            "activate",
            "--release-id",
            root.name,
            "--expected-revision",
            "0",
        ],
        env={"PATH": "", "IMPORT_MARKER": str(marker), "PYTHONPATH": "/untrusted"},
        cwd="/",
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_archive_only_first_install_entry_needs_no_policy_selection_or_source(tmp_path):
    root, digest = distribution(tmp_path)
    state, marker = tmp_path / "state", tmp_path / "imported"
    result = invoke(root, digest, state, marker)
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "verified import"
    arguments = json.loads(result.stdout)["arguments"]
    assert arguments[:2] == ["--state-directory", str(state)]
    assert arguments[-2:] == ["--manifest-sha256", digest]
    assert not (state / "release-policy.json").exists()
    assert not (root.parent.parent / "selection.json").exists()


@pytest.mark.parametrize("failure", ["wrong_hash", "higher_floor", "missing_enrolled_policy"])
def test_distribution_refuses_before_any_bundled_import(tmp_path, failure):
    root, digest = distribution(tmp_path)
    state, marker = tmp_path / "state", tmp_path / "imported"
    state.mkdir(mode=0o700)
    if failure == "higher_floor":
        policy = state / "release-policy.json"
        policy.write_bytes(canonical({"version": 1, "revision": 2, "minimum_reader": 15}))
        policy.chmod(0o600)
    elif failure == "missing_enrolled_policy":
        (state / ".release-policy.json.enrolled").write_bytes(b"{}")
    else:
        digest = "0" * 64
    result = invoke(root, digest, state, marker)
    assert result.returncode != 0 and not marker.exists()
    assert not result.stdout


def test_distribution_archives_are_byte_identical_and_contain_no_state(tmp_path):
    root, digest = distribution(tmp_path)
    release = SimpleNamespace(
        root=root,
        release_id=root.name,
        manifest_sha256=digest,
        payload={"qualification": {"fixture": True}},
    )
    first = builder.export_archive(release, tmp_path / "one")
    second = builder.export_archive(release, tmp_path / "two")
    assert first == second
    archive = tmp_path / "one" / (root.name + ".tar.xz")
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == first["archive_sha256"]
    with tarfile.open(archive, "r:xz") as source:
        assert all(member.uid == 0 and member.gid == 0 for member in source)
        assert all(member.name.startswith(root.name) for member in source.getmembers())
        assert not any("release-policy" in member.name for member in source.getmembers())


def test_host_library_audit_preserves_paths_containing_spaces(tmp_path, monkeypatch):
    library = tmp_path / "library directory" / "libfixture.so"
    library.parent.mkdir()
    library.write_bytes(b"fixture library")
    monkeypatch.setattr(
        builder,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=f"/mapped/name.so => {library} (0x00001234)\n"
        ),
    )
    result = builder.external_libraries([tmp_path / "program"])
    assert result == [
        {"path": str(library), "sha256": hashlib.sha256(library.read_bytes()).hexdigest()}
    ]


@pytest.mark.parametrize("injection", ["https://example.invalid/direct.whl", "-r remote.txt"])
def test_builder_rejects_requirement_directives_before_pip(tmp_path, injection):
    records = []
    for name in ["av", "pylibsrtp", "aiortc", "ring-doorbell", "textual"]:
        path = tmp_path / (name.replace("-", "_") + "-1.0.0-py3-none-any.whl")
        path.write_bytes(b"fixture wheel")
        records.append(
            {
                "name": name,
                "version": "1.0.0",
                "filename": path.name,
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    (tmp_path / "wheel-inputs.json").write_text(json.dumps({"wheels": records}))
    canonical_requirements = "".join(
        f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}\n" for item in records
    )
    requirements = tmp_path / "runtime-requirements.txt"
    requirements.write_text(canonical_requirements)
    assert builder.locked_requirements(tmp_path) == canonical_requirements
    requirements.write_text(canonical_requirements + injection + "\n")
    with pytest.raises(ValueError, match="noncanonical wheel requirements"):
        builder.locked_requirements(tmp_path)


def test_builder_disables_global_pip_remote_find_links(tmp_path, monkeypatch):
    configuration = pytest.importorskip("pip._internal.configuration")
    system_config = tmp_path / "system-pip.conf"
    system_config.write_text("[install]\nfind-links = https://example.invalid/network\n")
    monkeypatch.setattr(
        configuration,
        "get_configuration_files",
        lambda: {
            configuration.kinds.GLOBAL: [str(system_config)],
            configuration.kinds.USER: [],
            configuration.kinds.SITE: [],
        },
    )
    monkeypatch.setenv("PIP_CONFIG_FILE", builder.clean_environment()["PIP_CONFIG_FILE"])
    protected = configuration.Configuration(isolated=True)
    protected.load()
    assert dict(protected.items()) == {}
    monkeypatch.delenv("PIP_CONFIG_FILE")
    unprotected = configuration.Configuration(isolated=True)
    unprotected.load()
    assert unprotected.get_value("install.find-links") == "https://example.invalid/network"
