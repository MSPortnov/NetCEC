import asyncio
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from support import SettingsCase, main

PAIRED = {"ip": "192.168.1.95", "auth_cookie": "auth=x"}
LINUX = os.path.isdir("/proc/self") and shutil.which("bash") and shutil.which("sleep")

INHIBIT = """#!/bin/bash
# imitates systemd-inhibit: runs the command as a CHILD process and waits for it
echo $$ >> "$PIDS"
while [[ "$1" == --* ]]; do shift; done
"$@" &
echo $! >> "$PIDS"
wait $!
"""


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"  # a zombie is dead
    except OSError:
        return False


@unittest.skipUnless(LINUX, "needs Linux (/proc) and bash")
class SleepLock(SettingsCase):
    def setUp(self):
        super().setUp()
        self.bin = tempfile.mkdtemp(prefix="netcec-bin-")
        self.pids = os.path.join(self.bin, "pids")
        stub = os.path.join(self.bin, "systemd-inhibit")
        with open(stub, "w") as f:
            f.write(INHIBIT)
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
        env = mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ["PATH"], "PIDS": self.pids})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.bin, True)
        main._save({**main.DEFAULTS, **PAIRED})

    def started(self):
        with open(self.pids) as f:
            return [int(x) for x in f.read().split()]

    def test_quick_repeated_calls_start_only_one_lock_and_unload_leaves_nothing_behind(self):
        async def scenario():
            plugin = main.Plugin()
            await asyncio.gather(*[plugin._take_lock() for _ in range(5)])
            await asyncio.sleep(0.4)
            pids = self.started()
            self.assertEqual(len(pids), 2, "one systemd-inhibit and its sleep child")
            self.assertTrue(all(alive(p) for p in pids))
            await plugin._unload()
            await asyncio.sleep(0.5)
            return pids

        pids = asyncio.run(scenario())
        self.assertFalse([p for p in pids if alive(p)], "no process may survive the unload")

    def test_no_lock_without_a_paired_tv(self):
        main._save({**main.DEFAULTS})  # sleep and shutdown always act, but there is no TV to act on

        async def scenario():
            plugin = main.Plugin()
            await plugin._take_lock()
            return plugin._lock

        self.assertIsNone(asyncio.run(scenario()))
        self.assertFalse(os.path.exists(self.pids))

    def test_the_lock_follows_the_pairing(self):
        main._save({**main.DEFAULTS})

        async def scenario():
            plugin = main.Plugin()
            await plugin._take_lock()
            assert plugin._lock is None
            main._update(lambda s: s.update(**PAIRED))
            await plugin._take_lock()  # what pair_finish does after a successful pairing
            taken = plugin._lock is not None
            await plugin.tv_unpair()
            return taken, plugin._lock

        taken, after_unpair = asyncio.run(scenario())
        self.assertTrue(taken)
        self.assertIsNone(after_unpair)


if __name__ == "__main__":
    unittest.main()
