# SPDX-License-Identifier: AGPL-3.0-only
"""#3944 : une entrée inscrite en grâce après l'instantané de `tick` garde le
statut de son travail de fond face à la passe des orphelins."""
import os
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from . import support  # noqa: F401
from ameesh import background, platform


class GraceStraysTest(unittest.TestCase):
    @unittest.skipUnless(platform.is_linux() and hasattr(os, "fork"), "Linux children")
    def test_entry_started_after_snapshot_keeps_exit_code(self):
        runner = SimpleNamespace(db=None, host="h", harness_pids=set(),
                                 launch_lock=threading.Lock(), container_runtime=lambda: None)
        tracker = background.Tracker(runner, log=lambda _: None)
        lire, ecrire = os.pipe()
        # forké à la main, sans `Popen` : un adopté, moissonnable par la passe
        pid = os.fork()
        if pid == 0:
            os.close(ecrire)
            os.setpgid(0, 0)
            os.read(lire, 1)
            os._exit(3)
        os.close(lire)
        try:
            until = time.monotonic() + 5
            while os.getpgid(pid) != pid:
                self.assertLess(time.monotonic(), until)
                time.sleep(.01)
            row = next(p for p in platform.process_table() if p["pid"] == pid)
            suivis = []  # instantané de `tick`, pris avant l'inscription
            with mock.patch.object(background.storage, "of"):
                entry = tracker.start(agent="test", turn_id="apres-instantane", pgid=pid,
                                      members=[row], containers=[], grace_s=60, now=0,
                                      handles={})
            os.write(ecrire, b"x")
            until = time.monotonic() + 5
            while os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                self.assertLess(time.monotonic(), until)
                time.sleep(.01)
            table = [p for p in platform.process_table() if p["pid"] == pid]
            tracker._reap_strays(table, suivis)
            self.assertIsNotNone(os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT))
            tracker._refresh(entry, table, now=1)
            self.assertEqual(entry.jobs[pid].code, 3)
        finally:
            os.close(ecrire)
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass


if __name__ == "__main__":
    unittest.main()
