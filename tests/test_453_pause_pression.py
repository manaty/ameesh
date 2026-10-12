# SPDX-License-Identifier: AGPL-3.0-only
"""#453 — a heavy command has a duration cap, and a test suite a turn
launches outside `ameesh heavy` gets the same cap from the runner. Real
process groups; no database."""
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


def _state(pid: int) -> str | None:
    try:
        with open("/proc/%d/stat" % pid, encoding="ascii") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return None


def _wait(predicate, timeout=10.0) -> bool:
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _gone(pid: int) -> bool:
    return _state(pid) in (None, "Z")


class SuiteRecognitionTest(unittest.TestCase):
    def test_suites_on_the_executable_and_its_arguments(self):
        from ameesh.suite_cap import argv_of, is_heavy, is_suite
        for argv in (["node", "/x/node_modules/.bin/jest", "--runInBand"],
                     ["node", "--max-old-space-size=4096", "/x/node_modules/jest/bin/jest.js"],
                     ["npx", "vitest", "run"], ["python3", "-m", "pytest", "tests"],
                     ["python", "-m", "unittest", "tests.test_x"], ["/usr/bin/pytest", "-x"],
                     ["npm", "test"], ["npm", "run", "test"], ["pnpm", "test", "--", "-w", "2"],
                     ["yarn", "test:e2e"], ["node", "/usr/bin/npm", "test"],
                     ["pnpm", "exec", "vitest"], ["go", "test", "./..."], ["cargo", "test"],
                     ["/bin/sh", "/w/node_modules/.bin/jest"], argv_of(["npm test"]),
                     ["python", "-O", "-m", "unittest"], ["python3", "-OO", "-m", "pytest"],
                     ["npm", "--silent", "test"], ["npm", "--prefix=app", "test"],
                     ["deno", "test"], ["bun", "test"]):
            with self.subTest(argv=argv):
                self.assertTrue(is_suite(argv))
        for argv in (["rg", "pytest", "README.md"], ["python", "analysis.py", "--label", "jest"],
                     ["node", "server.js"], ["npm", "run", "build"], ["vim", "jest.config.js"],
                     ["python", "-m", "ameesh", "list"], ["git", "log", "--grep", "test"],
                     ["python3", "-c", "import pytest"], ["bash", "-c", "npm test"],
                     ["grep", "-r", "jest", "."], argv_of(["npm run build"]),
                     ["npm", "--prefix", "test", "install"],
                     ["npm", "--workspace", "test", "run", "build"],
                     ["npx", "-p", "jest", "eslint"], ["python", "-X", "pytest", "app.py"],
                     ["node", "-r", "jest", "server.js"], ["deno", "run", "test.ts"]):
            with self.subTest(argv=argv):
                self.assertFalse(is_suite(argv))
        for argv in (["ameesh", "heavy", "--", "npm", "test"],
                     ["/home/u/.local/bin/ameesh", "heavy", "--nice", "0", "--", "jest"],
                     ["python3", "-m", "ameesh", "heavy", "--", "pytest"],
                     ["/venv/bin/python3", "/home/u/.local/bin/ameesh", "heavy", "--", "jest"]):
            with self.subTest(argv=argv):
                self.assertTrue(is_heavy(argv))
        for argv in (["ameesh", "list"], ["rg", "ameesh heavy"], ["python", "heavy.py"]):
            with self.subTest(argv=argv):
                self.assertFalse(is_heavy(argv))


#: a "jest" suite: it starts a child that ignores SIGTERM (it outlives the
#: suite, reparented, unless SIGKILLed through its handle)
JEST = """#!/bin/sh
(trap '' TERM; exec sleep 120) &
echo $! > "$0.child.$$"
wait
"""
#: `ameesh heavy` stand-in: runs its command (after `heavy --`)
AMEESH = """#!/bin/sh
shift 2
"$@" &
wait
"""
#: a fake harness: a suite leading its own group, a suite in the harness's
#: group, a suite under `ameesh heavy`, and a script that only mentions jest
HARNESS = r"""
import subprocess, sys, time
d = sys.argv[1]
propre = subprocess.Popen([d + "/jest"], process_group=0)
dedans = subprocess.Popen([d + "/jest"])
lourd = subprocess.Popen([d + "/ameesh", "heavy", "--", d + "/jest"])
autre = subprocess.Popen([sys.executable, d + "/analysis.py", "--label", "jest"])
open(d + "/pids", "w").write("%d %d %d %d" % (propre.pid, dedans.pid, lourd.pid, autre.pid))
time.sleep(120)
"""


