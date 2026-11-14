"""Compatibility import for the shared private owner client.

Alias the module so existing TUI callers and transport instrumentation retain the
same objects and timeout settings as the web companion.
"""

import sys

from halo_server import owner_client

sys.modules[__name__] = owner_client
