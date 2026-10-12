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


#: a fake harness: a session leader with a shell running a tool (which starts
#: a command in a process group of its own, like `ameesh heavy`), and a
#: non-shell child (an MCP server); writes the pids it started
HARNESS = r"""
import os, subprocess, sys, time
out = sys.argv[1]
mcp = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
time.sleep(float(sys.argv[2]))        # the harness's start-up, then the tool
tool = subprocess.Popen(["bash", "-c", "exec python3 -c 'import subprocess, sys, time; "
                         "c = subprocess.Popen([sys.executable, \"-c\", \"import time; "
                         "time.sleep(120)\"], process_group=0); "
                         "open(sys.argv[1], \"w\").write(str(c.pid)); time.sleep(120)' %s.heavy"
                         % out])
open(out, "w").write("%d %d" % (tool.pid, mcp.pid))
time.sleep(120)
"""


@unittest.skipUnless(os.path.isdir("/proc"), "Linux /proc")
class FreezeTest(unittest.TestCase):
    def setUp(self):
        from ameesh import pressure_freeze
        self.mod = pressure_freeze
        self.dir = tempfile.mkdtemp(prefix="ameesh-453-gel-")
        out = os.path.join(self.dir, "pids")
        patch = mock.patch.object(pressure_freeze, "HARNESS_STARTUP_S", 0.5)
        patch.start()
        self.addCleanup(patch.stop)
        self.harness = subprocess.Popen([sys.executable, "-c", HARNESS, out, "1.0"],
                                        start_new_session=True)
        self.addCleanup(self._kill_all)
        self.assertTrue(_wait(lambda: os.path.exists(out + ".heavy")
                              and os.path.exists(out) and open(out).read()))
        with open(out) as fh:
            self.tool, self.mcp = map(int, fh.read().split())
        self.assertTrue(_wait(lambda: open(out + ".heavy").read().strip()))
        with open(out + ".heavy") as fh:
            self.heavy = int(fh.read())
        self.verdict = {"blocked": True, "critical": True, "breaches": []}
        self.notes: list = []
        self.worker = SimpleNamespace(
            proc=SimpleNamespace(pid=self.harness.pid), agent={"priority": 0},
            _held_by_pressure=lambda v: bool(v.get("blocked")),
            _fil_note=lambda text, meta=None: self.notes.append((text, meta)))
        self.runner = SimpleNamespace(
            workers={"a1": self.worker}, host="h1", db=None, cfg=SimpleNamespace(),
            background=SimpleNamespace(entries=[]),
            host_pressure=lambda: dict(self.verdict))
        self.logs: list = []
        self.freezer = self.mod.Freezer(self.runner, log=self.logs.append)

    def _kill_all(self):
        import signal
        for pid in (getattr(self, "heavy", None), getattr(self, "tool", None),
                    getattr(self, "mcp", None)):
            if pid:
                for sig in (signal.SIGCONT, signal.SIGKILL):
                    try:
                        os.kill(pid, sig)
                    except OSError:
                        pass
        self.harness.kill()
        self.harness.wait()

    def test_turn_tools_spare_the_harness_and_its_mcp_server(self):
        from ameesh import platform
        tools = self.mod.turn_tools(platform.process_table(), self.harness.pid)
        self.assertIn(self.tool, tools)
        self.assertIn(self.heavy, tools, "its own process group, still a tool of the turn")
        self.assertNotIn(self.harness.pid, tools)
        self.assertNotIn(self.mcp, tools)

    def test_freeze_then_resume_when_the_pressure_goes_down(self):
        with mock.patch.dict(os.environ, {"AMEESH_PRESSURE_FREEZE_MAX_S": ""}):
            self.freezer.tick()
            self.assertTrue(_wait(lambda: _state(self.tool) == "T"
                                  and _state(self.heavy) == "T"))
            self.assertNotEqual(_state(self.harness.pid), "T")
            self.assertNotEqual(_state(self.mcp), "T")
            self.assertIn("frozen (SIGSTOP)", self.notes[-1][0])
            self.verdict = {"blocked": True, "critical": False, "breaches": []}
            self.freezer.tick()
            self.assertTrue(_wait(lambda: _state(self.tool) != "T"
                                  and _state(self.heavy) != "T"))
            self.assertIn("resumed (SIGCONT)", self.notes[-1][0])
            self.assertEqual(self.freezer.frozen, {})

    def test_not_frozen_when_the_agent_is_not_paused(self):
        self.worker.agent = {"priority": 1}       # not paused: it only waits
        self.freezer.tick()
        time.sleep(0.3)
        self.assertNotEqual(_state(self.tool), "T")
        self.worker.agent = {"priority": 0}
        self.worker._held_by_pressure = lambda v: False    # orchestrator, auditor
        self.freezer.tick()
        time.sleep(0.3)
        self.assertNotEqual(_state(self.tool), "T")

    def test_frozen_too_long_the_tools_are_stopped_and_the_agent_told(self):
        sent = []
        with mock.patch.dict(os.environ, {"AMEESH_PRESSURE_FREEZE_MAX_S": "60"}), \
                mock.patch("ameesh.mail.send", side_effect=lambda *a, **k: sent.append(a)):
            debut = time.time()
            self.freezer.tick(now=debut)
            self.assertTrue(_wait(lambda: _state(self.tool) == "T"))
            self.freezer.tick(now=debut + 61)
            self.freezer.tick(now=debut + 61 + self.mod.TERM_GRACE_S)
            self.assertTrue(_wait(lambda: _state(self.heavy) in (None, "Z")))
            self.assertTrue(_wait(lambda: _state(self.tool) in (None, "Z")))
        self.assertEqual(sent[0][1:3], ("ameesh", "a1"))
        self.assertIn("stopped (SIGTERM, then SIGKILL)", sent[0][3])
        self.assertTrue(_state(self.harness.pid) not in (None, "Z", "T"))

    def test_background_work_in_grace_is_frozen_too(self):
        self.runner.workers = {"a1": SimpleNamespace(
            proc=None, agent={"priority": 0}, _held_by_pressure=lambda v: True,
            _fil_note=lambda text, meta=None: None)}
        self.runner.background.entries = [SimpleNamespace(
            state="running", pgid=self.harness.pid, lineage={self.harness.pid},
            agent="a1", turn_id="t" * 16)]
        self.freezer.tick()
        self.assertTrue(_wait(lambda: _state(self.heavy) == "T"
                              and _state(self.mcp) == "T"))
        self.assertEqual(self.freezer.thaw_all("test"), len(self.freezer.frozen) or 4)
        self.assertTrue(_wait(lambda: _state(self.heavy) != "T"))


