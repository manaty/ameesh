# SPDX-License-Identifier: AGPL-3.0-only
"""#453 — host pressure relieves the host: the tool processes of a paused
agent are frozen (SIGSTOP, SIGCONT on resume), and a heavy command has a
duration cap. Real process groups; no database for the heavy cap."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import heavy


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open("/proc/%d/stat" % pid, encoding="ascii") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


class HeavyCapTest(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(state_dir=tempfile.mkdtemp(prefix="ameesh-453-"),
                                   max_heavy_suites=1)
        env = mock.patch.dict(os.environ, {"AMEESH_MAX_HEAVY_SUITES": "",
                                           "AMEESH_HEAVY_MAX_S": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_durations(self):
        self.assertEqual([heavy.parse_duration(t) for t in ("90", "90s", "45m", "2h", "0")],
                         [90.0, 90.0, 2700.0, 7200.0, 0.0])
        for bad in ("", "abc", "-5", "inf"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                heavy.parse_duration(bad)
        self.assertEqual(heavy.max_duration(self.cfg), heavy.DEFAULT_MAX_S)
        self.cfg.heavy_max_seconds = "30m"
        self.assertEqual(heavy.max_duration(self.cfg), 1800.0)
        with mock.patch.dict(os.environ, {"AMEESH_HEAVY_MAX_S": "10m"}):
            self.assertEqual(heavy.max_duration(self.cfg), 600.0)
            self.assertEqual(heavy.max_duration(self.cfg, "0"), 0.0)

    def test_the_cap_stops_the_command_and_its_workers(self):
        pids = os.path.join(self.cfg.state_dir, "pids")
        script = ("import os, subprocess, sys, time\n"
                  "w = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                  "open(%r, 'w').write('%%d %%d' %% (os.getpid(), w.pid))\n"
                  "time.sleep(60)\n" % pids)
        debut = time.monotonic()
        with mock.patch("sys.stderr", new=__import__("io").StringIO()) as err:
            code = heavy.run(self.cfg, [sys.executable, "-c", script], nice=0, max_s=1.0)
        self.assertEqual(code, heavy.CAPPED_EXIT)
        self.assertLess(time.monotonic() - debut, 15)
        self.assertIn("duration cap reached (1 s)", err.getvalue())
        self.assertIn("--max-duration", err.getvalue())
        for pid in map(int, open(pids).read().split()):
            self.assertFalse(_alive(pid), "pid %d survives the cap" % pid)
        self.assertEqual([s["busy"] for s in heavy.status(self.cfg)], [False])

    def test_a_command_ignoring_sigterm_is_killed_after_the_grace(self):
        script = ("import signal, time\n"
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  "time.sleep(60)\n")
        with mock.patch.object(heavy, "GROUP_GRACE_S", 1.0), \
                mock.patch("sys.stderr", new=__import__("io").StringIO()):
            debut = time.monotonic()
            code = heavy.run(self.cfg, [sys.executable, "-c", script], nice=0, max_s=0.5)
        self.assertEqual(code, heavy.CAPPED_EXIT)
        self.assertLess(time.monotonic() - debut, 15)

    def test_no_cap_under_the_limit_or_when_disabled(self):
        self.assertEqual(heavy.run(self.cfg, [sys.executable, "-c", "raise SystemExit(3)"],
                                   nice=0, max_s=30.0), 3)
        self.assertEqual(heavy.run(self.cfg, [sys.executable, "-c", "import time; "
                                              "time.sleep(1.5)"], nice=0, max_s=0.0), 0)

    def test_cli_refuses_an_invalid_cap(self):
        out = subprocess.run([sys.executable, "-m", "ameesh", "heavy", "--max-duration",
                              "bientôt", "--", "true"], capture_output=True, text=True,
                             env=dict(os.environ, PYTHONPATH=os.path.join(
                                 os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "src")))
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIn("invalid --max-duration", out.stderr)
