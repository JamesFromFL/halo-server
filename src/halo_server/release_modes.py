"""Explicit initial mode for the managed distribution without system GI imports."""

from __future__ import annotations

import os
from dataclasses import replace

from halo_server.configuration import ConfigurationError, ConfigurationStore, ServerMode
from halo_server.release_activation import ActivationError


class ManagedModeStore(ConfigurationStore):
    """Initialize only absent configuration; preserve every saved owner decision."""

    def prepare(self, initial_mode=None):
        return self._prepare(initial_mode, initialize=True)

    def preflight(self, initial_mode=None):
        """Refuse an obvious unsupported choice before stopping a working service."""
        return self._prepare(initial_mode, initialize=False)

    def _prepare(self, initial_mode, *, initialize):
        if initial_mode is not None and initial_mode != "homekit":
            raise ActivationError("Initial managed mode is invalid", code="bad_parameters")
        try:
            with self._transaction() as directory:
                try:
                    os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
                    present = True
                except FileNotFoundError:
                    present = False
                current = self._read_locked(directory)
                if present:
                    if current.compatibility_enabled:
                        raise ActivationError(
                            "Managed releases require a saved homekit or ring-only mode; "
                            "change compatibility or dual mode through the existing owner "
                            "administration before migration",
                            code="managed_mode_unsupported",
                        )
                    return current
                if initial_mode is None:
                    raise ActivationError(
                        "Fresh managed state requires explicit --initial-mode homekit",
                        code="initial_mode_required",
                    )
                if not initialize:
                    return self._validated(replace(current, mode=ServerMode.HOMEKIT))
                self._check_revision(current, 0)
                return self._save_locked(
                    directory, current, self._validated(replace(current, mode=ServerMode.HOMEKIT))
                )
        except ConfigurationError as error:
            raise ActivationError(
                "Managed initial configuration could not be saved safely",
                code="configuration_unavailable",
                possibly_saved=error.possibly_saved,
            ) from error