class TestCommandTest(unittest.TestCase):
    def test_recognised_suites(self):
        from ameesh.pressure_freeze import HEAVY_COMMAND, TEST_COMMAND
        for line in ("node /x/node_modules/.bin/jest --runInBand", "npx vitest run",
                     "python3 -m pytest tests", "python -m unittest tests.test_x",
                     "npm test", "npm run test", "pnpm test -- -w 2", "yarn test:e2e",
                     "go test ./...", "cargo test", "/usr/bin/pytest -x"):
            with self.subTest(line=line):
                self.assertTrue(TEST_COMMAND.search(line))
        for line in ("node server.js", "npm run build", "vim jest.config.js",
                     "python -m ameesh list", "git log --grep test"):
            with self.subTest(line=line):
                self.assertFalse(TEST_COMMAND.search(line))
        for line in ("ameesh heavy -- npm test", "/home/u/.local/bin/ameesh heavy --nice 0 "
                     "-- jest", "python3 -m ameesh heavy -- pytest"):
            with self.subTest(line=line):
                self.assertTrue(HEAVY_COMMAND.search(line))


#: a fake harness whose tool is a "jest" suite, and one under `ameesh heavy`
SUITES = r"""
import subprocess, sys, time
out = sys.argv[1]
time.sleep(float(sys.argv[2]))
seule = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "jest"])
heavy = subprocess.Popen(["bash", "-c", "%s -c 'import subprocess, sys, time; "
                          "subprocess.Popen([sys.executable, \"-c\", \"import time; "
                          "time.sleep(120)\", \"jest\"]); time.sleep(120)' ameesh heavy --"
                          % sys.executable])
open(out, "w").write("%d %d" % (seule.pid, heavy.pid))
time.sleep(120)
"""


@unittest.skipUnless(os.path.isdir("/proc"), "Linux /proc")
class TestCapTest(unittest.TestCase):
    def test_a_suite_outside_heavy_is_stopped_past_the_cap(self):
        from ameesh import pressure_freeze
        out = os.path.join(tempfile.mkdtemp(prefix="ameesh-453-cap-"), "pids")
        harness = subprocess.Popen([sys.executable, "-c", SUITES, out, "0.6"],
                                   start_new_session=True)
        self.addCleanup(lambda: (os.killpg(harness.pid, 9), harness.wait()))
        self.assertTrue(_wait(lambda: os.path.exists(out) and open(out).read()))
        with open(out) as fh:
            seule, sous_heavy = map(int, fh.read().split())
        sent, notes = [], []
        worker = SimpleNamespace(proc=SimpleNamespace(pid=harness.pid), agent={},
                                 _held_by_pressure=lambda v: False,
                                 _fil_note=lambda text, meta=None: notes.append(text))
        runner = SimpleNamespace(workers={"a1": worker}, host="h1", db=None,
                                 cfg=SimpleNamespace(), background=SimpleNamespace(entries=[]),
                                 host_pressure=lambda: {})
        freezer = pressure_freeze.Freezer(runner, log=lambda *_: None)
        time.sleep(1.2)
        with mock.patch.dict(os.environ, {"AMEESH_HEAVY_MAX_S": "1"}), \
                mock.patch.object(pressure_freeze, "HARNESS_STARTUP_S", 0.3), \
                mock.patch("ameesh.mail.send", side_effect=lambda *a, **k: sent.append(a)):
            freezer.tick()
            self.assertTrue(_wait(lambda: _state(seule) in (None, "Z")))
        self.assertTrue(_state(sous_heavy) not in (None, "Z"), "ameesh heavy has its own cap")
        self.assertEqual(sent[0][1:3], ("ameesh", "a1"))
        self.assertIn("Test suite stopped after", sent[0][3])
        self.assertIn("ameesh heavy", notes[-1])
