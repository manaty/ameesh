# SPDX-License-Identifier: AGPL-3.0-only
"""L131 — heavy suites capped per host (`ameesh heavy`). Pure tests: slots
live in a temporary state directory, no database."""
from __future__ import annotations

import io
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

    def test_the_terminal_left_to_the_shell_stays_with_it(self):
        # L131d (codex r2 of #266): stopped, sent to the background and left
        # to exit, the command leaves the foreground with the shell — the
        # wrapper's cleanup must not take it from the shell
        import pty
        import termios
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        env = dict(os.environ, PYTHONPATH=SRC, AMEESH_MAX_HEAVY_SUITES="1")
        child = ("import os, signal, sys; signal.signal(signal.SIGTTOU, signal.SIG_IGN); "
                 "fd = os.open('/dev/tty', os.O_RDWR); os.tcsetpgrp(fd, int(sys.argv[1]))")
        # the "shell": a group of its own, not a child of the wrapper (its
        # cleanup would stop it), reparented to init before heavy.run
        code = ("import os, signal, sys, time, types; from ameesh import heavy\n"
                "r, w = os.pipe()\n"
                "if os.fork() == 0:\n"
                "    if os.fork() == 0:\n"
                "        os.setpgid(0, 0); os.write(w, b'%8d' % os.getpid()); time.sleep(30)\n"
                "    os._exit(0)\n"
                "os.wait()\n"
                "shell = int(os.read(r, 8))\n"
                "cfg = types.SimpleNamespace(state_dir=sys.argv[1], max_heavy_suites=1)\n"
                "rc = heavy.run(cfg, [sys.executable, '-c', sys.argv[2], str(shell)], nice=0)\n"
                "print('shell keeps it', os.tcgetpgrp(0) == shell, flush=True)\n"
                "os.kill(shell, signal.SIGKILL)\n"
                "raise SystemExit(rc)\n")

        def controlling():
            import fcntl
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        proc = subprocess.Popen([sys.executable, "-c", code, self.state, child],
                                stdin=slave, stdout=slave, stderr=slave, env=env,
                                start_new_session=True, preexec_fn=controlling)
        os.close(slave)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        out = self._read_until(master, b"shell keeps it ", 20)
        self.assertEqual(proc.wait(timeout=20), 0, out)
        self.assertIn(b"shell keeps it True", out + self._read_until(master, b"True", 2))

    def _on_pty(self, child: str, *, stdin_null: bool = False):
        """The wrapper as a session leader on a new pty (its controlling
        terminal, wrapper in the foreground): (process, master fd)."""
        import pty
        import termios
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        env = dict(os.environ, PYTHONPATH=SRC, AMEESH_MAX_HEAVY_SUITES="1")
        code = ("import sys, types; from ameesh import heavy; "
                "cfg = types.SimpleNamespace(state_dir=sys.argv[1], max_heavy_suites=1); "
                "raise SystemExit(heavy.run(cfg, [sys.executable, '-c', sys.argv[2]], nice=0))")

        def controlling():
            import fcntl
            fcntl.ioctl(1, termios.TIOCSCTTY, 0)

        proc = subprocess.Popen([sys.executable, "-c", code, self.state, child],
                                stdin=subprocess.DEVNULL if stdin_null else slave,
                                stdout=slave, stderr=slave, env=env,
                                start_new_session=True, preexec_fn=controlling)
        os.close(slave)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc, master

    @staticmethod
    def _read_until(master: int, needle: bytes, timeout: float = 15.0) -> bytes:
        import select
        out, deadline = b"", time.monotonic() + timeout
        while needle not in out and time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    out += os.read(master, 4096)
                except OSError:
                    break
        return out

    def test_reads_dev_tty_with_stdin_redirected(self):
        # L131c (codex r4): stdin is /dev/null, the command opens /dev/tty
        proc, master = self._on_pty(
            "import os; fd = os.open('/dev/tty', os.O_RDWR); os.write(fd, b'?'); "
            "print('got', os.read(fd, 100).decode().strip(), flush=True)", stdin_null=True)
        self.assertIn(b"?", self._read_until(master, b"?"))
        os.write(master, b"yes\n")
        self.assertEqual(proc.wait(timeout=20), 0)
        self.assertIn(b"got yes", self._read_until(master, b"got yes", 5))

    def test_ctrl_z_suspends_the_wrapper_and_fg_resumes(self):
        # L131c (codex r4): Ctrl-Z stops the command's group; the wrapper
        # stops too (the shell gets its prompt), and SIGCONT (`fg`) resumes
        proc, master = self._on_pty("print('got', input('?'), flush=True)")
        self.assertIn(b"?", self._read_until(master, b"?"))
        os.write(master, b"\x1a")                       # Ctrl-Z
        status, deadline = None, time.monotonic() + 15
        while status is None and time.monotonic() < deadline:
            pid, raw = os.waitpid(proc.pid, os.WUNTRACED | os.WNOHANG)
            status = raw if pid else None
            time.sleep(0.05)
        self.assertIsNotNone(status, "the wrapper did not stop with its command")
        self.assertTrue(os.WIFSTOPPED(status))
        os.kill(proc.pid, signal.SIGCONT)                # what `fg` sends
        os.write(master, b"yes\n")
        self.assertEqual(proc.wait(timeout=20), 0)
        self.assertIn(b"got yes", self._read_until(master, b"got yes", 5))

    def test_a_fork_chain_and_setsid_cannot_outlive_the_slot(self):
        # L131c (codex r4): W1 forks W2 and exits, W2 leaves the group
        # (setsid), forks W3 and exits; W3 runs on — the wrapper, a
        # subreaper, adopts it and stops it before releasing the slot
        chain = ("import os, sys, time\n"
                 "r, w = os.pipe()\n"
                 "if os.fork() == 0:\n"
                 "    if os.fork() == 0:\n"
                 "        os.setsid()\n"
                 "        if os.fork() == 0:\n"
                 "            os.write(w, b'%d\\n' % os.getpid()); time.sleep(60)\n"
                 "        os._exit(0)\n"
                 "    os._exit(0)\n"
                 "print(os.getpid(), flush=True)\n"
                 "print(os.read(r, 100).decode().strip(), flush=True)\n")
        proc, (command, w3) = self._wrapper(chain, grace=1.0, pids=2)
        self.assertNotEqual(os.getsid(w3), os.getsid(command))
        self.assertEqual(proc.wait(timeout=20), 0)
        self.assertTrue(self._gone(w3))
        got = heavy.acquire(self.one, max_wait_s=0)
        self.assertIsNotNone(got)
        heavy.release(got[1])


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

    def test_without_a_process_table_kill_zero_decides(self):
        # mesh-design r4 (CI L63): the scan lives in the platform layer; an OS
        # where it cannot list processes refuses (NotAvailable), and heavy
        # falls back on kill(0) and reaps the command at once
        from ameesh import platform
        refuse = mock.patch.object(platform, "process_table",
                                   side_effect=platform.NotAvailable("table", "test"))
        with refuse, mock.patch.object(heavy, "_killpg", return_value=True):
            self.assertTrue(heavy._group_alive(4242))
            self.assertFalse(heavy._can_scan())
        with refuse, mock.patch.object(heavy, "_killpg", return_value=False):
            self.assertFalse(heavy._group_alive(4242))
        table = [{"pid": 1, "pgid": 4242, "zombie": True},
                 {"pid": 2, "pgid": 4243, "zombie": False}]
        with mock.patch.object(platform, "process_table", return_value=table), \
                mock.patch.object(heavy, "_killpg", return_value=True):
            self.assertFalse(heavy._group_alive(4242))      # only a zombie left
            table.append({"pid": 3, "pgid": 4242, "zombie": False})
            self.assertTrue(heavy._group_alive(4242))


