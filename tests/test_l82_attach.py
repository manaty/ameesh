# SPDX-License-Identifier: AGPL-3.0-only
"""L82: native handoff, a hard duration cap and duplicate refusal."""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from ameesh import adapters, attach, harnesses, mail, registry, reprise, resident_runner
from ameesh.platform import session_lock
from ameesh.runner import AgentWorker, Runner, attach_main, run_attach

from .support import FAKEBIN, PgTestCase
from .test_l75_resident import claude_doc


class DeadlineTest(unittest.TestCase):
    def test_a_short_probe_is_not_refused_as_an_attached_session(self):
        with tempfile.TemporaryDirectory() as state:
            probe = session_lock.try_lock(state, "attach-request")
            self.addCleanup(probe.close)
            cfg = SimpleNamespace(agent_dir=lambda _name: state)
            with mock.patch("ameesh.runner.time.sleep", side_effect=lambda _: probe.close()), \
                    mock.patch("ameesh.runner._run_attach", return_value=0) as run:
                self.assertEqual(run_attach(cfg, object(), "probe82"), 0)
            run.assert_called_once()

    def test_duration_and_notice_are_monotonic(self):
        self.assertEqual(attach.duration("2h"), 7200)
        self.assertEqual(attach.duration("30m"), 1800)
        for bad in ("0", "-1", "nan", "inf", "2years"):
            with self.assertRaises(ValueError):
                attach.duration(bad)
        with mock.patch.object(attach.time, "monotonic", return_value=100) as clock:
            deadline = attach.Deadline(120)
            self.assertIsNone(deadline.notice())
            clock.return_value = 160
            self.assertEqual(deadline.notice(), 60)
            self.assertIsNone(deadline.notice())
            self.assertFalse(deadline.expired())
            clock.return_value = 220
            self.assertTrue(deadline.expired())

    def test_invalid_cli_max_precedes_database_access(self):
        with mock.patch("ameesh.runner.load_config") as load:
            with self.assertRaises(SystemExit) as error:
                attach_main(["agent", "--max", "0"])
        self.assertEqual(error.exception.code, 2)
        load.assert_not_called()

    def test_locks_exclude_other_processes_and_release_on_death(self):
        with tempfile.TemporaryDirectory() as state:
            code = ("from ameesh.platform.session_lock import try_lock; import sys; "
                    "lock=try_lock(sys.argv[1], 'attach-request'); "
                    "print('ready', flush=True); sys.stdin.read()")
            proc = subprocess.Popen([sys.executable, "-c", code, state],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(proc.stdout.readline().strip(), "ready")
                self.assertTrue(session_lock.requested(state))
                self.assertIsNone(session_lock.try_lock(state, "attach-request"))
                proc.kill()
                proc.wait(timeout=5)
                self.assertFalse(session_lock.requested(state))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()


class AttachTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register("att82", "claude", cwd=self.tmp)
        patch = mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN,
                                            "AMEESH_TEST_LOG": self.turns_log})
        patch.start()
        self.addCleanup(patch.stop)

    def test_max_stops_the_harness_warns_and_returns_the_lease(self):
        with mock.patch.dict(os.environ, {"AMEESH_TEST_SLEEP": "20"}), \
                mock.patch("ameesh.runner.log_async") as log:
            code = run_attach(self.cfg, self.db, "att82", ttl=30, max_seconds=1)
        self.assertEqual(code, 124)
        self.assertIsNone(registry.get(self.db, "att82")["lease_owner"])
        messages = "\n".join(call.args[0] for call in log.call_args_list)
        self.assertIn("attach ends in", messages)
        self.assertIn("maximum duration reached", messages)
        # Both local locks are released even on expiry.
        self.assertFalse(session_lock.requested(self.cfg.agent_dir("att82")))
        lock = session_lock.try_lock(self.cfg.agent_dir("att82"), "session-use")
        self.assertIsNotNone(lock)
        lock.close()

    def test_second_attach_refused_even_with_wait(self):
        with session_lock.try_lock(self.cfg.agent_dir("att82"), "attach-request"):
            self.assertEqual(run_attach(self.cfg, self.db, "att82", wait=True), 3)
        lease = registry.attach_claim(self.db, "att82", "attach:older-runner", 30)
        self.assertIsNotNone(lease)
        self.assertEqual(run_attach(self.cfg, self.db, "att82", wait=True), 3)

    def test_wait_uses_the_session_written_by_the_finishing_turn(self):
        lease = registry.claim(self.db, "att82", "runner-before", 30)
        self.db.execute("UPDATE agent_registry SET status = 'running', session_id = 'old82' "
                        "WHERE name = 'att82'")
        original_sleep = time.sleep

        def finish(seconds):
            if seconds == 2.0:
                self.db.execute("UPDATE agent_registry SET status = 'idle', session_id = 'new82' "
                                "WHERE name = 'att82'")
            else:
                original_sleep(seconds)

        with mock.patch("ameesh.runner.time.sleep", side_effect=finish):
            self.assertEqual(run_attach(self.cfg, self.db, "att82", wait=True), 0)
        self.assertIn("new82", self.turns()[0]["argv"])
        self.assertIsNotNone(lease)

    def test_waiting_input_is_reported_without_being_delivered(self):
        mail.send(self.db, "source", "att82", "pending input")
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '11 minutes' "
                        "WHERE recipient = 'att82'")
        with mock.patch.dict(os.environ, {"AMEESH_TEST_SLEEP": "1"}), \
                mock.patch("ameesh.runner.log_async") as log:
            self.assertEqual(run_attach(self.cfg, self.db, "att82"), 0)
        self.assertTrue(any("inputs waiting" in call.args[0] for call in log.call_args_list))
        self.assertEqual(len(mail.unread(self.db, "att82")), 1)

    def test_attach_request_does_not_close_or_unlock_a_turn_in_progress(self):
        runner = Runner(self.cfg, self.db)
        lease = registry.claim(self.db, "att82", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "att82"), lease)
        self.addCleanup(worker.release_lease)
        started, finish = threading.Event(), threading.Event()

        def turn(_spec):
            started.set()
            self.assertTrue(finish.wait(10))
            return True

        result = []
        with mock.patch.object(worker, "_run_guarded_turn", side_effect=turn):
            thread = threading.Thread(target=lambda: result.append(worker.run_turn({})), daemon=True)
            thread.start()
            self.assertTrue(started.wait(10))
            try:
                with session_lock.try_lock(worker.state_dir, "attach-request"):
                    with mock.patch.object(worker, "_close_resident") as close:
                        self.assertTrue(worker._attach_pending())
                        close.assert_not_called()
                    self.assertIsNone(session_lock.try_lock(worker.state_dir, "session-use"))
                    finish.set()
                    thread.join(timeout=10)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(result, [True])
                    lock = session_lock.try_lock(worker.state_dir, "session-use")
                    self.assertIsNotNone(lock)
                    lock.close()
            finally:
                finish.set()
                thread.join(timeout=10)

    def test_resident_sleeps_before_claim_and_pending_inputs_resume_on_same_session(self):
        self.db.execute("TRUNCATE turn_costs, spend_pending, turn_resources CASCADE")
        path = os.path.join(self.tmp, "fake-claude.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(claude_doc(), fh)
        adapter = adapters.HarnessAdapter(harnesses.load(path, source="test"), sys.executable)
        runner = Runner(self.cfg, self.db)
        lease = registry.claim(self.db, "att82", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "att82"), lease)
        self.addCleanup(worker.watchdog_stop.set)
        self.addCleanup(worker.release_lease)
        worker._adapter = lambda: adapter
        with open(os.path.join(worker.state_dir, "resident"), "w") as fh:
            fh.write("on")
        with mock.patch.object(resident_runner, "binary_version", return_value="2.1.286"):
            self.assertTrue(worker.run_turn({"kind": "prompt", "prompt": "first", "ids": []}))
            rp = worker.resident
            self.assertIsNotNone(rp)
            session = worker.current_session()
            mail.send(self.db, "source", "att82", "queued during attach")
            result = []
            original_claim = registry.attach_claim
            original_lock = session_lock.try_lock
            requested = threading.Event()

            def request_lock(state, kind):
                lock = original_lock(state, kind)
                if kind == "attach-request" and lock is not None:
                    requested.set()
                return lock

            def claim(*args, **kwargs):
                self.assertFalse(rp.alive, "resident must be closed BEFORE taking its lease")
                return original_claim(*args, **kwargs)

            with mock.patch.object(registry, "attach_claim", side_effect=claim), \
                    mock.patch.object(session_lock, "try_lock", side_effect=request_lock):
                thread = threading.Thread(target=lambda: result.append(
                    run_attach(self.cfg, self.db, "att82")), daemon=True)
                thread.start()
                self.assertTrue(requested.wait(10), "attach did not request the session")
                self.assertEqual(registry.get(self.db, "att82")["lease_owner"], runner.runner_id)
                self.assertIsNone(worker.pick())
                thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [0])
            self.assertFalse(rp.alive)
            self.assertEqual(len(mail.unread(self.db, "att82")), 1)
            lease2 = registry.claim(self.db, "att82", runner.runner_id, 3600)
            worker2 = AgentWorker(runner, registry.get(self.db, "att82"), lease2)
            self.addCleanup(worker2.watchdog_stop.set)
            self.addCleanup(worker2.release_lease)
            worker2._adapter = lambda: adapter
            self.assertEqual(worker2.current_session(), session)
            self.assertTrue(worker2.run_turn(worker2.pick()))
            self.assertIsNotNone(worker2.resident)
            self.assertEqual(worker2.current_session(), session)
            self.assertEqual(mail.unread(self.db, "att82"), [])


class DuplicateTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.sid = str(uuid.uuid4())
        self.session_home = os.path.join(self.tmp, "claude-home")
        self.session_dir = os.path.join(self.session_home, "projects", "test")
        os.makedirs(self.session_dir)
        self.path = os.path.join(self.session_dir, self.sid + ".jsonl")
        with open(self.path, "w") as fh:
            fh.write("{}\n")
        self.cfg = dataclasses.replace(self.cfg, accounts={"claude": [
            {"name": "one", "path": self.session_home}]})
        self.register("dup82", "claude", cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET session_id = %s, session_account = 'one' "
                        "WHERE name = 'dup82'", (self.sid,))

    def test_adopt_refuses_a_recently_written_closed_file(self):
        with mock.patch.object(reprise, "holders", return_value=[]):
            with self.assertRaisesRegex(reprise.RepriseError, "last 60 seconds"):
                reprise.adopt(self.cfg, self.db, "dup82", session_id=self.sid, harness="claude")

    def test_adopt_with_force_records_recent_activity_as_forced(self):
        warnings = []
        with mock.patch.object(reprise, "holders", return_value=[]), \
                mock.patch.object(reprise, "recently_modified", return_value=True):
            result = reprise.adopt(self.cfg, self.db, "dup82", session_id=self.sid,
                                   harness="claude", force=True, warn=warnings.append)
        self.assertTrue(result["forced"])
        self.assertTrue(any("activity in the last 60 seconds" in text for text in warnings))

    def test_resume_refuses_recent_activity_without_changing_the_queue(self):
        with mock.patch.object(reprise, "holders", return_value=[]), \
                mock.patch.object(reprise, "plan", return_value={
                    "keep": True, "session": self.sid, "previous": "one", "warnings": []}):
            with self.assertRaisesRegex(reprise.RepriseError, "last 60 seconds"):
                reprise.resume(self.cfg, self.db, "dup82")
        self.assertFalse(registry.get(self.db, "dup82")["pending_prompt"])

    def test_resume_refuses_an_external_writer_and_wakes_a_dormant_runner(self):
        os.utime(self.path, (time.time() - 120, time.time() - 120))
        with mock.patch.object(reprise, "holders", return_value=[{"pid": 123}]), \
                mock.patch.object(reprise, "plan", return_value={
                    "keep": True, "session": self.sid, "previous": "one", "warnings": []}):
            with self.assertRaisesRegex(reprise.RepriseError, "close the interactive session first"):
                reprise.resume(self.cfg, self.db, "dup82")
        # An idle resident owned by the runner is expected to hold its file.
        registry.claim(self.db, "dup82", "resident-runner", 3600)
        with mock.patch.object(reprise, "holders", side_effect=AssertionError("own resident")):
            result = reprise.resume(self.cfg, self.db, "dup82", brief="wake up")
        self.assertTrue(result["runner_live"])
        self.assertIn("wake up", registry.get(self.db, "dup82")["pending_prompt"])


if __name__ == "__main__":
    unittest.main()
