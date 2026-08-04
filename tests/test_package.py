from halo_server import __version__
from halo_server.cli import main


def test_package_version_is_exposed():
    assert __version__ == "0.1.0"


def test_empty_cli_invocation_succeeds():
    assert main([]) == 0
