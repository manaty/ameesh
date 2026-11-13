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

#: #5665: kept before ExecutorTest replaces it.
FINGERPRINTS = executor.fingerprints
#: #5669: the real tree guard, kept before ExecutorTest replaces it.
TREE_PROBLEM = executor.tree_problem


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
                            github="o/r", config_path=str(self.root / "staging.json"),
                            quiescence_probe=["prove-old-and-remote-operations-stopped"])
        self.cfg = types.SimpleNamespace(host="host-a", config_path="",
                                         dsn="", schema="")
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
            # #5500: the activation conditions have their own tests.
            mock.patch.object(executor, "activation_problem", return_value=None),
            # #5665: the fingerprints have their own tests (FingerprintTest).
            mock.patch.object(executor, "fingerprints", return_value=({}, None)),
            mock.patch.object(executor, "tree_problem", return_value=None),
            mock.patch.object(executor, "forge_ci", return_value=(None, "CI green")),
            # #6761: the preparation's own tests are SequenceConfigTest.
            mock.patch.object(executor, "sequence_problem", return_value=None),
            mock.patch.object(executor, "publish_sequence_readiness"),
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

    def test_submit_refuses_a_blocked_request_for_the_same_commit(self):
        # Third review witness #6520: the free lease and the same commit used
        # to return the blocked request as a normal queued handoff.
        self.request["blocked_reason"] = "another staging sequence running: old"
        with self.assertRaisesRegex(RuntimeError, "staging request blocked"):
            executor.submit(self.db, self.cfg, self.options, "a" * 40)

    # -- #6776: the start deadline expires a request; cancel is traced --------

    def test_pending_past_its_start_deadline_expires_without_attempt(self):
        self.request.update(deadline=time.time() - 1,
                            blocked_reason="forge CI: pending")
        result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], executor.EXPIRED)
        self.assertEqual(self.request["state"], executor.EXPIRED)
        # The refusal stays apart from the expiry.
        self.assertNotIn("blocked_reason", self.request)
        self.assertEqual(self.request["last_blocked_reason"], "forge CI: pending")
        self.assertEqual(self.calls, [])
        executor.subprocess.run.assert_not_called()

    def test_pending_before_its_deadline_is_taken(self):
        self.request["deadline"] = time.time() + 300
        self.assertEqual(executor.tick(self.db, self.cfg, self.options)["state"],
                         "completed")

    def test_deadline_reached_during_preparation_expires_before_attempt(self):
        self.request["deadline"] = time.time() + 300
        # Checked on reading, then again after the preparation.
        with mock.patch.object(executor, "start_expired", side_effect=[False, True]):
            result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], executor.EXPIRED)
        self.assertEqual(self.request["state"], executor.EXPIRED)
        self.assertFalse([argv for argv, _ in self.calls if argv[0] == "bash"])

    def test_expired_request_is_never_taken_after_a_takeover(self):
        self.request.update(state=executor.EXPIRED, deadline=time.time() - 1)
        self.cfg.host = "host-b"
        self.assertEqual(executor.tick(self.db, self.cfg, self.options),
                         {"state": "idle"})
        self.assertEqual(self.calls, [])

    def test_running_request_never_expires_on_its_start_deadline(self):
        self.assertFalse(executor.start_expired(
            dict(state="running", deadline=1), now=10))
        self.assertFalse(executor.start_expired(dict(state="pending"), now=10))

    def test_cancel_of_a_pending_request_is_traced(self):
        self.request["blocked_reason"] = "another staging sequence running: old"
        data = executor.cancel(self.db, self.cfg, self.options, "request-1",
                               reason="drain AG1", actor="agent:pilot")
        self.assertEqual(self.request, data)
        self.assertEqual(data["state"], executor.CANCELLED)
        self.assertEqual(data["cancelled_by"], "agent:pilot")
        self.assertEqual(data["reason"], "drain AG1")
        self.assertIn("cancelled_at", data)
        self.assertEqual(executor.tick(self.db, self.cfg, self.options),
                         {"state": "idle"})
        # A new submit may follow.
        self.assertEqual(executor.submit(self.db, self.cfg, self.options,
                                         "b" * 40)["state"], "pending")

    def test_cancel_refuses_a_running_request(self):
        self.request.update(state="running", host="host-a", run="attempt-a")
        with self.assertRaisesRegex(RuntimeError, "never killed"):
            executor.cancel(self.db, self.cfg, self.options, "request-1",
                            reason="drain", actor="agent:pilot")
        self.assertEqual(self.request["state"], "running")

    def test_cancel_refuses_another_request_terminal_or_no_reason(self):
        with self.assertRaisesRegex(RuntimeError, "not the current"):
            executor.cancel(self.db, self.cfg, self.options, "other",
                            reason="x", actor="agent:pilot")
        with self.assertRaisesRegex(ValueError, "reason"):
            executor.cancel(self.db, self.cfg, self.options, "request-1",
                            reason=" ", actor="agent:pilot")
        self.request["state"] = "completed"
        with self.assertRaisesRegex(RuntimeError, "terminal"):
            executor.cancel(self.db, self.cfg, self.options, "request-1",
                            reason="x", actor="agent:pilot")

    def test_cancel_waits_for_a_free_execution_lease(self):
        executor.ExecutionLease.return_value.acquire.return_value = None
        with self.assertRaisesRegex(RuntimeError, "busy"):
            executor.cancel(self.db, self.cfg, self.options, "request-1",
                            reason="x", actor="agent:pilot")
        self.assertEqual(self.request["state"], "pending")

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
        # #6520: the request is tied to this execution before the wait, so a
        # reader never takes the held lease for a foreign staging.
        self.assertEqual(self.request["execution"],
                         executor.ExecutionLease.return_value.owner)
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

    def test_a_sequence_config_refusal_blocks_before_any_take(self):
        executor.sequence_problem.return_value = "different base (db; executor other)"
        result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "blocked")
        self.assertTrue(result["reason"].startswith(executor.SEQUENCE_REFUSAL))
        # the request stays pending for another candidate: no run, no host act
        self.assertEqual(self.request["state"], "pending")
        self.assertEqual(self.request["blocked_reason"], result["reason"])
        self.assertIn("sequence_config", self.request)
        executor.role_run._run_under_lease.assert_not_called()

    def test_a_previous_reason_does_not_survive_a_new_failure(self):
        self.request.update(reason="old failure", exit=2)
        executor.role_run._run_under_lease.side_effect = lambda *a, **k: (1, False)
        result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "failed")
        self.assertNotIn("reason", result)

    def test_a_failed_sequence_reason_is_copied_redacted_on_the_request(self):
        def failing(argv, env, *_args, **_kwargs):
            self.calls.append((argv, env))
            self.proofs = [dict(data=dict(
                state="failed",
                reason="installer ko postgres://user:secret@db/ameesh token=abc"))]
            return 1, False
        executor.role_run._run_under_lease.side_effect = failing
        result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["exit"], 1)
        self.assertIn("<dsn>", result["reason"])
        self.assertNotIn("secret", result["reason"])
        self.assertNotIn("user:", result["reason"])
        self.assertEqual(self.request["reason"], result["reason"])