@unittest.skipUnless(os.path.isdir("/proc"), "Linux /proc")
class SuiteCapTest(unittest.TestCase):
    def setUp(self):
        from ameesh import suite_cap
        self.mod = suite_cap
        self.dir = d = tempfile.mkdtemp(prefix="ameesh-453-cap-")
        for name, text in (("jest", JEST), ("ameesh", AMEESH),
                           ("analysis.py", "import time\ntime.sleep(120)\n")):
            with open(os.path.join(d, name), "w") as fh:
                fh.write(text)
            os.chmod(os.path.join(d, name), 0o755)
        self.harness = subprocess.Popen([sys.executable, "-c", HARNESS, d],
                                        start_new_session=True)
        self.addCleanup(self._kill_all)
        self.assertTrue(_wait(lambda: os.path.exists(d + "/pids")
                              and os.path.getsize(d + "/pids") > 0))
        with open(d + "/pids") as fh:
            self.propre, self.dedans, self.lourd, self.autre = map(int, fh.read().split())
        self.assertTrue(_wait(lambda: len(self._children()) == 3))
        self.sent, self.notes = [], []
        worker = SimpleNamespace(proc=SimpleNamespace(pid=self.harness.pid), agent={},
                                 _fil_note=lambda text, meta=None: self.notes.append(text))
        self.runner = SimpleNamespace(workers={"a1": worker}, host="h1", db=None,
                                      cfg=SimpleNamespace(),
                                      background=SimpleNamespace(entries=[]))
        self.cap = suite_cap.SuiteCap(self.runner, log=lambda *_: None)

    def _children(self) -> dict[int, int]:
        """{pid of the jest process: pid of its TERM-ignoring child}"""
        out = {}
        for name in os.listdir(self.dir):
            if ".child." in name:
                with open(os.path.join(self.dir, name)) as fh:
                    text = fh.read().strip()
                if text:
                    out[int(name.rsplit(".", 1)[1])] = int(text)
        return out

    def _kill_all(self):
        import signal
        for pid in list(self._children().values()):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            os.killpg(self.harness.pid, signal.SIGKILL)
        except OSError:
            pass
        self.harness.wait()

    def _tick(self, *nows):
        with mock.patch.dict(os.environ, {"AMEESH_HEAVY_MAX_S": "1"}), \
                mock.patch("ameesh.mail.send",
                           side_effect=lambda *a, **k: self.sent.append(a)):
            return [self.cap.tick(now=n) for n in nows]

    def test_a_suite_outside_heavy_and_all_its_processes_are_stopped_past_the_cap(self):
        time.sleep(1.2)
        debut = time.time()
        stopped = self._tick(debut)[0]
        self.assertEqual(sorted(stopped), sorted([self.propre, self.dedans]))
        self.assertTrue(_wait(lambda: _gone(self.propre) and _gone(self.dedans)))
        enfants = self._children()
        survivants = [enfants[self.propre], enfants[self.dedans]]
        time.sleep(0.3)
        self.assertFalse(any(_gone(p) for p in survivants),
                         "they ignore SIGTERM, reparented once their suite is gone")
        self._tick(debut + self.mod.TERM_GRACE_S)
        self.assertTrue(_wait(lambda: all(_gone(p) for p in survivants)))
        self.assertEqual(self.cap.capped, {})
        # never under ameesh heavy (its own cap), never a script that names jest
        lourd = [p for j, p in enfants.items() if j not in (self.propre, self.dedans)]
        self.assertTrue(lourd and not _gone(lourd[0]))
        self.assertFalse(_gone(self.lourd))
        self.assertFalse(_gone(self.autre))
        self.assertFalse(_gone(self.harness.pid))
        self.assertEqual(self.sent[0][1:3], ("ameesh", "a1"))
        self.assertIn("Test suite stopped after", self.sent[0][3])
        self.assertIn("ameesh heavy", self.notes[-1])

    def test_under_the_cap_nothing_is_stopped(self):
        with mock.patch.dict(os.environ, {"AMEESH_HEAVY_MAX_S": "3600"}):
            self.assertEqual(self.cap.tick(), [])
        with mock.patch.dict(os.environ, {"AMEESH_HEAVY_MAX_S": "0"}):
            self.cap._at = float("-inf")
            self.assertEqual(self.cap.tick(), [])

    def test_the_runner_s_own_group_is_never_a_candidate(self):
        jest = subprocess.Popen([os.path.join(self.dir, "jest")])     # our own group
        self.addCleanup(lambda: (jest.kill(), jest.wait()))
        time.sleep(1.2)
        stopped = self._tick(time.time())[0]
        self.assertNotIn(jest.pid, stopped)
        self.assertFalse(_gone(jest.pid))


    def test_a_lock_the_suite_holds_never_delays_the_sigkill(self):
        import threading
        bloque = threading.Event()
        self.runner.workers["a1"]._fil_note = lambda text, meta=None: bloque.wait(30)
        self.addCleanup(bloque.set)
        time.sleep(1.2)
        debut = time.time()
        t0 = time.monotonic()
        self._tick(debut)
        self._tick(debut + self.mod.TERM_GRACE_S)
        self.assertLess(time.monotonic() - t0, 10)
        enfants = self._children()
        self.assertTrue(_wait(lambda: all(_gone(enfants[j]) for j in
                                          (self.propre, self.dedans))))

    def test_a_suite_adopted_by_the_runner_is_capped_too(self):
        """r4 #423 : an orphaned suite the runner adopted (subreaper) is one of
        its own children, outside its group."""
        jest = subprocess.Popen([os.path.join(self.dir, "jest")], start_new_session=True)
        self.addCleanup(lambda: (os.killpg(jest.pid, 9) if not _gone(jest.pid) else None,
                                 jest.wait()))
        time.sleep(1.2)
        self.assertIn(jest.pid, self._tick(time.time())[0])
        self.assertTrue(_wait(lambda: _gone(jest.pid)))

    def test_one_trace_thread_and_a_bounded_queue(self):
        """r4 #423 : a blocked note never piles up threads."""
        import threading
        bloque = threading.Event()
        self.runner.workers["a1"]._fil_note = lambda text, meta=None: bloque.wait(30)
        self.addCleanup(bloque.set)
        avant = set(threading.enumerate())
        for i in range(self.mod.TRACE_QUEUE + 20):
            self.cap._trace("a1", "trace %d" % i)
        time.sleep(0.2)
        tracers = [t for t in set(threading.enumerate()) - avant if t.name == "plafond-trace"]
        self.assertEqual(len(tracers), 1)
        self.assertGreaterEqual(self.cap.traces_dropped, 19)
        self.assertLessEqual(self.cap._traces.qsize(), self.mod.TRACE_QUEUE)

    def test_enosys_at_run_time_turns_the_cap_off_and_says_so_once(self):
        import errno
        logs = []
        self.cap.log = logs.append
        self.addCleanup(setattr, self.mod, "_PIDFD_ENOSYS", False)

        def enosys(pid):
            raise OSError(errno.ENOSYS, "pidfd_open")
        time.sleep(1.2)
        with mock.patch("os.pidfd_open", enosys):
            debut = time.time()
            self.assertEqual(self._tick(debut)[0], [])
            self.assertEqual(self._tick(debut + self.mod.CAP_EVERY_S)[0], [])
        self.assertEqual(sum("cap off" in line for line in logs), 1)
        self.assertFalse(_gone(self.propre))

    def test_nothing_is_signalled_without_pidfd(self):
        time.sleep(1.2)
        with mock.patch.object(self.mod, "pidfd_supported", lambda: False):
            self.assertEqual(self._tick(time.time())[0], [])
        self.assertFalse(_gone(self.propre))


