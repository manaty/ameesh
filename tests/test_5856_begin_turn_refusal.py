# SPDX-License-Identifier: AGPL-3.0-only
"""#5856: diagnose admission under its lock and stop repeated host refusals."""
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import registry, storage
from ameesh.runner import AgentWorker
from .support import PgTestCase


class AdmissionTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE host_lifecycle CASCADE")
        registry.upsert(self.db, "dev", harness="codex", host="source")
        self.lease = registry.claim(self.db, "dev", "runner", ttl_seconds=300)
        self.epoch = self.lease["lease_epoch"]

    def begin(self, owner="runner", epoch=None):
        return registry.begin_turn_diagnosed(
            self.db, "dev", owner, self.epoch if epoch is None else epoch, "new")

    def test_closed_host_with_valid_lease_preserves_prompt(self):
        registry.set_pending_prompt(self.db, "dev", "keep me")
        self.assertEqual(registry.take_pending_prompt(
            self.db, "dev", "runner", self.epoch), "keep me")
        storage.of(self.db).host_lifecycle.drain("source", actor="orch")
        self.assertEqual(self.begin(), "host_closed")
        registry.restore_prompt(self.db, "dev", "runner", self.epoch)
        row = registry.get(self.db, "dev")
        self.assertEqual(row["pending_prompt"], "keep me")
        self.assertEqual(row["lease_owner"], "runner")
        self.assertEqual(row["lease_epoch"], self.epoch)

    def test_success_and_each_locked_row_refusal(self):
        self.assertEqual(self.begin(), "started")
        self.assertEqual(self.begin(owner="other"), "lease_lost")
        self.assertEqual(self.begin(epoch=self.epoch + 1), "lease_lost")
        for assignment, reason in (
            ("operator_paused = true", "operator_paused"),
            ("handoff_fenced = true", "handoff_fenced"),
            ("status = 'stopped'", "stopped"),
            ("lease_expires_at = now() - interval '1 second'", "lease_expired"),
        ):
            with self.subTest(reason=reason):
                with self.db.transaction() as tx:
                    tx.execute("UPDATE agent_registry SET " + assignment + " WHERE name = 'dev'")
                    self.assertEqual(registry.begin_turn_diagnosed(
                        tx, "dev", "runner", self.epoch, "new"), reason)
                    # Reset the fixture change, including the paused state.
                    tx.execute("UPDATE agent_registry SET operator_paused=false, handoff_fenced=false, "
                               "status='idle', lease_expires_at=now()+interval '5 minutes' WHERE name='dev'")


class WorkerRefusalTest(unittest.TestCase):
    def worker(self):
        return SimpleNamespace(
            name="dev", db=object(), epoch=4,
            runner=SimpleNamespace(runner_id="runner", db_retry_max=8),
            lease_lost=threading.Event(), host_yield=None,
            _leave_for_host=mock.Mock(), wake=mock.Mock(),
            wait_timeout=lambda delay: delay)

    @mock.patch("ameesh.runner.registry.restore_prompt")
    @mock.patch("ameesh.runner.log")
    def test_host_refusal_leaves_worker_and_logs_reason(self, log, restore):
        worker = self.worker()
        AgentWorker._begin_turn_refused(worker, "host_closed")
        worker._leave_for_host.assert_called_once()
        self.assertIsNotNone(worker.host_yield)
        self.assertFalse(worker.lease_lost.is_set())
        self.assertIn("host_closed", log.call_args.args[0])
        restore.assert_called_once_with(worker.db, "dev", "runner", 4)
        worker.wake.wait.assert_not_called()

    @mock.patch("ameesh.runner.migrations.require_compatible_writer")
    @mock.patch("ameesh.runner.registry.restore_prompt")
    @mock.patch("ameesh.runner.log")
    def test_durable_host_refusal_exits_even_when_local_gate_is_open(self, log, restore, compatible):
        worker = self.worker()
        worker.stopping = threading.Event()
        worker.runner.stop = threading.Event()
        worker.runner.draining = threading.Event()
        worker.runner.gate_holds = mock.Mock(return_value=False)
        for name in ("ensure_watchdog", "ensure_peek", "_admission_priority",
                     "_apply_pending_config", "_deferral_over", "_mark_power_stop",
                     "_close_peek", "release_lease", "pick"):
            setattr(worker, name, mock.Mock())
        AgentWorker._begin_turn_refused(worker, "host_closed")
        AgentWorker._run_under_heartbeat(worker)
        worker.pick.assert_not_called()
        worker.release_lease.assert_called_once()

    @mock.patch("ameesh.runner.registry.restore_prompt")
    @mock.patch("ameesh.runner.log")
    def test_lost_lease_exits_without_backoff(self, log, restore):
        for reason in ("lease_lost", "lease_expired", "missing", "handoff_fenced", "stopped"):
            worker = self.worker()
            AgentWorker._begin_turn_refused(worker, reason)
            self.assertTrue(worker.lease_lost.is_set())
            worker.wake.wait.assert_not_called()

    @mock.patch("ameesh.runner.registry.restore_prompt")
    @mock.patch("ameesh.runner.log")
    def test_pause_or_unknown_refusal_has_bounded_backoff(self, log, restore):
        worker = self.worker()
        for reason in ("operator_paused", "unknown", "unknown", "unknown"):
            AgentWorker._begin_turn_refused(worker, reason)
        self.assertEqual(worker.wake.wait.call_args_list,
                         [mock.call(timeout=n) for n in (2, 4, 8, 8)])
