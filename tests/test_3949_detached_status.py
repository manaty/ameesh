# SPDX-License-Identifier: AGPL-3.0-only
"""#3949 : un enfant détaché, capturé avant la fin du tour par la sonde du
sous-moissonneur, garde son code de sortie quand la passe des orphelins le
moissonne entre `survivors` et `start`.

Le témoin est déterministe : sous-moissonneur réel, enfant adopté, `setsid`
hors du groupe du tour, `cgroup=None`, instantané de `tick` (`suivis=[]`)
pris avant l'inscription de l'entrée ; seule l'observation retenue par
`capture_before_wait` peut encore rendre le statut.
"""
import os
import select
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


class DetachedStatusTest(unittest.TestCase):
    @unittest.skipUnless(platform.is_linux() and hasattr(os, "fork"), "Linux children")
    def test_enfant_detache_capture_garde_son_code_de_sortie(self):
        runner = SimpleNamespace(db=None, host="h", harness_pids=set(),
                                 launch_lock=threading.Lock(),
                                 container_runtime=lambda: None)
        tracker = background.Tracker(runner, log=lambda _: None)
        self.assertTrue(tracker.subreaper)
        saut_lu, saut_ecrit = os.pipe()     # test -> enfant : quitte le groupe
        sortie_lu, sortie_ecrit = os.pipe()  # enfant -> test : setsid fait
        fin_lu, fin_ecrit = os.pipe()        # test -> enfant : sort avec 3
        # Le harnais fork son enfant dans SON groupe (nouvelle session) puis
        # meurt sans le moissonner : l'enfant est adopté par nous.
        parent = subprocess.Popen(
            [sys.executable, "-c", """
import os, sys
pid = os.fork()
if pid == 0:
    os.read(int(sys.argv[1]), 1)
    os.setsid()
    os.write(int(sys.argv[2]), b"x")
    os.read(int(sys.argv[3]), 1)
    os._exit(3)
print(pid, flush=True)
os._exit(0)
""", str(saut_lu), str(sortie_ecrit), str(fin_lu)],
            pass_fds=(saut_lu, sortie_ecrit, fin_lu),
            stdout=subprocess.PIPE, text=True, start_new_session=True)
        os.close(saut_lu)
        os.close(sortie_ecrit)
        os.close(fin_lu)
        pid = None
        entry = None
        held = {}
        try:
            self.assertTrue(select.select([parent.stdout], [], [], 5)[0])
            pid = int(parent.stdout.readline())
            until = time.monotonic() + 5
            while os.getpgid(pid) != parent.pid:
                self.assertLess(time.monotonic(), until)
                time.sleep(.01)
            # Le chef est mort non moissonné : la sonde fixe le groupe avant
            # que Popen ne puisse le recycler, et capture l'enfant dedans.
            until = time.monotonic() + 5
            while os.waitid(os.P_PID, parent.pid,
                            os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                self.assertLess(time.monotonic(), until)
                time.sleep(.01)
            held = tracker.capture_before_wait(parent)
            self.assertIn(pid, held)
            self.assertEqual(parent.wait(timeout=5), 0)
            # L'enfant quitte le groupe : `survivors` ne le verra plus que par
            # son handle, et `start` ne pourra plus le recapturer par pgid.
            os.write(saut_ecrit, b"x")
            self.assertTrue(select.select([sortie_lu], [], [], 5)[0])
            self.assertEqual(os.read(sortie_lu, 1), b"x")
            until = time.monotonic() + 5
            while os.getpgid(pid) != pid:
                self.assertLess(time.monotonic(), until)
                time.sleep(.01)
            row = next(p for p in platform.process_table() if p["pid"] == pid)
            self.assertEqual(row["ppid"], os.getpid())
            tracker.SETTLE_S = 0
            survivors = tracker.survivors("detache", parent.pid, handles=held)
            self.assertIsNotNone(survivors)
            self.assertIn(pid, [p["pid"] for p in survivors[0]])
            # L'enfant meurt avant l'inscription de l'entrée ; la passe des
            # orphelins de `tick` a figé ses suivis avant `start`.
            os.write(fin_ecrit, b"x")
            until = time.monotonic() + 5
            while os.waitid(os.P_PID, pid,
                            os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                self.assertLess(time.monotonic(), until)
                time.sleep(.01)
            table = [p for p in platform.process_table() if p["pid"] == pid]
            self.assertTrue(table and table[0]["zombie"])
            tracker._reap_strays(table, [])
            with mock.patch.object(background.storage, "of"):
                entry = tracker.start(agent="test", turn_id="detache",
                                      pgid=parent.pid, members=survivors[0],
                                      containers=survivors[1], grace_s=60, now=0,
                                      handles=held)
            self.assertIsNone(entry.cgroup)
            tracker._refresh(entry, table, now=1)
            self.assertIn(pid, entry.jobs)
            self.assertEqual(entry.jobs[pid].code, 3,
                             "le statut de l'enfant détaché a été consommé par la passe des orphelins")
        finally:
            for fd in (saut_ecrit, fin_ecrit, sortie_lu):
                try:
                    os.close(fd)
                except OSError:
                    pass
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
                except ProcessLookupError:
                    pass
                except ChildProcessError:
                    pass
            for handle in held.values():
                handle.close()
            if entry is not None:
                for handle in entry.handles.values():
                    handle.close()


if __name__ == "__main__":
    unittest.main()
