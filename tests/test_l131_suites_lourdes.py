# SPDX-License-Identifier: AGPL-3.0-only
"""L131 — heavy suites capped per host (`ameesh heavy`). Pure tests: slots
live in a temporary state directory, no database."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import adapters, heavy

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


class SlotsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(state_dir=tempfile.mkdtemp(prefix="ameesh-l131-"),
                                   max_heavy_suites=2)
        env = mock.patch.dict(os.environ, {"AMEESH_MAX_HEAVY_SUITES": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_limit_from_config_then_cpus(self):
        self.assertEqual(heavy.limit(self.cfg), 2)
        with mock.patch.object(heavy.os, "cpu_count", return_value=12):
            self.assertEqual(heavy.limit(SimpleNamespace(max_heavy_suites=0)), 3)
        with mock.patch.object(heavy.os, "cpu_count", return_value=2):
            self.assertEqual(heavy.limit(SimpleNamespace(max_heavy_suites=0)), 1)

    def test_no_more_than_the_limit_at_once(self):
        first = heavy.acquire(self.cfg, command="jest")
        second = heavy.acquire(self.cfg, command="make build")
        self.assertEqual({first[0], second[0]}, {0, 1})
        logs = []
        third = heavy.acquire(self.cfg, command="pytest", log=logs.append,
                              sleep=lambda s: None, max_wait_s=0)
        self.assertIsNone(third)
        busy = heavy.status(self.cfg)
        self.assertEqual([s["busy"] for s in busy], [True, True])
        self.assertEqual({s["command"] for s in busy}, {"jest", "make build"})
        heavy.release(first[1])
        again = heavy.acquire(self.cfg, command="pytest", max_wait_s=0)
        self.assertIsNotNone(again)
        heavy.release(second[1])
        heavy.release(again[1])
        self.assertEqual([s["busy"] for s in heavy.status(self.cfg)], [False, False])

    def test_waiting_is_logged_with_the_holders(self):
        held = [heavy.acquire(self.cfg, command="jest --all") for _ in range(2)]
        logs, clock = [], iter(range(0, 1000, 30))
        self.assertIsNone(heavy.acquire(self.cfg, log=logs.append, sleep=lambda s: None,
                                        clock=lambda: next(clock), max_wait_s=100))
        self.assertTrue(logs)
        self.assertIn("2/2 heavy suites running", logs[0])
        self.assertIn("jest --all", logs[0])
        for _, fh in held:
            heavy.release(fh)

    def test_a_killed_holder_frees_its_slot(self):
        # the slot is a flock: the kernel drops it with the process
        env = dict(os.environ, PYTHONPATH=SRC, AMEESH_MAX_HEAVY_SUITES="1")
        code = ("import sys, types, time; from ameesh import heavy; "
                "cfg = types.SimpleNamespace(state_dir=sys.argv[1], max_heavy_suites=1); "
                "held = heavy.acquire(cfg, command='holder'); print('held', flush=True); "
                "time.sleep(60)")
        proc = subprocess.Popen([sys.executable, "-c", code, self.cfg.state_dir],
                                stdout=subprocess.PIPE, text=True, env=env)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "held")
            one = SimpleNamespace(state_dir=self.cfg.state_dir, max_heavy_suites=1)
            self.assertIsNone(heavy.acquire(one, max_wait_s=0))
            proc.kill()
            proc.wait(timeout=10)
            got = heavy.acquire(one, max_wait_s=0)
            self.assertIsNotNone(got)
            heavy.release(got[1])
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()

    def test_run_passes_the_exit_code_and_frees_the_slot(self):
        self.assertEqual(heavy.run(self.cfg, [sys.executable, "-c", "raise SystemExit(3)"],
                                   nice=0), 3)
        self.assertEqual(heavy.run(self.cfg, ["ameesh-no-such-command"], nice=0), 127)
        self.assertEqual([s["busy"] for s in heavy.status(self.cfg)], [False, False])


class RunChildTest(unittest.TestCase):
    """`heavy.run` through a real wrapper and a real command (codex review r1,
    P2 `heavy.py:138`): the slot lives as long as the command."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="ameesh-l131-run-")
        self.one = SimpleNamespace(state_dir=self.state, max_heavy_suites=1)
        env = mock.patch.dict(os.environ, {"AMEESH_MAX_HEAVY_SUITES": ""})
        env.start()
        self.addCleanup(env.stop)

    def _wrapper(self, child: str = "import os, time; print(os.getpid(), flush=True); "
                                    "time.sleep(60)", *, grace: float = 10.0, pids: int = 1):
        """The wrapper process and the pid(s) its command prints, once started."""
        env = dict(os.environ, PYTHONPATH=SRC, AMEESH_MAX_HEAVY_SUITES="1")
        code = ("import sys, types; from ameesh import heavy; "
                "heavy.GROUP_GRACE_S = float(sys.argv[3]); "
                "cfg = types.SimpleNamespace(state_dir=sys.argv[1], max_heavy_suites=1); "
                "raise SystemExit(heavy.run(cfg, [sys.executable, '-c', sys.argv[2]], nice=0))")
        proc = subprocess.Popen([sys.executable, "-c", code, self.state, child, str(grace)],
                                stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        found = [int(proc.stdout.readline()) for _ in range(pids)]
        for pid in found:
            self.addCleanup(self._kill, pid)
        return (proc, found[0]) if pids == 1 else (proc, found)

    #: a suite that starts a worker (which does not inherit the slot
    #: descriptor) and waits for it; `ignore`: the worker ignores SIGTERM
    SUITE = ("import os, subprocess, sys; "
             "worker = subprocess.Popen([sys.executable, '-c', "
             "'import os, signal, sys, time; '"
             "'signal.signal(signal.SIGTERM, signal.SIG_IGN) if sys.argv[1] == \"1\" else None; '"
             "'print(os.getpid(), flush=True); time.sleep(60)', '%s']); "
             "print(os.getpid(), flush=True); worker.wait()")

    @staticmethod
    def _kill(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    @staticmethod
    def _gone(pid: int, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                waited = 0          # not ours: probe it
                try:
                    os.kill(pid, 0)
                    with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
                        if fh.read().split(") ")[-1].startswith("Z"):
                            return True
                except (ProcessLookupError, FileNotFoundError):
                    return True
            if waited:
                return True
            time.sleep(0.05)
        return False

    def test_killed_wrapper_keeps_the_slot_while_the_command_runs(self):
        proc, pid = self._wrapper()
        self.assertEqual(heavy.status(self.one)[0].get("child"), pid)
        proc.kill()
        proc.wait(timeout=10)
        # the command still runs: no other suite may start
        self.assertFalse(self._gone(pid, timeout=0.2))
        self.assertIsNone(heavy.acquire(self.one, max_wait_s=0))
        self._kill(pid)
        self.assertTrue(self._gone(pid))
        got = heavy.acquire(self.one, max_wait_s=0)
        self.assertIsNotNone(got)
        heavy.release(got[1])

    def test_sigterm_and_sigint_are_relayed_to_the_command(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                proc, pid = self._wrapper()
                proc.send_signal(signum)
                code = proc.wait(timeout=10)
                self.assertTrue(self._gone(pid))
                # SIGINT: Python's KeyboardInterrupt exits with 1
                self.assertIn(code, (128 + signum, 1))
                got = heavy.acquire(self.one, max_wait_s=0)
                self.assertIsNotNone(got)
                heavy.release(got[1])

    def test_sigterm_reaches_the_suite_s_workers_and_the_slot_waits_for_them(self):
        # codex r2: the worker of a Python suite, which closes the inherited
        # slot descriptor, must not keep running on a released slot
        proc, (suite, worker) = self._wrapper(self.SUITE % "0", pids=2)
        self.assertEqual(os.getpgid(worker), suite)
        self.assertNotEqual(os.getpgid(suite), os.getpgid(0))
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=10), 128 + signal.SIGTERM)
        self.assertTrue(self._gone(suite))
        self.assertTrue(self._gone(worker))
        got = heavy.acquire(self.one, max_wait_s=0)
        self.assertIsNotNone(got)
        heavy.release(got[1])

    def test_a_worker_ignoring_sigterm_keeps_the_slot_until_sigkill(self):
        proc, (suite, worker) = self._wrapper(self.SUITE % "1", grace=1.5, pids=2)
        proc.send_signal(signal.SIGTERM)
        self.assertTrue(self._gone(suite))
        # codex r3: the command stays unreaped while its group drains — its
        # zombie keeps the group's number from being reused
        with open("/proc/%d/stat" % suite, encoding="utf-8") as fh:
            self.assertEqual(fh.read().rsplit(") ", 1)[-1].split()[0], "Z")
        # the suite is gone, its worker is not: the slot stays held
        self.assertFalse(self._gone(worker, timeout=0.3))
        self.assertIsNone(proc.poll())
        self.assertIsNone(heavy.acquire(self.one, max_wait_s=0))
        self.assertEqual(proc.wait(timeout=10), 128 + signal.SIGTERM)
        self.assertTrue(self._gone(worker))
        got = heavy.acquire(self.one, max_wait_s=0)
        self.assertIsNotNone(got)
        heavy.release(got[1])

    def test_what_the_command_leaves_running_ends_with_it(self):
        # the command exits 0 and leaves a worker: part of the suite, stopped
        # before the slot is released; the command's exit code is kept
        left = ("import os, subprocess, sys; "
                "worker = subprocess.Popen([sys.executable, '-c', "
                "'import os, time; print(os.getpid(), flush=True); time.sleep(60)'], "
                "stdout=subprocess.PIPE, text=True); "
                "print(os.getpid(), worker.stdout.readline(), sep='\\n', end='', flush=True)")
        proc, (command, worker) = self._wrapper(left, pids=2)
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertTrue(self._gone(worker))
        got = heavy.acquire(self.one, max_wait_s=0)
        self.assertIsNotNone(got)
        heavy.release(got[1])

    def test_a_suite_on_a_terminal_can_read_it(self):
        # codex r3: on a controlling terminal the command's group gets the
        # foreground; input() would otherwise stop on SIGTTIN, the wrapper
        # waiting for ever with the slot
        import pty
        import termios
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        env = dict(os.environ, PYTHONPATH=SRC, AMEESH_MAX_HEAVY_SUITES="1")
        child = "print('got', input('?'), flush=True)"
        code = ("import sys, types; from ameesh import heavy; "
                "cfg = types.SimpleNamespace(state_dir=sys.argv[1], max_heavy_suites=1); "
                "rc = heavy.run(cfg, [sys.executable, '-c', sys.argv[2]], nice=0); "
                "import os; print('fg', os.tcgetpgrp(0) == os.getpgrp(), flush=True); "
                "raise SystemExit(rc)")

        def controlling():
            import fcntl
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        proc = subprocess.Popen([sys.executable, "-c", code, self.state, child],
                                stdin=slave, stdout=slave, stderr=slave, env=env,
                                start_new_session=True, preexec_fn=controlling)
        os.close(slave)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        os.write(master, b"yes\n")
        self.assertEqual(proc.wait(timeout=20), 0)
        out = b""
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        self.assertIn(b"got yes", out)
        self.assertIn(b"fg True", out)       # the wrapper took the terminal back


class DrainTest(unittest.TestCase):
    def test_one_empty_scan_is_not_enough(self):
        # codex r3: a worker forks a replacement and exits between the
        # listing of /proc and its stat — one scan sees nothing, the next does
        scans = iter([False, True, False, False])
        sent = []
        with mock.patch.object(heavy, "_group_alive", side_effect=lambda pgid: next(scans)), \
                mock.patch.object(heavy, "_killpg",
                                  side_effect=lambda pgid, sig: sent.append(sig) or True):
            self.assertTrue(heavy._drain_group(4242, grace=5, sleep=lambda s: None))
        self.assertEqual(sent, [signal.SIGTERM])


class NoteTest(unittest.TestCase):
    def test_mail_turns_tell_agents_to_use_ameesh_heavy(self):
        from ameesh import mail
        texte = mail._assemble("mail", ["bloc"], 0)
        self.assertIn(adapters.HEAVY_NOTE, texte)
        self.assertIn("ameesh heavy --", adapters.HEAVY_NOTE)


if __name__ == "__main__":
    unittest.main()
