"""Require fresh prebuilt browser assets; builds never download dependencies."""

import importlib.util
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        path = root / "src/halo_server/web_assets.py"
        spec = importlib.util.spec_from_file_location("halo_web_build_validation", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.validate_bundle(root / "src/halo_server/web/static", source=root / "web-ui")