class TreeReviewR1Test(unittest.TestCase):
    """#266 r1 (codex)."""

    @staticmethod
    def _clock():
        t = [0.0]

        def clock():
            t[0] += 1.0
            return t[0]
        return clock

    def test_a_read_that_reaps_a_zombie_is_read_again(self):
        # a listed child forks a setsid replacement and exits before its
        # state is read: the zombie is reaped, its replacement must be seen
        from ameesh import platform
        reads = iter([[{"pid": 10, "zombie": True}], [{"pid": 11, "zombie": False}],
                      [], [], [], [], []])
        killed = []
        with mock.patch.object(platform, "children", side_effect=lambda: next(reads)), \
                mock.patch.object(platform, "reap", return_value=0), \
                mock.patch.object(heavy.os, "kill",
                                  side_effect=lambda pid, sig: killed.append((pid, sig))):
            self.assertTrue(heavy._drain_tree(1, grace=5, sleep=lambda s: None,
                                              clock=self._clock()))
        self.assertEqual(killed, [(11, signal.SIGTERM)])

    def test_a_leftover_that_may_not_be_signalled_does_not_stop_the_cleanup(self):
        from ameesh import platform
        killed = []

        def kill(pid, sig):
            if pid == 20:
                raise PermissionError(1, "Operation not permitted")
            killed.append((pid, sig))
        rows = [{"pid": 20, "zombie": False}, {"pid": 21, "zombie": False}]
        err = io.StringIO()
        with mock.patch.object(platform, "children", return_value=rows), \
                mock.patch.object(heavy.os, "kill", side_effect=kill), \
                mock.patch.object(heavy.sys, "stderr", err):
            self.assertFalse(heavy._drain_tree(1, grace=3, sleep=lambda s: None,
                                               clock=self._clock()))
        self.assertEqual(killed, [(21, signal.SIGTERM), (21, signal.SIGKILL)])
        self.assertEqual(err.getvalue().count("process 20 "), 1)

    def test_a_fork_churn_does_not_delay_the_signals_nor_the_deadline(self):
        # L131d (codex r2 of #266): every read reaps a zombie — the one that
        # just forked its replacement — and lists that replacement
        from ameesh import platform
        pids = iter(range(100, 10000))
        reads = []

        def churn():
            reads.append(1)
            if len(reads) > 500:
                raise AssertionError("drain stuck in the churn")
            return [{"pid": next(pids), "zombie": True}, {"pid": next(pids), "zombie": False}]

        killed = []
        with mock.patch.object(platform, "children", side_effect=churn), \
                mock.patch.object(platform, "reap", return_value=0), \
                mock.patch.object(heavy.os, "kill",
                                  side_effect=lambda pid, sig: killed.append((pid, sig))):
            self.assertFalse(heavy._drain_tree(1, grace=3, sleep=lambda s: None,
                                               clock=self._clock()))
        self.assertEqual(killed[0], (101, signal.SIGTERM))   # the first read signals
        self.assertIn(signal.SIGKILL, [sig for _, sig in killed])
        self.assertLess(len(reads), 30)

    def _suspend(self, owners):
        owner = iter(owners)
        moved = []
        with mock.patch.object(heavy.os, "tcgetpgrp", side_effect=lambda fd: next(owner)), \
                mock.patch.object(heavy.os, "tcsetpgrp",
                                  side_effect=lambda fd, pg: moved.append(pg)), \
                mock.patch.object(heavy.os, "getpgrp", return_value=300), \
                mock.patch.object(heavy.os, "kill"), \
                mock.patch.object(heavy, "_killpg", return_value=True) as cont:
            heavy._suspend(7, 500)
        cont.assert_called_once_with(500, signal.SIGCONT)
        return moved

    def test_a_command_stopped_in_the_background_leaves_the_terminal_to_the_shell(self):
        # `bg` then a read of the terminal (SIGTTIN): the shell (999) holds
        # the foreground, the wrapper must not take it
        self.assertEqual(self._suspend([999, 999]), [])

    def test_ctrl_z_then_fg_hands_the_terminal_back_to_the_command(self):
        self.assertEqual(self._suspend([500, 300]), [300, 500])


class NoteTest(unittest.TestCase):
    def test_mail_turns_tell_agents_to_use_ameesh_heavy(self):
        from ameesh import mail
        texte = mail._assemble("mail", ["bloc"], 0)
        self.assertIn(adapters.HEAVY_NOTE, texte)
        self.assertIn("ameesh heavy --", adapters.HEAVY_NOTE)


if __name__ == "__main__":
    unittest.main()
