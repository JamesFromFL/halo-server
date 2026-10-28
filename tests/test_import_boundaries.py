import os
import subprocess
import sys
from pathlib import Path


def test_capture_and_service_import_without_any_gi_or_gstreamer_dependency():
    source = Path(__file__).resolve().parents[1] / "src"
    code = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'gi' or name.startswith('gi.'):
        raise AssertionError('direct capture imported GI')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import halo_server.media.consumers
import halo_server.media.timestamps
import halo_server.media.session
import halo_server.homekit.runtime
import halo_server.service
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=os.environ | {"PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