class SequenceConfigTest(unittest.TestCase):
    """#6761: the sequence resolves the executor's base, or refuses by name."""

    DSN = "postgresql://user:secret@db:5432/ameesh"

    def cfg(self, path=""):
        return types.SimpleNamespace(host="host-a", config_path=path,
                                     dsn=self.DSN, schema="public")

    def test_the_loaded_file_and_dsn_source_are_transmitted_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "config.json")
            Path(path).write_text("{}", encoding="utf-8")
            env = {"AMEESH_CONFIG": "/other.json",
                   "AGENT_MESH_CONFIG": "/legacy.json",
                   "AMEESH_DSN": self.DSN,
                   "AMEESH_DATABASE_URL": "postgresql://user:secret@other/db"}
            descriptor = executor.sequence_config(self.cfg(path), env)
        self.assertEqual(env["AMEESH_CONFIG"], path)
        # the source is named, never quoted: no secret in the child's argv
        self.assertEqual(env["AMEESH_SEQUENCE_DSN"], "AMEESH_DSN")
        self.assertEqual(env["AMEESH_DSN"], self.DSN)
        self.assertNotIn("AMEESH_DATABASE_URL", env)
        self.assertNotIn("AGENT_MESH_CONFIG", env)
        self.assertEqual(descriptor["path"], path)
        self.assertEqual(descriptor["source"], "file:env:AMEESH_DSN")
        self.assertEqual(descriptor["schema"], "public")
        self.assertNotIn("secret", descriptor["dsn"])

    def test_without_a_read_file_the_sequence_gets_no_inherited_config(self):
        env = {"AMEESH_CONFIG": "/elsewhere.json", "AGENT_MESH_CONFIG": "/x",
               "AMEESH_DSN": self.DSN}
        descriptor = executor.sequence_config(self.cfg(""), env)
        self.assertNotIn("AMEESH_CONFIG", env)
        self.assertNotIn("AGENT_MESH_CONFIG", env)
        self.assertEqual(descriptor["path"], "")
        self.assertEqual(descriptor["source"], "default:env:AMEESH_DSN")

    def test_a_unit_without_the_current_name_still_gets_the_file_read(self):
        # #6761 witness: the unit exports no AMEESH_CONFIG (AG1), the executor
        # resolved its configuration another way; the sequence gets the file
        # the executor actually read, not the ambient alias.
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "profile.json")
            Path(path).write_text("{}", encoding="utf-8")
            env = {"AGENT_MESH_CONFIG": "/stale.json", "AMEESH_DSN": self.DSN}
            descriptor = executor.sequence_config(self.cfg(path), env)
        self.assertEqual(env["AMEESH_CONFIG"], path)
        self.assertNotIn("AGENT_MESH_CONFIG", env)
        self.assertEqual(descriptor["path"], path)

    def test_public_reason_bounds_and_redacts_a_sequence_error(self):
        reason = executor.public_reason(
            "postgres://user:secret@db/x password=hunter2 token=abc " + "x" * 500)
        self.assertNotIn("secret", reason)
        self.assertNotIn("hunter2", reason)
        self.assertNotIn("postgres://", reason)
        self.assertIn("<dsn>", reason)
        self.assertLessEqual(len(reason), 300)
        self.assertEqual(executor.public_reason(""), "")

    def test_an_unresolvable_config_is_a_named_refusal(self):
        with mock.patch("ameesh.config.load",
                        side_effect=SystemExit("config file not found: /x")):
            problem = executor.sequence_problem(self.cfg(), {})
        self.assertTrue(problem.startswith("unresolvable ("))
        self.assertIn("config file not found", problem)

    def test_an_empty_dsn_is_refused_never_the_local_socket(self):
        resolved = types.SimpleNamespace(dsn="", schema="")
        cfg = types.SimpleNamespace(host="host-a", config_path="",
                                    dsn="", schema="")
        with mock.patch("ameesh.config.load", return_value=resolved), \
             mock.patch("ameesh.db.connect") as connect:
            problem = executor.sequence_problem(cfg, {})
        self.assertIn("empty dsn", problem)
        self.assertIn("local socket", problem)
        connect.assert_not_called()

    def test_another_base_is_refused_before_any_connection(self):
        resolved = types.SimpleNamespace(dsn="postgresql://user:secret@other/db",
                                         schema="public")
        with mock.patch("ameesh.config.load", return_value=resolved), \
             mock.patch("ameesh.db.connect") as connect:
            problem = executor.sequence_problem(self.cfg(), {})
        self.assertTrue(problem.startswith("different base"))
        connect.assert_not_called()

    def test_an_unreachable_base_is_a_named_refusal(self):
        resolved = types.SimpleNamespace(dsn=self.DSN, schema="public")
        with mock.patch("ameesh.config.load", return_value=resolved), \
             mock.patch("ameesh.db.connect",
                        side_effect=RuntimeError("dial tcp: refused")), \
             mock.patch("ameesh.db.explain",
                        return_value="dial tcp: refused"):
            problem = executor.sequence_problem(self.cfg(), {})
        self.assertTrue(problem.startswith("unreachable"))
        self.assertIn("refused", problem)

    def test_the_probe_connects_and_selects_on_the_same_base(self):
        resolved = types.SimpleNamespace(dsn=self.DSN, schema="public")
        connection = mock.Mock()
        with mock.patch("ameesh.config.load", return_value=resolved), \
             mock.patch("ameesh.db.connect", return_value=connection):
            self.assertIsNone(executor.sequence_problem(self.cfg(), {}))
        connection.query.assert_called_once_with("select 1 as sequence_probe")
        connection.close.assert_called_once()

    def test_the_quiescence_targets_are_all_reachable_or_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bascule.env"
            path.write_text('HOTES_STAGING="ameesh-agents-1=root@a1 '
                            'ameesh-agents-2=root@a2 pc-smichea=poste"\n'
                            'QUIESCENCE_ABSENTS="ameesh-agents-2"\n',
                            encoding="utf-8")
            path.chmod(0o600)
            done = types.SimpleNamespace(returncode=0, stdout="", stderr="")
            with mock.patch.object(executor, "_program",
                                   return_value="ssh"), \
                 mock.patch.object(executor, "path_problem",
                                   return_value=None), \
                 mock.patch.object(executor.subprocess, "run",
                                   return_value=done) as run:
                problem = executor.quiescence_access_problem(
                    {}, {"bascule_env": str(path)})
        self.assertIsNone(problem)
        self.assertEqual(run.call_args.args[0],
                         ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                          "root@a1", "true"])

    def test_an_unreachable_target_is_a_named_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bascule.env"
            path.write_text('HOTES_STAGING="ameesh-agents-1=root@a1"\n',
                            encoding="utf-8")
            path.chmod(0o600)
            done = types.SimpleNamespace(
                returncode=255, stdout="",
                stderr="ssh: connect to host a1: refused")
            with mock.patch.object(executor, "_program",
                                   return_value="ssh"), \
                 mock.patch.object(executor, "path_problem",
                                   return_value=None), \
                 mock.patch.object(executor.subprocess, "run",
                                   return_value=done):
                problem = executor.quiescence_access_problem(
                    {}, {"bascule_env": str(path)})
        self.assertTrue(problem.startswith(
            "quiescence access: ameesh-agents-1 unreachable"))
        self.assertIn("refused", problem)

    def test_a_sequence_problem_includes_the_quiescence_access(self):
        resolved = types.SimpleNamespace(dsn=self.DSN, schema="public")
        with mock.patch("ameesh.config.load", return_value=resolved), \
             mock.patch("ameesh.db.connect"), \
             mock.patch.object(executor, "quiescence_access_problem",
                               return_value="quiescence access: x"):
            problem = executor.sequence_problem(
                self.cfg(), {}, {"bascule_env": "/b"})
        self.assertEqual(problem, "quiescence access: x")

    def test_a_failed_preparation_is_published_for_observation(self):
        events = mock.Mock()
        events.recent_type.return_value = []
        store = types.SimpleNamespace(events=events)
        cfg = types.SimpleNamespace(host="host-a")
        with mock.patch.object(executor.storage, "of", return_value=store):
            executor.publish_sequence_readiness(
                object(), cfg, {"role": "mesh-design", "key": "mesh"},
                "unreachable (dial tcp)", {"path": "/c", "source": "file"})
        events.record.assert_called_once()
        kind, obj, _project, data = events.record.call_args.args
        self.assertEqual(kind, executor.SEQUENCE_EVENT)
        self.assertEqual(obj, "staging-executor:mesh-design:host-a")
        self.assertFalse(data["ready"])
        self.assertEqual(data["diagnostic"], "unreachable (dial tcp)")

    def test_a_recovery_is_published_at_once(self):
        events = mock.Mock()
        events.recent_type.return_value = [
            dict(data={"ready": False}, ts=time.time())]
        store = types.SimpleNamespace(events=events)
        with mock.patch.object(executor.storage, "of", return_value=store):
            executor.publish_sequence_readiness(
                object(), types.SimpleNamespace(host="host-a"),
                {"role": "mesh-design", "key": "mesh"}, None, {})
        events.record.assert_called_once()
        self.assertTrue(events.record.call_args.args[3]["ready"])


    def test_a_changed_executor_version_is_published_at_once(self):
        """#7259: the tick carries the version it runs."""
        events = mock.Mock()
        events.recent_type.return_value = [dict(data={
            "ready": True, "executor_venv": {"running": "1", "live": "1"}},
            ts=time.time())]
        store = types.SimpleNamespace(events=events)
        with mock.patch.object(executor.storage, "of", return_value=store), \
             mock.patch.object(executor, "executor_venv",
                               return_value={"running": "2", "live": "2"}):
            executor.publish_sequence_readiness(
                object(), types.SimpleNamespace(host="host-a"),
                {"role": "mesh-design", "key": "mesh"}, None, {})
        events.record.assert_called_once()
        self.assertEqual(events.record.call_args.args[3]["executor_venv"],
                         {"running": "2", "live": "2"})


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


