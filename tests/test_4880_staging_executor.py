# SPDX-License-Identifier: AGPL-3.0-only
"""Simulated candidate takeover; no SSH, service or real deployment."""
from contextlib import nullcontext
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from ameesh import (release_cutoff, role_run, staging_executor as executor,
                    staging_watch)


def running(run, deadline=600):
    """#4885: a sequence of another path, in flight (deadline relative)."""
    return dict(id=1, data=dict(run=run, sha="a" * 40, state="running",
                                started=1, deadline=time.time() + deadline))


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
        self.attested = []
        self.calls = []
        self.renewals = []
        self.held = False
        #: #4885: sequences of other paths in the shared progress log
        self.foreign = []
        self.db = types.SimpleNamespace()
        self.db.transaction = lambda: nullcontext(self.db)
        store = types.SimpleNamespace(events=types.SimpleNamespace(
            record=self.record, recent_type=self.recent,
            latest_type=lambda kind, *a: (self.attested if kind == release_cutoff.STAGING_EVENT
                                         else self.foreign)),
            work=types.SimpleNamespace(land_lock=lambda *a, **kw: 1))
        def renewing(lease, execution, stop):
            self.renewals.append(stop)
            stop.wait(5)
        patches = [
            mock.patch.object(executor.storage, "of", return_value=store),
            mock.patch.object(executor.role_run, "live", side_effect=lambda *_: dict(
                held=True, holder=self.cfg.host)),
            mock.patch.object(executor, "ExecutionLease"),
            mock.patch.object(executor.role_run, "_AlertsExecution"),
            mock.patch.object(executor.role_run, "_watch_alerts_lease",
                              side_effect=renewing),
            mock.patch.object(executor.role_run, "_run_under_lease", side_effect=self.run_child),
            mock.patch.object(executor.subprocess, "run"),
            mock.patch.object(release_cutoff, "_worktree", return_value=("folder", "tree")),
            mock.patch.object(release_cutoff, "_drop_worktree"),
            # #5450: the executor settles the workstation units before any
            # terminal release; a test overrides the outcome when it matters.
            mock.patch.object(release_cutoff, "_settle_poste_units",
                              return_value=("", True)),
        ]
        for patch in patches:
            self.addCleanup(patch.stop)
            patch.start()
        executor.ExecutionLease.return_value.acquire.return_value = 1
        executor.ExecutionLease.return_value.remaining.return_value = 60
        executor.ExecutionLease.return_value.epoch = 1
        executor.ExecutionLease.return_value.ttl = 60.0
        executor.role_run._AlertsExecution.return_value.lost.is_set.return_value = False
        # `$$` of the bash group, the pid that names the workstation units.
        executor.role_run._AlertsExecution.return_value.pgid = 1234

    def record(self, kind, key, project, data):
        if kind == executor.EVENT:
            self.request = data
        return 1

    def recent(self, kind, days, limit, object=None):
        if kind == executor.EVENT:
            return [dict(data=self.request)] if self.request else []
        if kind == release_cutoff.STAGING_EVENT:
            return self.attested
        return self.proofs

    def run_child(self, argv, env, *_args, **_kwargs):
        self.calls.append((argv, env))
        if argv[0] == "bash":
            self.proofs = [dict(data=dict(state="completed"))]
        return 0, False

    def test_staging_scripts_receive_explicit_transport_credentials(self):
        from ameesh.platform import child_environment as E
        with mock.patch.dict("os.environ", {"GH_TOKEN": "test-gh", "AMEESH_DSN": "test-db",
                                            "OPENAI_API_KEY": "other"}):
            executor.tick(self.db, self.cfg, self.options)
        self.assertTrue(self.calls)
        for argv, env in self.calls:
            clean = E.for_child(argv, env)
            self.assertEqual(clean.get("GH_TOKEN"), "test-gh")
            self.assertEqual(clean.get("AMEESH_DSN"), "test-db")
            self.assertNotIn("OPENAI_API_KEY", clean)

    def test_attested_foreign_run_allows_next_attempt(self):
        self.foreign = [running("previous")]
        self.attested = [dict(id=2, data=dict(sha="a" * 40))]
        self.assertEqual(executor.tick(self.db, self.cfg, self.options)["state"], "completed")
        self.assertTrue(self.calls)

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
        executor.role_run._run_under_lease.side_effect = lambda *a, **k: (1, False)
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
        def partial(*args, **kwargs):
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
        executor.role_run._run_under_lease.side_effect = lambda *a, **k: (0, False)
        self.assertEqual(executor.tick(self.db, self.cfg, self.options)["state"], "failed")

    def test_missing_terminal_snapshot_is_settled_by_the_attestation(self):
        def child(*args, **kwargs):
            self.proofs = []
            self.attested = [dict(id=5, data=dict(sha="a" * 40))]
            return 0, False
        executor.role_run._run_under_lease.side_effect = child
        self.assertEqual(executor.tick(self.db, self.cfg, self.options)["state"],
                         "completed")

    def test_attestation_older_than_the_attempt_does_not_settle_it(self):
        def child(*args, **kwargs):
            self.proofs = []
            self.attested = [dict(id=1, data=dict(sha="a" * 40))]
            return 0, False
        executor.role_run._run_under_lease.side_effect = child
        self.assertEqual(executor.tick(self.db, self.cfg, self.options)["state"],
                         "failed")

    def test_one_renewal_loop_covers_prelaunch_and_child(self):
        self.request.update(state="running", host="host-a", run="attempt-a")
        self.assertEqual(executor.tick(self.db, self.cfg, self.options)["state"],
                         "completed")
        self.assertEqual(len(self.renewals), 1)
        self.assertTrue(self.renewals[0].is_set())
        self.assertEqual(executor.role_run._run_under_lease.call_count, 2)
        for call in executor.role_run._run_under_lease.call_args_list:
            self.assertIs(call.kwargs.get("watch"), False)

    def test_the_child_is_monitored_and_a_stop_is_a_recorded_failure(self):
        release_cutoff._settle_poste_units.return_value = (
            "; workstation unit(s) poste-maj-staging-1234-retour.service ended "
            "before the worktree was dropped", True)

        def child(*args, **kwargs):
            self.calls.append((args[0], args[1]))
            executor.role_run._AlertsExecution.return_value.stop_reason = \
                "délai dépassé : figé"
            return 143, False
        executor.role_run._run_under_lease.side_effect = child
        result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "délai dépassé : figé")
        self.assertEqual(self.request["state"], "failed")
        self.assertEqual(self.request["reason"], "délai dépassé : figé")
        self.assertIn("ended before the worktree was dropped",
                      self.request["unit_note"])
        release_cutoff._settle_poste_units.assert_called_once_with(1234, mock.ANY)
        release_cutoff._drop_worktree.assert_called_once_with(
            str(self.root.resolve()), "folder", "tree")
        call = executor.role_run._run_under_lease.call_args_list[-1]
        self.assertEqual(call.kwargs["stop_grace"], staging_watch.STOP_GRACE_S)
        self.assertEqual(call.kwargs["interval"], staging_watch.WATCH_INTERVAL_S)
        self.assertEqual(call.kwargs["monitor"].__self__.run, result["run"])

    def test_a_unit_surviving_the_leader_keeps_the_worktree_and_blocks(self):
        """#5450 B/P1 witness: the group is dead, a workstation unit is not.

        No terminal state and no worktree deletion may precede the proved
        stop; the request stays running with the guard that keeps every later
        start behind the quiescence probe.
        """
        release_cutoff._settle_poste_units.return_value = (
            "; workstation unit(s) poste-maj-staging-1234-retour.service still "
            "running after 600s and a stop: worktree kept, check the "
            "workstation", False)

        def child(*args, **kwargs):
            self.calls.append((args[0], args[1]))
            executor.role_run._AlertsExecution.return_value.stop_reason = \
                "délai dépassé : figé"
            return 143, False
        executor.role_run._run_under_lease.side_effect = child
        with mock.patch.object(executor.staging_watch, "Monitor") as monitor_cls:
            result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "blocked")
        self.assertIn("still running", result["reason"])
        self.assertEqual(self.request["state"], "running")
        self.assertIn("blocked_reason", self.request)
        self.assertIn("quiescence", self.request["blocked_reason"])
        self.assertEqual(self.request["unit_pid"], 1234)
        monitor_cls.return_value.stop.assert_not_called()
        release_cutoff._drop_worktree.assert_not_called()
        release_cutoff._settle_poste_units.assert_called_once_with(1234, mock.ANY)

    def test_a_proven_stop_runs_the_bounded_return_before_failed_and_drop(self):
        """#5450: settle -> return -> failed, in that order; guard until the end."""
        order = []
        release_cutoff._settle_poste_units.side_effect = lambda *a: (
            order.append("settle"), ("", True))[1]

        def retour(*args, **kwargs):
            order.append("return")
            return {"ran": True, "ok": True, "note": "retour apres arret: ok",
                    "mailed": False, "drop": True}

        def child(*args, **kwargs):
            self.calls.append((args[0], args[1]))
            executor.role_run._AlertsExecution.return_value.stop_reason = \
                "délai dépassé : figé"
            return 143, False

        executor.role_run._run_under_lease.side_effect = child
        with mock.patch.object(release_cutoff, "_return_after_stop",
                               side_effect=retour) as ret, \
             mock.patch.object(executor.staging_watch, "Monitor") as monitor_cls:
            monitor_cls.return_value.stop.side_effect = \
                lambda reason: order.append("stop")
            result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(order, ["settle", "return", "stop"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["return_note"], "retour apres arret: ok")
        self.assertEqual(ret.call_args.args[:2], ("tree", "a" * 40))
        release_cutoff._drop_worktree.assert_called_once()
        self.assertEqual(self.request["state"], "failed")

    def test_a_return_failure_without_mail_uses_the_manual_fallback(self):
        release_cutoff._settle_poste_units.return_value = ("", True)

        def child(*args, **kwargs):
            self.calls.append((args[0], args[1]))
            executor.role_run._AlertsExecution.return_value.stop_reason = \
                "délai dépassé : figé"
            return 143, False

        executor.role_run._run_under_lease.side_effect = child
        with mock.patch.object(release_cutoff, "_return_after_stop",
                               return_value=dict(ran=True, ok=False,
                                                 note="depassement de la borne",
                                                 mailed=False, drop=True)), \
             mock.patch.object(release_cutoff, "_manual_return_mail") as mail:
            result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "failed")
        mail.assert_called_once()
        self.assertIn("depassement", self.request["return_note"])
        release_cutoff._drop_worktree.assert_called_once()

    def test_a_return_not_proved_stopped_keeps_the_guard(self):
        release_cutoff._settle_poste_units.return_value = ("", True)

        def child(*args, **kwargs):
            self.calls.append((args[0], args[1]))
            executor.role_run._AlertsExecution.return_value.stop_reason = \
                "délai dépassé : figé"
            return 143, False

        executor.role_run._run_under_lease.side_effect = child
        with mock.patch.object(release_cutoff, "_return_after_stop",
                               return_value=dict(ran=True, ok=False,
                                                 note="unite encore vivante",
                                                 mailed=False, drop=False,
                                                 pid=4321)), \
             mock.patch.object(release_cutoff, "_manual_return_mail") as mail, \
             mock.patch.object(executor.staging_watch, "Monitor") as monitor_cls:
            result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(self.request["state"], "running")
        self.assertIn("retour apres arret non conclu",
                      self.request["blocked_reason"])
        self.assertIn("unite encore vivante", self.request["blocked_reason"])
        self.assertEqual(self.request["return_pid"], 4321)
        monitor_cls.return_value.stop.assert_not_called()
        release_cutoff._drop_worktree.assert_not_called()
        mail.assert_not_called()

    def test_a_guarded_unit_pid_is_settled_before_the_next_attempt(self):
        self.request.update(state="running", host="host-a", run="attempt-a",
                            unit_pid=4321)
        result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "completed")
        release_cutoff._settle_poste_units.assert_any_call(4321, mock.ANY)
        self.assertNotIn("unit_pid", self.request)

    def test_guarded_units_not_settled_block_before_any_host_operation(self):
        self.request.update(state="running", host="host-a", run="attempt-a",
                            return_pid=4321)
        release_cutoff._settle_poste_units.return_value = ("unit live", False)
        result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "blocked")
        self.assertIn("previous stop not proved", result["reason"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.request["state"], "running")
        self.assertEqual(self.request["return_pid"], 4321)

    def test_a_return_state_left_by_a_killed_supervisor_blocks_and_mails(self):
        """#5450: the next tick must not drop the tree and restart silently."""
        self.request.update(state="running", host="host-a", run="attempt-a")
        tree = self.root / "kept" / "worktree"
        tree.mkdir(parents=True)
        (tree / release_cutoff.STAGING_RETURN_STATE).write_text(
            "ameesh-agents-2\tinstaller\t0123456789abcdef\t"
            "vm.sh agents retour\t/tmp/journal.jsonl\t" + "a" * 40 + "\n")
        with mock.patch.object(executor, "_retained_trees",
                               return_value=[(str(tree.parent), str(tree))]), \
             mock.patch.object(release_cutoff, "_manual_return_mail") as mail, \
             mock.patch.object(executor, "_drop_retained") as drop:
            result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "blocked")
        self.assertIn("marche manuelle", result["reason"])
        self.assertIn("blocked_reason", self.request)
        mail.assert_called_once()
        drop.assert_not_called()
        with mock.patch.object(executor, "_retained_trees",
                               return_value=[(str(tree.parent), str(tree))]), \
             mock.patch.object(release_cutoff, "_manual_return_mail") as mail2, \
             mock.patch.object(executor, "_drop_retained") as drop2:
            again = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(again["state"], "blocked")
        mail2.assert_not_called()
        drop2.assert_not_called()

    def test_record_renewal_extends_the_local_deadline(self):
        executor.tick(self.db, self.cfg, self.options)
        executor.ExecutionLease.return_value.note_renewed.assert_called()

    def test_retained_worktree_is_removed_only_after_proven_stop(self):
        self.request.update(state="running", host="host-a", run="attempt-a")
        with mock.patch.object(executor, "_drop_retained") as retained:
            result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "completed")
        retained.assert_any_call(str(self.root.resolve()), "attempt-a")
        self.request.update(state="running", host="host-a", run="attempt-a")
        executor.role_run._run_under_lease.side_effect = lambda *a, **k: (1, False)
        with mock.patch.object(executor, "_drop_retained") as retained:
            result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "blocked")
        retained.assert_not_called()

    def test_worktree_is_named_after_the_attempt_run(self):
        result = executor.tick(self.db, self.cfg, self.options)
        prefix = release_cutoff._worktree.call_args.kwargs["prefix"]
        self.assertEqual(prefix, executor._worktree_prefix(result["run"]))
        self.assertTrue(prefix.startswith(executor.WORKTREE_PREFIX))

    def test_retained_trees_are_scanned_by_run_prefix(self):
        base = self.root / "base"
        (base / "repo").mkdir(parents=True)
        kept = base / ("ameesh-release-staging-deadbeef-1234")
        (kept / "worktree").mkdir(parents=True)
        other = base / "ameesh-release-staging-cafe-1234"
        (other / "worktree").mkdir(parents=True)
        with mock.patch.object(release_cutoff, "_drop_worktree") as drop:
            executor._drop_retained(str(base / "repo"), "deadbeef")
        drop.assert_called_once_with(str(base / "repo"), str(kept),
                                     str(kept / "worktree"))
        self.assertTrue(other.is_dir())

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

    def test_manual_sequence_blocks_elected_attempt_without_host_operation(self):
        self.foreign = [running("manual")]
        result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "blocked")
        self.assertIn("manual", self.request["blocked_reason"])
        self.assertEqual(self.request["state"], "pending")
        release_cutoff._worktree.assert_not_called()
        # The same wait is not journaled again on every timer pass.
        with mock.patch.object(executor, "_record") as record:
            executor.tick(self.db, self.cfg, self.options)
        record.assert_not_called()
        self.foreign = []
        self.assertEqual(executor.tick(
            self.db, self.cfg, self.options)["state"], "completed")

    def test_recovered_run_does_not_block_its_own_takeover(self):
        self.foreign = [running("attempt-a")]
        self.request.update(state="running", host="host-a", run="attempt-a")
        self.cfg.host = "host-b"
        self.assertEqual(executor.tick(
            self.db, self.cfg, self.options)["state"], "completed")

    def test_child_refused_at_start_requeues_request(self):
        def busy(argv, env, *_, **__):
            self.calls.append((argv, env))
            return staging_watch.BUSY, False
        executor.role_run._run_under_lease.side_effect = busy
        result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(self.request["state"], "pending")
        self.assertNotIn("run", self.request)

    def test_late_sequence_of_another_path_still_blocks(self):
        self.foreign = [running("manual", deadline=-60)]
        self.assertEqual(executor.tick(self.db, self.cfg, self.options)["state"], "blocked")
        release_cutoff._worktree.assert_not_called()

    def test_truncated_history_waits_instead_of_failing(self):
        self.foreign = [running("x-%d" % i, deadline=-1) for i in range(staging_watch.LIMIT)]
        for i, r in enumerate(self.foreign):
            r["data"]["state"] = "completed"
        result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "blocked")
        self.assertIn("truncated", self.request["blocked_reason"])
        self.assertEqual(self.request["state"], "pending")

    def test_unit_environment_cannot_supersede_without_proof(self):
        with mock.patch.dict("os.environ", STAGING_RUN_ID="fixed",
                             STAGING_PREVIOUS_RUN="other", STAGING_PREVIOUS_HOST="h"):
            result = executor.tick(self.db, self.cfg, self.options)
        env = self.calls[-1][1]
        self.assertEqual(env["STAGING_RUN_ID"], result["run"])
        self.assertNotIn("STAGING_PREVIOUS_RUN", env)
        self.assertNotIn("STAGING_PREVIOUS_HOST", env)


