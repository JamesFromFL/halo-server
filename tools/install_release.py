#!/usr/bin/env python3
"""Audited first-install/recovery entry; stages no downloads or mutable backups.

Run with a supported host Python from a trusted Halo source/distribution.
This operator uses only the standard library. Normal upgrades use the verified
prefix/bin/halo-release entry.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from halo_server.release_operator import main

raise SystemExit(main())
