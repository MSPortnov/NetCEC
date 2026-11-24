"""Stand-in for Decky's `decky` module, so main.py can be imported and tested anywhere."""
import logging
import tempfile

DECKY_PLUGIN_SETTINGS_DIR = tempfile.mkdtemp(prefix="netcec-test-")
logger = logging.getLogger("netcec-test")


async def emit(*args, **kwargs):
    pass
