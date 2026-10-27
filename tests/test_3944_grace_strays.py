# SPDX-License-Identifier: AGPL-3.0-only
"""#3944 : une entrée inscrite en grâce après l'instantané de `tick` garde le
statut de son travail de fond face à la passe des orphelins."""
import os
import signal
import subprocess
import sys
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
        self.assertTrue(tracker.subreaper)
        lire, ecrire = os.pipe()
        # Le parent intermédiaire meurt : son enfant est réellement adopté
        # par le sous-moissonneur, sans cgroup ni propriétaire Popen.
        parent = subprocess.Popen(
            [sys.executable, "-c", """
import os, sys
pid = os.fork()
if pid == 0:
    os.setpgid(0, 0)
    os.read(int(sys.argv[1]), 1)
    os._exit(3)
print(pid, flush=True)
os._exit(0)
""", str(lire)], pass_fds=(lire,), stdout=subprocess.PIPE, text=True)
        os.close(lire)
        pid = None
        entry = None
        try:
            pid = int(parent.stdout.readline())
            self.assertEqual(parent.wait(timeout=5), 0)
            until = time.monotonic() + 5
            while os.getpgid(pid) != pid:
                self.assertLess(time.monotonic(), until)
                time.sleep(.01)
            row = next(p for p in platform.process_table() if p["pid"] == pid)
            self.assertEqual(row["ppid"], os.getpid())
            suivis = []  # instantané de `tick`, pris avant l'inscription
            with mock.patch.object(background.storage, "of"):
                entry = tracker.start(agent="test", turn_id="apres-instantane", pgid=pid,
                                      members=[row], containers=[], grace_s=60, now=0,
                                      handles={})
            self.assertIsNone(entry.cgroup)
            os.write(ecrire, b"x")
            until = time.monotonic() + 5
            while os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                self.assertLess(time.monotonic(), until)
                time.sleep(.01)
            table = [p for p in platform.process_table() if p["pid"] == pid]
            tracker._reap_strays(table, suivis)
            try:
                status = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                self.fail("stray pass consumed the adopted background job's exit status")
            self.assertIsNotNone(status)
            tracker._refresh(entry, table, now=1)
            self.assertEqual(entry.jobs[pid].code, 3)
        finally:
            os.close(ecrire)
            parent.stdout.close()
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
            if pid is not None:
                try:
                    waited, _ = os.waitpid(pid, os.WNOHANG)
                    if waited == 0:
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
            if entry is not None:
                for handle in entry.handles.values():
                    handle.close()


if __name__ == "__main__":
    unittest.main()
