# SPDX-License-Identifier: AGPL-3.0-only
"""#5029: a turn no longer reads the environment of the other executors.

The provider keys live in the initial environment of each `ameesh run`
executor; a process of the same user could read them in
`/proc/<pid>/environ` (incident cx2 #4944, 20 executors on agents-2). The
executor now makes itself non-dumpable (`PR_SET_DUMPABLE=0`) at startup: the
kernel refuses that read to its peers, while its own children still run.

The witness is the kernel's answer, not an internal flag: a process that
called `platform.become_undumpable()` — directly, or because it is the real
`runner.main` — has its `environ` (and `mem`) refused to the test process.
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ameesh import deps, platform, reprise
from ameesh import runner as runner_mod
from ameesh.config import Config

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (SRC, env.get("PYTHONPATH", "")) if p)
    return env


class UndumpableWitnessTest(unittest.TestCase):
    """The kernel refuses `/proc/<pid>/environ` of a hardened process."""

    def _start(self, script: str):
        workdir = tempfile.mkdtemp(prefix="l5029-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(shutil.rmtree, workdir, True)
        marker = os.path.join(workdir, "ready")
        proc = subprocess.Popen(
            [sys.executable, "-c", script, marker],
            env=dict(_env(), AMEESH_TEST_SECRET="une-valeur-de-test"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (proc.kill(), proc.wait(), proc.stdout.close(),
                                 proc.stderr.close()))
        deadline = time.monotonic() + 10
        while not os.path.exists(marker):
            if proc.poll() is not None:
                self.fail("helper exited before hardening: %s" % proc.stderr.read())
            if time.monotonic() > deadline:
                self.fail("helper did not become undumpable in time")
            time.sleep(0.02)
        return proc

    def _assert_refused(self, pid: int) -> None:
        with self.assertRaises(PermissionError):
            with open("/proc/%d/environ" % pid, "rb") as stream:
                stream.read()
        with self.assertRaises(PermissionError):
            with open("/proc/%d/mem" % pid, "rb"):
                pass

    @unittest.skipUnless(platform.is_linux(), "the /proc credential check is Linux-only")
    def test_a_hardened_process_hides_its_environment(self):
        proc = self._start(
            "import sys, time\n"
            "from ameesh import platform\n"
            "assert platform.become_undumpable()\n"
            "open(sys.argv[1], 'w').write('ready')\n"
            "time.sleep(30)\n")
        self._assert_refused(proc.pid)

    @unittest.skipUnless(platform.is_linux(), "the /proc credential check is Linux-only")
    def test_runner_main_hides_its_environment(self):
        """The real startup path, not only the platform primitive."""
        proc = self._start(
            "import sys, time\n"
            "from ameesh import host_gate, runner\n"
            "from ameesh.config import Config\n"
            "runner.load_config = lambda: Config()\n"
            "host_gate.gate_choice = lambda *a, **k: None\n"
            "def wait(*a, **k):\n"
            "    open(sys.argv[1], 'w').write('ready')\n"
            "    time.sleep(30)\n"
            "runner._connect_at_start = wait\n"
            "runner.main(['--agents', 'witness'])\n")
        self._assert_refused(proc.pid)

    @unittest.skipUnless(platform.is_linux(), "the /proc credential check is Linux-only")
    def test_the_executor_can_be_ignored_by_the_orphan_scan(self):
        """A non-dumpable executor is not an unread *turn* survivor.

        `settle_dead_turns` scans every same-user environment for a turn id.
        Without `allow`, a restart of the executor after a crash would keep an
        unfinished turn open forever (its doubt has no upper bound).
        """
        proc = self._start(
            "import sys, time\n"
            "from ameesh import platform\n"
            "assert platform.become_undumpable()\n"
            "open(sys.argv[1], 'w').write('ready')\n"
            "time.sleep(30)\n")
        start = platform.start_time(proc.pid)
        self.assertIsNotNone(start)
        # The interpreter's name is the host's: a venv runs `.../bin/python`
        # (comm "python"), the system one `python3`. `allow` matches `comm` and
        # one of the first argv entries, so take the real names from the live
        # process instead of naming one host's binary (#5029, CI red on the
        # head).
        with open("/proc/%d/comm" % proc.pid, encoding="utf-8") as fh:
            comm = fh.read().strip()
        with open("/proc/%d/cmdline" % proc.pid, "rb") as fh:
            argv0 = os.path.basename(
                fh.read().split(b"\0", 1)[0].decode("utf-8", "replace"))
        allow = tuple(dict.fromkeys(n for n in (comm, argv0) if n))
        self.assertTrue(allow, "the helper has no readable process name")
        table = [{"pid": proc.pid, "start": start, "zombie": False}]
        with mock.patch.object(platform.process, "_psutil", return_value=None), \
                mock.patch.object(platform.process, "process_table", return_value=table):
            without = platform.env_values("AMEESH_TEST_SECRET")
            allowed = platform.env_values("AMEESH_TEST_SECRET", allow=allow)
        self.assertIn(start, without["unread"])
        self.assertNotIn(start, allowed["unread"])

    @unittest.skipUnless(platform.is_linux(), "the /proc credential check is Linux-only")
    def test_a_shebang_executor_is_excused_by_its_bare_name(self):
        """The production executor is a script: comm=ameesh, argv[0]=python.

        It is launched by the shebang of the venv console script, so `argv[0]`
        is the interpreter and the script announces itself in `argv[1]`.
        Naming only `EXECUTOR_NAMES` must excuse it (#5029, B/P1 ameesh-dev7):
        otherwise `adopt`/`resume`, the orphan scan and every housekeeping
        doubt keep failing on a live executor.
        """
        workdir = tempfile.mkdtemp(prefix="l5029-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(shutil.rmtree, workdir, True)
        script = os.path.join(workdir, "ameesh")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write("#!%s\n" % sys.executable)
            fh.write("import sys, time\n"
                     "from ameesh import platform\n"
                     "assert platform.become_undumpable()\n"
                     "open(sys.argv[1], 'w').write('ready')\n"
                     "time.sleep(30)\n")
        os.chmod(script, 0o755)
        marker = os.path.join(workdir, "ready")
        proc = subprocess.Popen(
            [script, marker], env=dict(_env(), AMEESH_TEST_SECRET="une-valeur-de-test"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (proc.kill(), proc.wait(), proc.stdout.close(),
                                 proc.stderr.close()))
        deadline = time.monotonic() + 10
        while not os.path.exists(marker):
            if proc.poll() is not None:
                self.fail("shebang helper exited before hardening: %s" % proc.stderr.read())
            if time.monotonic() > deadline:
                self.fail("shebang helper did not become undumpable in time")
            time.sleep(0.02)
        self.assertTrue(platform.process._allowed_proc(proc.pid, platform.EXECUTOR_NAMES))
        start = platform.start_time(proc.pid)
        table = [{"pid": proc.pid, "start": start, "zombie": False}]
        with mock.patch.object(platform.process, "_psutil", return_value=None), \
                mock.patch.object(platform.process, "process_table", return_value=table):
            scan = platform.env_values("AMEESH_TEST_SECRET",
                                       allow=platform.EXECUTOR_NAMES)
        self.assertNotIn(start, scan["unread"])
        self.assertEqual(scan["values"], {})


class BecomeUndumpableTest(unittest.TestCase):
    def test_no_proc_is_a_no_op(self):
        """macOS/Windows: no /proc credential model, nothing to hide."""
        with mock.patch.object(platform.process, "_proc_ok", return_value=False):
            self.assertFalse(platform.become_undumpable())

    @unittest.skipUnless(platform.is_linux(), "the /proc credential check is Linux-only")
    def test_the_kernel_refusal_is_raised(self):
        """A refused prctl is an error, never a silent success."""
        libc = mock.Mock()
        libc.prctl.return_value = -1
        with mock.patch("ctypes.CDLL", return_value=libc), \
                mock.patch("ctypes.get_errno", return_value=1), \
                mock.patch.object(platform.process, "_proc_ok", return_value=True):
            with self.assertRaises(OSError):
                platform.become_undumpable()


class ExecutorStartupTest(unittest.TestCase):
    """`runner.main` hardens the executor before it can run a turn."""

    def test_main_calls_the_hardening_before_loading_anything(self):
        called = []
        with mock.patch.object(platform.process, "become_undumpable",
                               side_effect=lambda: called.append(True) or True), \
                mock.patch.object(runner_mod, "load_config",
                                  return_value=Config()) as load:
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    runner_mod.main(["--help"])
        self.assertEqual(called, [True])
        load.assert_called_once()

    def test_main_refuses_to_run_when_the_kernel_refuses(self):
        with mock.patch.object(platform.process, "become_undumpable",
                               side_effect=OSError(1, "operation not permitted")), \
                mock.patch.object(runner_mod, "load_config") as load:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = runner_mod.main(["--help"])
        self.assertEqual(code, 1)
        self.assertIn("refusing to run", stderr.getvalue())
        load.assert_not_called()

    def test_attach_calls_the_hardening_before_the_database(self):
        called = []
        with mock.patch.object(platform.process, "become_undumpable",
                               side_effect=lambda: called.append(True) or True), \
                mock.patch.object(runner_mod, "load_config", return_value=Config()), \
                mock.patch.object(runner_mod.adapters, "configure"), \
                mock.patch.object(runner_mod.db_mod, "connect",
                                  side_effect=runner_mod.db_mod.Unavailable("absent")):
            with contextlib.redirect_stderr(io.StringIO()):
                code = runner_mod.attach_main(["agent", "--max", "1h"])
        self.assertEqual(code, 1)
        self.assertEqual(called, [True])


class HolderChecksTest(unittest.TestCase):
    """#5029: a non-dumpable executor is not a doubt for a session or a cache.

    Adopting or resuming a session and collecting a dependency entry must not
    fail because the executor refuses the read of its own files; the executor
    never holds a session journal or a dependency file.
    """

    def test_session_holder_check_allows_the_executor(self):
        with mock.patch.object(platform, "holders", return_value=[]) as holders:
            reprise.holders("/tmp/session", "id")
        self.assertEqual(holders.call_args.kwargs.get("allow"), platform.EXECUTOR_NAMES)

    def test_dependency_holder_check_carries_the_host_allow(self):
        # #5046 : `_holders` reçoit désormais l'allow explicite de l'hôte
        # (`housekeeping.unreadable_allow`, qui inclut l'exécuteur) ; il ne le
        # fixe plus lui-même.
        with mock.patch.object(platform, "holders", return_value=[]) as holders:
            deps._holders("/tmp/cache-entry", allow=platform.EXECUTOR_NAMES)
        self.assertEqual(holders.call_args.kwargs.get("allow"), platform.EXECUTOR_NAMES)

    def test_housekeeping_allow_list_includes_the_executor(self):
        from ameesh import housekeeping
        allowed = housekeeping.unreadable_allow(
            housekeeping.Policy(unreadable_ok=("gpg-agent",)))
        self.assertEqual(allowed, ("gpg-agent",) + platform.EXECUTOR_NAMES)


if __name__ == "__main__":
    unittest.main()
