"""Central logging configuration for the Halo Server process."""

from __future__ import annotations

import logging

from halo_server.log_redaction import RedactingFormatter

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: str) -> None:
    """Configure root logging with mandatory secret redaction."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(LOG_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)