class GuardedStopTest(unittest.TestCase):
    """#5450: a monitored stop kills the group, not the lease."""

    def test_a_monitor_stop_halts_the_group_and_keeps_the_lease(self):
        import subprocess
        import threading

        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)

        def cleanup():
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.addCleanup(cleanup)

        class Execution:
            def __init__(self):
                self.lost = threading.Event()
                self.stop_reason = None
                self.halted = []

            @property
            def pgid(self):
                return proc.pid

            def start_child(self, argv, env):
                return proc

            def halt(self, reason, *, grace=None):
                self.stop_reason = reason
                self.halted.append(grace)
                role_run._stop_child(
                    proc, 0.2 if grace is None else grace, pgid=proc.pid)

        execution = Execution()
        checks = []

        def monitor():
            checks.append(1)
            return "délai dépassé : figé"

        code, lost = role_run._run_under_lease(
            ["sleep", "30"], {}, object(), execution, watch=False,
            monitor=monitor, interval=0.05, stop_grace=0.2)
        self.assertFalse(lost)
        self.assertFalse(execution.lost.is_set())
        self.assertEqual(execution.stop_reason, "délai dépassé : figé")
        self.assertGreaterEqual(len(checks), 1)
        self.assertNotEqual(code, 0)


class LeaseNoteTest(unittest.TestCase):
    def test_a_caller_renewal_extends_the_local_deadline(self):
        lease = role_run._AlertsLease(None, "pilot", "host-a")
        lease.epoch = 7
        lease.deadline = time.monotonic() + 1
        started = time.monotonic()
        self.assertTrue(lease.note_renewed(7, started))
        self.assertGreater(lease.remaining(), 40)
        # A renewal of another holder's epoch never extends ours.
        self.assertFalse(lease.note_renewed(8, started))


if __name__ == '__main__':
    unittest.main()