@unittest.skipUnless(os.path.isdir("/proc"), "Linux /proc")
class HandleTest(unittest.TestCase):
    def test_a_handle_is_only_opened_on_the_process_seen_in_the_table(self):
        from ameesh import platform
        from ameesh.suite_cap import Handle
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        start = platform.start_time(proc.pid)
        self.assertIsNone(Handle.open(proc.pid, start + 0.5), "another process (reused PID)")
        self.assertIsNone(Handle.open(proc.pid, None))
        h = Handle.open(proc.pid, start)
        self.assertIsNotNone(h)
        self.assertTrue(h.send(0))
        proc.kill()
        proc.wait()
        self.assertFalse(h.send(0), "gone: the handle never reaches another process")
        h.close()

    def test_lineage_is_checked_live_up_to_the_runner(self):
        from ameesh import platform
        from ameesh.suite_cap import Handle, lineage
        me, group = os.getpid(), os.getpgrp()
        child = subprocess.Popen([sys.executable, "-c", "import subprocess, sys, time; "
                                  "subprocess.Popen([sys.executable, '-c', "
                                  "'import time; time.sleep(60)']); time.sleep(60)"],
                                 start_new_session=True)
        self.addCleanup(lambda: (os.killpg(child.pid, 9), child.wait()))
        self.assertTrue(_wait(lambda: any(
            p["ppid"] == child.pid for p in platform.process_table())))
        petit = next(p for p in platform.process_table() if p["ppid"] == child.pid)
        h = Handle.open(petit["pid"], petit["start"])
        self.addCleanup(h.close)
        chaine = lineage(h, me, group)
        self.assertEqual([pid for pid, _ in chaine], [child.pid])
        # not the runner's: another process stands for `me` (a PID never in the chain)
        self.assertIsNone(lineage(h, 2 ** 22 + 7, group))
        # through the runner's own group: refused
        self.assertIsNone(lineage(h, me, os.getpgid(child.pid)))