class FingerprintTest(unittest.TestCase):
    """#5665: the probe, its scripts and its notifier are read and
    fingerprinted before any host operation (ownership: #5500 tests)."""
    record = ExecutorTest.record
    recent = ExecutorTest.recent
    run_child = ExecutorTest.run_child

    def setUp(self):
        ExecutorTest.setUp(self)
        # #5669: the package tree is really guarded here; the service sets
        # umask 022, while this host's shell umask may create 664 fixtures.
        import os
        umask = os.umask(0o022)
        self.addCleanup(os.umask, umask)
        # #5665: the fingerprints have their own tests; only ownership and
        # paths are neutralized here.
        for patch in (mock.patch.object(executor, "fingerprints", FINGERPRINTS),
                      mock.patch.object(executor, "path_problem", return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)
        self.probe = self.write("bin/probe", "#!/bin/sh\n")
        self.script = self.write("quiescence-staging.sh", "exit 0\n")
        self.notifier = self.write("bin/notifier", "#!/bin/sh\n")
        self.options["quiescence_probe"] = [self.probe, self.script, "--flag"]
        patch = mock.patch.dict("os.environ", {"QUIESCENCE_NOTIFIER": self.notifier})
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return str(path.resolve())

    @staticmethod
    def digest(path):
        import hashlib
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def assert_refused_before_any_operation(self, result, text):
        self.assertEqual(result["state"], "blocked")
        self.assertTrue(result["reason"].startswith(executor.FINGERPRINT_REFUSAL))
        self.assertIn(text, result["reason"])
        self.assertEqual(self.calls, [])
        executor.subprocess.run.assert_not_called()
        release_cutoff._settle_poste_units.assert_not_called()
        release_cutoff._worktree.assert_not_called()

    def test_attempt_records_each_fingerprint(self):
        result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["fingerprints"], {
            path: self.digest(path) for path in (self.probe, self.script, self.notifier)})
        self.assertEqual(self.request["fingerprints"], result["fingerprints"])

    def test_refused_probe_records_what_ran(self):
        self.request.update(state="running", host="host-a", run="attempt-a")
        self.cfg.host = "host-b"
        self.run_child = lambda argv, env, *a, **kw: (self.calls.append((argv, env))
                                                      or (1, False))
        executor.role_run._run_under_lease.side_effect = self.run_child
        result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["reason"], "old execution not proved stopped")
        self.assertEqual(self.request["fingerprints"][self.probe], self.digest(self.probe))

    def test_unreadable_file_is_refused_before_any_operation(self):
        import os
        if os.geteuid() == 0:
            self.skipTest("root reads any file")
        os.chmod(self.script, 0)
        self.addCleanup(os.chmod, self.script, 0o600)
        result = executor.tick(self.db, self.cfg, self.options)
        self.assert_refused_before_any_operation(result, "unreadable")
        self.assertEqual(self.request["blocked_reason"], result["reason"])

    def test_missing_notifier_is_refused(self):
        with mock.patch.dict("os.environ", {"QUIESCENCE_NOTIFIER": str(self.root / "absent")}):
            result = executor.tick(self.db, self.cfg, self.options)
        self.assert_refused_before_any_operation(result, "notifier not found")

    def test_expected_digest(self):
        self.options["expected_sha256"] = {self.notifier: self.digest(self.notifier)}
        self.assertEqual(executor.tick(self.db, self.cfg, self.options)["state"], "completed")
        for expected, text in (({self.notifier: "0" * 64}, "expected " + "0" * 64),
                               ({str(self.root / "other"): "0" * 64}, "expected but not run")):
            with self.subTest(text):
                self.calls.clear()
                executor.subprocess.run.reset_mock()
                release_cutoff._worktree.reset_mock()
                self.request = dict(request="request-2", sha="a" * 40, state="pending")
                self.options["expected_sha256"] = expected
                result = executor.tick(self.db, self.cfg, self.options)
                self.assert_refused_before_any_operation(result, text)

    def test_expected_digest_declaration_is_validated(self):
        import json
        base = dict(key="k", project="p", role="r", repo="/r", bascule_env="/b",
                    github="o/r", candidates=["a", "b"])
        for bad in ({"relative": "0" * 64}, {"/abs": "short"}, ["/abs"]):
            with self.subTest(bad=bad):
                path = self.root / "settings.json"
                path.write_text(json.dumps(dict(base, expected_sha256=bad)))
                with self.assertRaises(ValueError):
                    executor.settings(str(path))

    def test_python_notifier_fingerprints_its_ameesh_package(self):
        python = self.write("venv/bin/python", "binary")
        package = self.root / "venv/lib/python3.12/site-packages/ameesh"
        self.write("venv/lib/python3.12/site-packages/ameesh/mail.py", "send = 1\n")
        with mock.patch.dict("os.environ", {"QUIESCENCE_NOTIFIER": "",
                                            "QUIESCENCE_PYTHON": python}):
            first, problem = executor.fingerprints(self.options, dict(executor.os.environ))
            self.assertIsNone(problem)
            self.assertEqual(first[python], self.digest(python))
            (package / "__pycache__").mkdir()
            (package / "__pycache__" / "mail.pyc").write_bytes(b"cache")
            self.assertEqual(executor.fingerprints(
                self.options, dict(executor.os.environ))[0], first)
            (package / "db.py").write_text("")
            self.assertNotEqual(executor.fingerprints(
                self.options, dict(executor.os.environ))[0][str(package)],
                first[str(package)])
            import shutil
            shutil.rmtree(package)
            self.assertIn("package ameesh not found", executor.fingerprints(
                self.options, dict(executor.os.environ))[1])

    def test_bascule_env_notifier_overrides_the_unit_environment(self):
        other = self.write("bin/other-notifier", "#!/bin/sh\necho\n")
        self.bascule.write_text("X=1\nexport QUIESCENCE_NOTIFIER='%s'\n" % other)
        prints, problem = executor.fingerprints(self.options, dict(executor.os.environ))
        self.assertIsNone(problem)
        self.assertIn(other, prints)
        self.assertNotIn(self.notifier, prints)
        self.bascule.write_text("QUIESCENCE_NOTIFIER=$HOME/notifier\n")
        self.assertIn("not a plain path", executor.fingerprints(
            self.options, dict(executor.os.environ))[1])

    def test_bascule_env_accepts_the_literal_shell_forms(self):
        other = self.write("bin/other-notifier", "#!/bin/sh\necho\n")
        for line in ("QUIESCENCE_NOTIFIER='%s'" % other,
                     "export QUIESCENCE_NOTIFIER='%s'" % other,
                     "readonly QUIESCENCE_NOTIFIER='%s'" % other,
                     "declare -x QUIESCENCE_NOTIFIER='%s'" % other,
                     "declare -x -r QUIESCENCE_NOTIFIER='%s'" % other,
                     "typeset -rx QUIESCENCE_NOTIFIER='%s'" % other):
            with self.subTest(line=line):
                self.bascule.write_text(line + "\n")
                prints, problem = executor.fingerprints(
                    self.options, dict(executor.os.environ))
                self.assertIsNone(problem)
                self.assertIn(other, prints)
                self.assertNotIn(self.notifier, prints)

    def test_bascule_env_unset_clears_the_notifier(self):
        python = self.write("venv/bin/python", "binary")
        self.write("venv/lib/python3.12/site-packages/ameesh/mail.py", "send = 1\n")
        for unset in ("unset QUIESCENCE_NOTIFIER", "unset -v QUIESCENCE_NOTIFIER"):
            with self.subTest(unset=unset):
                self.bascule.write_text(
                    "QUIESCENCE_NOTIFIER='%s'\n%s\n" % (self.notifier, unset))
                with mock.patch.dict("os.environ",
                                     {"QUIESCENCE_NOTIFIER": self.notifier,
                                      "QUIESCENCE_PYTHON": python}):
                    prints, problem = executor.fingerprints(
                        self.options, dict(executor.os.environ))
                self.assertIsNone(problem)
                self.assertIn(python, prints)
                self.assertNotIn(self.notifier, prints)

    def test_bascule_env_assignment_after_unset_wins(self):
        self.bascule.write_text("unset QUIESCENCE_NOTIFIER\n"
                                "QUIESCENCE_NOTIFIER='%s'\n" % self.notifier)
        prints, problem = executor.fingerprints(self.options,
                                                dict(executor.os.environ))
        self.assertIsNone(problem)
        self.assertIn(self.notifier, prints)

    def test_bascule_env_refuses_an_unrecognized_mention(self):
        for line in ("readonly QUIESCENCE_NOTIFIER",
                     "declare -x QUIESCENCE_NOTIFIER",
                     "unset -f QUIESCENCE_NOTIFIER",
                     "QUIESCENCE_NOTIFIER+=%s" % self.notifier,
                     'if [ -n "$QUIESCENCE_NOTIFIER" ]; then :; fi'):
            with self.subTest(line=line):
                self.bascule.write_text(line + "\n")
                _prints, problem = executor.fingerprints(
                    self.options, dict(executor.os.environ))
                self.assertIn("not a recognized assignment", problem)

    def test_bascule_env_comment_naming_the_notifier_is_inert(self):
        self.bascule.write_text("# QUIESCENCE_NOTIFIER=/nope\n"
                                "export QUIESCENCE_NOTIFIER='%s'\n" % self.notifier)
        prints, problem = executor.fingerprints(self.options,
                                                dict(executor.os.environ))
        self.assertIsNone(problem)
        self.assertIn(self.notifier, prints)

    def test_writable_file_inside_the_notifier_package_is_refused(self):
        python = self.write("venv/bin/python", "binary")
        self.write("venv/lib/python3.12/site-packages/ameesh/mail.py", "send = 1\n")
        writable = (self.root
                    / "venv/lib/python3.12/site-packages/ameesh/mail.py")
        writable.chmod(0o666)
        self.addCleanup(writable.chmod, 0o644)
        with mock.patch.object(executor, "tree_problem", TREE_PROBLEM), \
                mock.patch.dict("os.environ", {"QUIESCENCE_NOTIFIER": "",
                                               "QUIESCENCE_PYTHON": python}):
            _prints, problem = executor.fingerprints(
                self.options, dict(executor.os.environ))
        self.assertIn("writable by group or others", problem)
