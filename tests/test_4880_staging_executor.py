# SPDX-License-Identifier: AGPL-3.0-only
"""Simulated candidate takeover; no SSH, service or real deployment."""
from contextlib import nullcontext
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from ameesh import release_cutoff, staging_executor as executor


class ExecutorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="4880-executor-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bascule = self.root / "bascule.env"
        self.bascule.touch()
        self.options = dict(key="mesh", project="test", role="pilot", candidates=["host-a", "host-b"],
                            repo=str(self.root), bascule_env=str(self.bascule),
                            quiescence_probe=["prove-old-and-remote-operations-stopped"])
        self.cfg = types.SimpleNamespace(host="host-a")
        self.request = dict(request="request-1", sha="a" * 40, state="pending")
        self.proofs = []
        self.calls = []
        self.held = False
        self.db = types.SimpleNamespace()
        self.db.transaction = lambda: nullcontext(self.db)
        store = types.SimpleNamespace(events=types.SimpleNamespace(
            record=self.record, recent_type=self.recent),
            work=types.SimpleNamespace(land_lock=lambda *a, **kw: 1))
        patches = [
            mock.patch.object(executor.storage, "of", return_value=store),
            mock.patch.object(executor.role_run, "live", side_effect=lambda *_: dict(
                held=True, holder=self.cfg.host)),
            mock.patch.object(executor, "ExecutionLease"),
            mock.patch.object(executor.role_run, "_AlertsExecution"),
            mock.patch.object(executor.role_run, "_run_under_lease", side_effect=self.run_child),
            mock.patch.object(executor.subprocess, "run"),
            mock.patch.object(release_cutoff, "_worktree", return_value=("folder", "tree")),
            mock.patch.object(release_cutoff, "_drop_worktree"),
        ]
        for patch in patches:
            self.addCleanup(patch.stop)
            patch.start()
        executor.ExecutionLease.return_value.acquire.return_value = 1
        executor.ExecutionLease.return_value.remaining.return_value = 60
        executor.ExecutionLease.return_value.epoch = 1
        executor.role_run._AlertsExecution.return_value.lost.is_set.return_value = False

    def record(self, kind, key, project, data):
        if kind == executor.EVENT:
            self.request = data
        return 1

    def recent(self, kind, days, limit, object=None):
        if kind == executor.EVENT:
            return [dict(data=self.request)] if self.request else []
        return self.proofs

    def run_child(self, argv, env, *_):
        self.calls.append((argv, env))
        if argv[0] == "bash":
            self.proofs = [dict(data=dict(state="completed"))]
        return 0, False

    def test_interrupted_a_is_taken_over_by_b_after_quiescence_proof(self):
        self.request.update(state="running", host="host-a", run="attempt-a")
        self.cfg.host = "host-b"
        result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["host"], "host-b")
        self.assertEqual(self.calls[0][0], self.options["quiescence_probe"])
        self.assertEqual(self.calls[0][1]["STAGING_PREVIOUS_RUN"], "attempt-a")
        self.assertEqual(self.calls[1][0][0], "bash")
        self.assertEqual(self.calls[1][1]["STAGING_RUN_ID"], result["run"])
        self.assertNotEqual(result["run"], "attempt-a")

    def test_takeover_refused_and_journaled_without_quiescence(self):
        self.request.update(state="running", host="host-a", run="attempt-a")
        self.options.pop("quiescence_probe")
        self.cfg.host = "host-b"
        result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "blocked")
        self.assertIn("quiescence", self.request["blocked_reason"])
        self.assertEqual(self.calls, [])

    def test_negative_probe_never_starts_deployment(self):
        self.request.update(state="running", host="host-a", run="attempt-a")
        executor.role_run._run_under_lease.side_effect = lambda *a: (1, False)
        result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "blocked")
        release_cutoff._worktree.assert_not_called()

    def test_busy_shared_lease_or_other_holder_has_no_effect(self):
        executor.ExecutionLease.return_value.acquire.return_value = None
        self.assertEqual(executor.tick(self.db, self.cfg, self.options)["state"], "standby")
        executor.role_run.live.return_value = dict(held=True, holder="host-b")
        executor.role_run.live.side_effect = None
        self.assertEqual(executor.tick(self.db, self.cfg, self.options)["state"], "standby")
        self.assertEqual(self.calls, [])

    def test_missing_candidate_access_blocks_before_host_operations(self):
        self.bascule.unlink()
        self.assertEqual(executor.tick(self.db, self.cfg, self.options)["state"], "blocked")
        self.assertEqual(self.calls, [])

    def test_partial_success_does_not_require_quiescence_on_next_request(self):
        def partial(*args):
            self.proofs = [dict(data=dict(state="partial"))]
            return 0, False
        executor.role_run._run_under_lease.side_effect = partial
        self.assertEqual(executor.tick(self.db, self.cfg, self.options)["state"], "partial")
        queued = executor.submit(self.db, self.cfg, self.options, "b" * 40)
        self.assertNotIn("previous_run", queued)

    def test_candidate_fetches_before_resolving_new_commit(self):
        executor.tick(self.db, self.cfg, self.options)
        calls = executor.subprocess.run.call_args_list
        self.assertEqual(calls[0].args[0][3:], ["fetch", "--no-tags", "origin"])
        self.assertEqual(calls[0].kwargs["timeout"], 60)
        self.assertEqual(calls[1].args[0][3], "merge-base")

    def test_zero_exit_without_attestation_is_failed(self):
        executor.role_run._run_under_lease.side_effect = lambda *a: (0, False)
        self.assertEqual(executor.tick(self.db, self.cfg, self.options)["state"], "failed")

    def test_resubmitting_failed_attempt_keeps_quiescence_requirement(self):
        self.request.update(state="failed", host="host-a", run="failed-a")
        queued = executor.submit(self.db, self.cfg, self.options, "b" * 40)
        self.assertEqual(queued["previous_run"], "failed-a")
        self.cfg.host = "host-b"
        result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.calls[0][1]["STAGING_PREVIOUS_RUN"], "failed-a")

    def test_blocked_running_can_be_replaced_only_with_quiescence(self):
        self.request.update(state="running", blocked_reason="would roll back active content",
                            host="host-a", run="blocked-a")
        queued = executor.submit(self.db, self.cfg, self.options, "b" * 40)
        self.assertEqual(queued["previous_run"], "blocked-a")
        self.assertNotIn("blocked_reason", queued)
        self.options.pop("quiescence_probe")
        self.assertEqual(executor.tick(self.db, self.cfg, self.options)["state"], "blocked")
        self.assertEqual(self.calls, [])
        queued = executor.submit(self.db, self.cfg, self.options, "c" * 40)
        self.assertEqual(queued["previous_run"], "blocked-a")
        self.assertEqual(queued["previous_host"], "host-a")
        self.options["quiescence_probe"] = ["prove-stopped"]
        self.assertEqual(executor.tick(self.db, self.cfg, self.options)["state"], "completed")
        self.assertEqual(self.calls[0][1]["STAGING_PREVIOUS_RUN"], "blocked-a")

    def test_blocked_pending_without_attempt_can_be_replaced(self):
        self.request["blocked_reason"] = "candidate would roll back active content"
        queued = executor.submit(self.db, self.cfg, self.options, "b" * 40)
        self.assertEqual(queued["sha"], "b" * 40)
        self.assertNotIn("previous_run", queued)

    def test_submit_idempotent_and_does_not_replace_unfinished_commit(self):
        current = executor.submit(self.db, self.cfg, self.options, "a" * 40)
        self.assertEqual(current["request"], "request-1")
        with self.assertRaisesRegex(RuntimeError, "unfinished"):
            executor.submit(self.db, self.cfg, self.options, "b" * 40)
        self.assertEqual(self.calls, [])

    def test_automatic_release_queues_without_running_in_its_launcher(self):
        project = release_cutoff.ProjectRelease(
            name="test", repo=str(self.root), staging_executor="private.json")
        with mock.patch.object(executor, "settings", return_value=self.options), \
             mock.patch("ameesh.config.load", return_value=self.cfg):
            result = release_cutoff.run_staging(project, "a" * 40, [],
                                               state_dir=str(self.root), db=self.db)
        self.assertEqual(result["exit"], 75)
        self.assertEqual(result["request"], "request-1")
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
