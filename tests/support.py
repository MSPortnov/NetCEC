import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, ROOT, os.path.join(ROOT, "py_modules")):  # tests/decky.py (stub) must win over a real decky module
    if p not in sys.path:
        sys.path.insert(0, p)

import main  # noqa: E402


class SettingsCase(unittest.TestCase):
    """Every test gets its own empty settings folder and a silent log."""

    def setUp(self):
        main._stop.clear()  # a test of the unload sets this flag; the real process ends after it
        self.dir = tempfile.mkdtemp(prefix="netcec-settings-")
        self._old = (main.SETTINGS_FILE, main._log)
        main.SETTINGS_FILE = os.path.join(self.dir, "settings.json")
        main._log = lambda msg: None
        self.addCleanup(self._restore)

    def _restore(self):
        main.SETTINGS_FILE, main._log = self._old
        shutil.rmtree(self.dir, ignore_errors=True)
