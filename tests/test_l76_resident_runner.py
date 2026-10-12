# SPDX-License-Identifier: AGPL-3.0-only
"""L76 (#285) — the resident process of an agent, under its lease.

Postgres for real; the L75 fake harness (`fake_resident_harness.py`) speaks
Claude Code stream-json: no network, no model, no cost.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from ameesh import adapters, harnesses, registry, resident, resident_runner, storage
from ameesh import runner as runner_mod
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase
from .test_l75_resident import claude_doc


def _group_gone(pgid: int, timeout: float = 5.0) -> bool:
    """No live member left in the group (the resident's leader may stay a
    zombie until it is reaped: its number is then still reserved)."""
    from ameesh import platform
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not [p for p in platform.process_table() if p["pgid"] == pgid and not p["zombie"]]:
            return True
        time.sleep(0.05)
    return False


def _state(pid: int) -> str:
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return ""


@unittest.skipUnless(os.path.isdir("/proc/self") and hasattr(os, "WNOWAIT"), "Linux")
class LeaderPopenTest(unittest.TestCase):
    """codex r2 (#417): the leader is reaped only after its group is killed."""

    def _leader(self, code: str):
        proc = resident.spawn_leader([sys.executable, "-c", code], start_new_session=True)
        self.assertIsInstance(proc, resident.LeaderPopen)
        self.addCleanup(proc.reap)
        return proc

    def test_exit_seen_without_reaping(self):
        proc = self._leader("import sys; sys.exit(3)")
        self.assertEqual(proc.wait(5), 3)
        self.assertEqual(proc.poll(), 3)
        self.assertIsNone(proc.returncode)            # not reaped
        self.assertEqual(_state(proc.pid), "Z")        # its number stays reserved
        self.assertEqual(proc.reap(), 3)
        self.assertEqual(_state(proc.pid), "")

    def test_reap_kills_the_group_first(self):
        proc = self._leader("import subprocess, sys, time; "
                            "subprocess.Popen([sys.executable, '-c', 'import time; "
                            "time.sleep(30)']); time.sleep(30)")
        time.sleep(0.5)
        self.assertEqual(proc.reap(), -signal.SIGKILL)
        self.assertTrue(_group_gone(proc.pid))

    def test_popen_internals_never_reap_behind_the_group(self):
        # codex r3: communicate() interrupted, then the driver's close
        proc = resident.spawn_leader(
            [sys.executable, "-c", "import subprocess, sys, time; "
             "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
             "sys.exit(0)"], start_new_session=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.addCleanup(proc.reap)
        self.assertEqual(proc.wait(5), 0)
        # an interrupted communicate() waits through Popen._wait
        with mock.patch.object(proc, "_communicate", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                proc.communicate()
        self.assertIsNone(proc.returncode)             # communicate/wait: not reaped
        time.sleep(0.3)
        self.assertFalse(_group_gone(proc.pid, timeout=0.2))   # its child still runs
        resident._kill_group(proc, 0.1)
        self.assertIsNotNone(proc.returncode)
        self.assertTrue(_group_gone(proc.pid))

    def test_an_orphaned_leader_is_cleaned_up_safely(self):
        # codex r3: Popen.__del__ / subprocess._cleanup
        proc = resident.spawn_leader([sys.executable, "-c", "pass"], start_new_session=True)
        proc.wait(5)
        pid = proc.pid
        with mock.patch.object(os, "killpg", wraps=os.killpg) as killpg:
            proc._internal_poll(_deadstate=sys.maxsize)
        killpg.assert_called_once_with(pid, signal.SIGKILL)   # its group first
        self.assertIsNotNone(proc.returncode)
        self.assertEqual(_state(pid), "")

    def test_a_finalizer_never_blocks_on_the_reap_lock(self):
        # codex r4: cleanup from __del__/_cleanup while another thread holds
        # the lock (and waits for the GC) must not block — it is deferred
        import gc
        import threading
        proc = resident.spawn_leader([sys.executable, "-c", "pass"], start_new_session=True)
        proc.wait(5)
        pid, lock = proc.pid, proc._reap_lock
        lock.acquire()
        try:
            box = []
            fil = threading.Thread(target=lambda: box.append(
                proc._internal_poll(_deadstate=sys.maxsize)), daemon=True)
            fil.start()
            fil.join(2)
            self.assertFalse(fil.is_alive(), "orphan cleanup blocked on the reap lock")
            self.assertEqual(box, [None])               # deferred: no exit code yet
            self.assertIsNone(proc.returncode)          # deferred, not dropped
            del proc, box
            gc.collect()                                # __del__ under the held lock
            self.assertEqual(_state(pid), "Z")          # its number still reserved
        finally:
            lock.release()
        subprocess._cleanup()                           # CPython's retry
        self.assertEqual(_state(pid), "")

    def test_a_deferred_cleanup_stays_in_subprocess_active(self):
        # codex r5: `_cleanup()` under the held lock keeps the orphan in
        # `_active`; the next one, once the lock is free, kills its group
        # (the descendant too) and reaps it
        proc = resident.spawn_leader(
            [sys.executable, "-c", "import subprocess, sys; "
             "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
             "sys.exit(0)"], start_new_session=True)
        self.addCleanup(proc.reap)
        self.assertEqual(proc.wait(5), 0)
        time.sleep(0.3)
        pid = proc.pid
        subprocess._active.append(proc)                 # as `__del__` leaves it
        self.addCleanup(lambda: proc in subprocess._active and subprocess._active.remove(proc))
        with proc._reap_lock:
            subprocess._cleanup()
            self.assertIn(proc, subprocess._active)     # not dropped
            self.assertIsNone(proc.returncode)
        self.assertFalse(_group_gone(pid, timeout=0.2))  # its descendant still runs
        subprocess._cleanup()                           # the retry, lock free
        self.assertNotIn(proc, subprocess._active)
        self.assertIsNotNone(proc.returncode)
        self.assertEqual(_state(pid), "")
        self.assertTrue(_group_gone(pid))

    def test_kill_group_signals_under_the_reap_lock(self):
        # codex r3: a reap between poll() and the signal never lets it out
        proc = self._leader("import time; time.sleep(30)")
        vraie = proc.poll
        proc.poll = lambda: (vraie(), proc.reap())[0]   # reaped right after poll()
        with mock.patch.object(os, "killpg", wraps=os.killpg) as killpg:
            resident._kill_group(proc, 0.1)
        self.assertEqual([c.args[1] for c in killpg.call_args_list], [signal.SIGKILL])

    def test_no_signal_once_reaped(self):
        proc = self._leader("pass")
        proc.wait(5)
        proc.reap()
        with mock.patch.object(os, "killpg") as killpg, mock.patch.object(os, "kill") as kill:
            self.assertFalse(proc.signal_group(signal.SIGKILL))
            proc.send_signal(signal.SIGKILL)
            rp = resident_runner.ResidentProcess(mock.Mock(proc=proc, pgid=proc.pid),
                                                 key=(), life_id="x")
            rp.kill_now()
        killpg.assert_not_called()
        kill.assert_not_called()


class TurnFileTest(unittest.TestCase):
    def test_the_hook_reads_the_turn_of_a_resident_process(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")), "resident-turn")
        env = {"AMEESH_TURN_ID": "vie", resident_runner.TURN_FILE_ENV: path}
        self.assertEqual(resident_runner.current_turn(env), "vie")   # no file yet
        resident_runner.write_turn(path, "tour-1")
        self.assertEqual(resident_runner.current_turn(env), "tour-1")
        resident_runner.write_turn(path, "")
        self.assertEqual(resident_runner.current_turn(env), "")       # between turns
        self.assertEqual(resident_runner.current_turn({"AMEESH_TURN_ID": "t"}), "t")


class ResidentRunnerTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE turn_costs, spend_pending, turn_resources CASCADE")
        self.log = os.path.join(self.tmp, "resident-trace.jsonl")
        path = os.path.join(self.tmp, "fake-claude.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(claude_doc(), fh)
        self.adapter = adapters.HarnessAdapter(harnesses.load(path, source="test"),
                                               sys.executable)
        for patch in (mock.patch.dict(os.environ, {"FAKE_RESIDENT_LOG": self.log}),
                      mock.patch.object(resident_runner, "binary_version",
                                        return_value="2.1.286")):
            patch.start()
            self.addCleanup(patch.stop)

    def _worker(self, name: str = "res1", **local):
        runner = Runner(self.cfg, self.db, once=False)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        self.addCleanup(worker._close_resident, "end of test")
        worker._adapter = lambda: self.adapter
        self._set(worker, resident="on", **local)
        return runner, worker

    @staticmethod
    def _set(worker, **values) -> None:
        os.makedirs(worker.state_dir, exist_ok=True)
        for key, value in values.items():
            with open(os.path.join(worker.state_dir, key), "w", encoding="utf-8") as fh:
                fh.write(str(value))

    def _trace(self) -> list[dict]:
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _starts(self) -> list[list[str]]:
        return [e["argv"] for e in self._trace() if "argv" in e]

    def _turn(self, worker, prompt: str) -> bool:
        return worker.run_turn({"kind": "prompt", "prompt": prompt, "ids": []})

    def test_one_process_serves_the_turns(self):
        _runner, worker = self._worker()
        self.assertTrue(self._turn(worker, "premier"))
        rp = worker.resident
        self.assertIsNotNone(rp)
        self.assertTrue(rp.alive)
        self.assertIsNone(worker.proc)            # published for a turn only
        self.assertTrue(self._turn(worker, "second"))
        self.assertIs(worker.resident, rp)
        self.assertEqual(len(self._starts()), 1)
        self.assertEqual([e["user"] for e in self._trace() if "user" in e],
                         ["premier", "second"])
        with open(worker._path("events.jsonl"), encoding="utf-8") as fh:
            flux = fh.read()
        self.assertIn("ECHO:premier", flux)
        self.assertIn("ECHO:second", flux)
        self.assertEqual(registry.get(self.db, "res1")["status"], "idle")
        self.assertEqual(rp.turns, 2)
        # the process is a resource of its life, not of each turn
        ligne = storage.of(self.db).turn_resources.get(rp.life_id)
        self.assertEqual((ligne["status"], ligne["pgid"]), ("running", rp.pgid))
        # the hook: no turn between two turns
        with open(worker._path(resident_runner.TURN_FILE), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "")

    def test_changed_settings_restart_on_the_session(self):
        _runner, worker = self._worker()
        self.assertTrue(self._turn(worker, "un"))
        premier = worker.resident
        session = premier.session
        self._set(worker, model="autre-modele")
        self.assertTrue(self._turn(worker, "deux"))
        self.assertIsNot(worker.resident, premier)
        self.assertTrue(_group_gone(premier.pgid))
        argv = self._starts()[-1]
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], session)
        self.assertIn("autre-modele", argv)
        self.assertEqual(storage.of(self.db).turn_resources.get(premier.life_id)["status"],
                         "done")

    def test_an_idle_resident_dies_with_the_lease(self):
        _runner, worker = self._worker()
        self.assertTrue(self._turn(worker, "un"))
        rp = worker.resident
        # every lease loss goes through stop_group_now(hard=True)
        worker.lease_lost.set()
        worker.stop_group_now("lease deadline reached", grace=0, hard=True)
        self.assertTrue(_group_gone(rp.pgid))
        worker.release_lease()
        self.assertIsNone(worker.resident)
        self.assertEqual(storage.of(self.db).turn_resources.get(rp.life_id)["status"],
                         "done")

    def test_asleep_after_inactivity_then_woken_on_its_session(self):
        _runner, worker = self._worker(resident_sleep="60")
        self.assertTrue(self._turn(worker, "un"))
        rp = worker.resident
        worker._resident_between_turns()
        self.assertIs(worker.resident, rp)        # not idle long enough yet
        rp.last_used -= 61
        worker._resident_between_turns()
        self.assertIsNone(worker.resident)
        self.assertTrue(_group_gone(rp.pgid))
        self.assertTrue(self._turn(worker, "deux"))
        self.assertIn("--resume", self._starts()[-1])

    def test_a_crash_fails_the_turn_and_drops_the_process(self):
        _runner, worker = self._worker()
        self.assertFalse(self._turn(worker, "CRASH"))
        self.assertIsNone(worker.resident)
        self.assertEqual(registry.get(self.db, "res1")["status"], "blocked")

    def test_a_cap_interrupts_without_killing(self):
        _runner, worker = self._worker()
        with mock.patch.object(worker, "turn_close_due", return_value="duration cap: test"):
            self.assertTrue(self._turn(worker, "SLOW"))
        rp = worker.resident
        self.assertIsNotNone(rp)
        self.assertTrue(rp.alive)
        self.assertTrue(worker.last_turn_closed.startswith("duration cap"))
        self.assertTrue(self._turn(worker, "suite"))
        self.assertIs(worker.resident, rp)

    def test_start_failure_falls_back_to_one_turn_per_event(self):
        _runner, worker = self._worker()
        launched = []
        real_popen = runner_mod.subprocess.Popen

        def popen(argv, **kwargs):
            if list(argv[:1]) != [sys.executable]:   # psql, git…: as they are
                return real_popen(argv, **kwargs)
            launched.append(list(argv))
            return real_popen([sys.executable, "-c", "pass"], **kwargs)
        with mock.patch.object(resident.ClaudeStreamJson, "start",
                               side_effect=resident.ResidentError("handshake refused")), \
                mock.patch.object(runner_mod.subprocess, "Popen", side_effect=popen):
            self._turn(worker, "un")
        self.assertIsNone(worker.resident)
        self.assertEqual(len(launched), 1)        # the turn's own harness
        self.assertGreater(worker._resident_refused_until, time.monotonic())
        self.assertIn("resident start failed",
                      worker._resident_refusal(self.adapter, False))

    def test_refusals(self):
        runner, worker = self._worker()
        self.assertEqual(worker._resident_refusal(self.adapter, False), "")
        self._set(worker, resident="off")
        self.assertEqual(worker._resident_refusal(self.adapter, False), "resident=off")
        self._set(worker, resident="on")
        with mock.patch.object(runner, "is_auditor", return_value=True):
            self.assertIn("auditor", worker._resident_refusal(self.adapter, False))
        runner.once = True
        self.assertIn("--once", worker._resident_refusal(self.adapter, False))

    # codex r1 (#417)

    def test_lease_lost_during_a_close_still_kills(self):
        import threading
        _runner, worker = self._worker()
        self.assertTrue(self._turn(worker, "un"))
        rp = worker.resident
        libre = threading.Event()
        vraie = rp.close
        rp.close = lambda grace: (libre.wait(10), vraie(grace))
        fil = threading.Thread(target=worker._close_resident, args=("asleep",), daemon=True)
        fil.start()
        deadline = time.monotonic() + 5
        while not rp.closing and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIs(worker.resident, rp)        # still reachable while it closes
        worker.stop_group_now("lease deadline reached", grace=0, hard=True)
        self.assertTrue(_group_gone(rp.pgid))
        libre.set()
        fil.join(10)
        self.assertIsNone(worker.resident)

    def test_kill_now_kills_an_idle_group(self):
        _runner, worker = self._worker()
        self.assertTrue(self._turn(worker, "un"))
        rp = worker.resident
        self.assertIsInstance(rp.proc, resident.LeaderPopen)
        rp.kill_now()
        self.assertTrue(_group_gone(rp.pgid))
        self.assertIsNone(rp.proc.returncode)       # still unreaped: number reserved
        worker._close_resident("test")
        self.assertIsNotNone(rp.proc.returncode)
        self.assertEqual(_state(rp.pgid), "")

    def test_a_changed_turn_environment_restarts(self):
        _runner, worker = self._worker()
        with mock.patch.object(worker, "turn_mail_max", return_value=20):
            self.assertTrue(self._turn(worker, "un"))
        premier = worker.resident
        with mock.patch.object(worker, "turn_mail_max", return_value=1):
            self.assertTrue(self._turn(worker, "deux"))
        self.assertIsNot(worker.resident, premier)
        self.assertEqual(len(self._starts()), 2)
        self.assertEqual(worker.resident.driver.extra_env["AMEESH_TURN_MAIL_MAX"], "1")

    def test_resident_off_stops_it_between_turns(self):
        _runner, worker = self._worker()
        self.assertTrue(self._turn(worker, "un"))
        rp = worker.resident
        self._set(worker, resident="off")
        worker._resident_between_turns()
        self.assertIsNone(worker.resident)
        self.assertTrue(_group_gone(rp.pgid))


if __name__ == "__main__":
    unittest.main()
