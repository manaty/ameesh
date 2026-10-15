# SPDX-License-Identifier: AGPL-3.0-only
"""Real SQL receipts through the runtime, including final-gate races/restart."""
import types
from unittest import mock

from ameesh import land, merge_queue, train, train_ci, train_fire, train_runtime, train_store, work
from .support import PgTestCase
from .test_1467_train import MemoryPorts, row


class TrainRuntimeTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE merge_locks RESTART IDENTITY CASCADE")
        self.memory = MemoryPorts([row(1)])
        self.project = types.SimpleNamespace(name="runtime-test", github="o/r")
        self.ledger = train_store.Ledger(self.db, self.project, "main", "runner")
        self.fires = train_fire.Fires(None, self.ledger)
        self.repository = mock.Mock(project=self.project, source="github.com/o/r")
        self.repository.target.side_effect = lambda target: self.memory.target()
        self.repository.compose.side_effect = lambda target, base, rows, nonce: self.memory.compose(
            base, rows, nonce)
        self.repository.publish.side_effect = lambda plan, **kw: self.memory.effects.append(
            ("publish", plan["head"]))
        self.repository.promote.side_effect = lambda plan, **kw: self.memory.promote(plan, {}, kw["epoch"])
        self.ci = mock.Mock(slug="o/r", workflows=(train_ci.Workflow(
            ".github/workflows/tests.yml", ("suite",),
            sources=((".github/workflows/tests.yml", "d" * 40),)),))
        self.ci.read.side_effect = self.memory.ci
        self.admissions = mock.Mock()
        self.admissions.entries.side_effect = self.memory.entries
        self.admissions.qualify.side_effect = self.memory.qualify
        self.admissions.dependencies.side_effect = self.memory.dependencies
        self.policy = train.Policy(enabled=True, build_beta=False)
        self.role = mock.patch.object(train_fire.Fires, "_still_pilot", return_value=True)
        self.role.start()
        self.addCleanup(self.role.stop)
        self.runtime = self.runtime_for()
        self.engine = self.engine_for(self.runtime)

    def runtime_for(self, **kw):
        return train_runtime.Runtime(**dict(dict(ledger=self.ledger, repository=self.repository,
            ci=self.ci, fires=self.fires, admissions=self.admissions, target="main",
            policy=self.policy, notify=self.memory.notify), **kw))

    def engine_for(self, runtime):
        return train.Engine(runtime, source=self.repository.source, target="main", policy=self.policy)

    def test_queue_alias_is_refused_before_any_remote_effect(self):
        self.admissions.entries.side_effect = lambda: [dict(row(1), target="origin/main")]
        raw_key = lambda project, target: "%s:%s" % (project.name, target)
        with mock.patch.object(land, "lock_key", side_effect=raw_key):
            result = self.engine.step(3600)
        self.assertEqual(result["result"], "refused", result)
        self.assertIn("queue target alias", result["error"])
        self.repository.publish.assert_not_called()
        self.repository.promote.assert_not_called()

    def test_alias_can_land_only_when_every_target_uses_the_shared_canonical_key(self):
        # #1374's shared lock contract; retain the raw admission target in
        # the manifest so a queue change still invalidates its identity.
        key = lambda project, target: "%s:%s" % (project.name, merge_queue._remote_branch(target))
        self.memory.rows[0]["target"] = "origin/main"
        with mock.patch.object(land, "lock_key", side_effect=key):
            ledger = train_store.Ledger(self.db, self.project, "refs/heads/main", "other")
            self.assertEqual(ledger.key, self.ledger.key)
            plan, _ = self.ready()
            self.assertEqual(plan["members"][0]["target"], "origin/main")
            self.assertEqual(self.engine.step(3602)["result"], "landed")

    def ready(self):
        outcome = self.engine.step(3600)
        self.assertEqual(outcome["result"], "ci", outcome)
        plan = self.ledger.load()["plan"]
        self.memory.ci_by_head[plan["head"]] = "green"
        outcome = self.engine.step(3601)
        self.assertEqual(outcome["result"], "awaiting-fire", outcome)
        with mock.patch.object(work, "corrector", return_value="pilot"):
            fire = self.fires.give(plan, actor="pilot")
        return plan, fire

    def test_sql_roundtrip_of_engine_tuple_proofs_can_publish_and_land(self):
        plan, fire = self.ready()
        outcome = self.engine.step(3602)
        self.assertEqual(outcome["result"], "landed", outcome)
        current = self.ledger.ops.train_fire_read(self.ledger.key, plan["id"])
        self.assertEqual((current["state"], current["id"]), ("landed", fire["id"]))
        self.repository.publish.assert_called_once()
        self.assertEqual(self.repository.publish.call_args.kwargs["workflows"], self.ci.workflows)
        self.repository.promote.assert_called_once()

    def test_ci_rerun_after_engine_green_before_effect_retains_claim_without_push(self):
        plan, fire = self.ready()
        reads = 0
        def changed(plan):
            nonlocal reads
            reads += 1
            result = self.memory.ci(plan)
            if reads > 1:
                result.update(status="pending", complete=False)
            return result
        self.ci.read.side_effect = changed
        outcome = self.engine.step(3602)
        self.assertEqual(outcome["result"], "refused", outcome)
        self.repository.promote.assert_not_called()
        self.assertEqual(self.ledger.load()["phase"], "promoting")
        self.assertEqual(self.ledger.ops.train_fire_read(self.ledger.key, plan["id"])["state"],
                         "promoting")
        # Seeing the base on a fresh pass cannot authorize a replay.
        self.assertEqual(self.engine_for(self.runtime_for()).step(3603)["result"], "refused")
        self.repository.promote.assert_not_called()

    def test_withdrawal_after_authorization_before_claim_prevents_push(self):
        plan, fire = self.ready()
        authorize = self.runtime.authorize
        def withdraw(plan):
            receipt = authorize(plan)
            with mock.patch.object(work, "corrector", return_value="pilot"):
                self.fires.withdraw(plan, fire["id"], actor="pilot")
            return receipt
        with mock.patch.object(self.runtime, "authorize", side_effect=withdraw):
            outcome = self.engine.step(3602)
        self.assertEqual(outcome["result"], "refused", outcome)
        self.repository.promote.assert_not_called()
        self.assertEqual(self.ledger.ops.train_fire_read(self.ledger.key, plan["id"])["state"],
                         "revoked")

    def test_generation_changed_after_engine_validation_prevents_effect(self):
        plan, fire = self.ready()
        original = self.runtime.authorize
        def requeue(plan):
            receipt = original(plan)
            self.memory.rows[0]["generation"] += 1
            return receipt
        with mock.patch.object(self.runtime, "authorize", side_effect=requeue):
            outcome = self.engine.step(3602)
        self.assertEqual(outcome["result"], "refused", outcome)
        self.assertIn("requeued", outcome["error"])
        self.repository.promote.assert_not_called()

    def test_remote_member_moved_after_authorization_prevents_effect(self):
        self.ready()
        original = self.runtime.authorize
        def moved(plan):
            receipt = original(plan)
            self.repository.check_members.side_effect = train.TrainError("member moved beyond freeze")
            return receipt
        with mock.patch.object(self.runtime, "authorize", side_effect=moved):
            outcome = self.engine.step(3602)
        self.assertEqual(outcome["result"], "refused", outcome)
        self.repository.promote.assert_not_called()

    def test_new_open_prerequisite_after_authorization_prevents_effect(self):
        self.ready()
        original = self.runtime.authorize
        def dependency(plan):
            receipt = original(plan)
            self.memory.edges = [dict(work_item_id=1, blocker_id=2, blocking=True)]
            return receipt
        with mock.patch.object(self.runtime, "authorize", side_effect=dependency):
            outcome = self.engine.step(3602)
        self.assertEqual(outcome["result"], "refused", outcome)
        self.assertIn("prerequisites changed", outcome["error"])
        self.repository.promote.assert_not_called()

    def test_lost_push_response_reconciles_real_receipt_under_new_epoch_without_replay(self):
        plan, fire = self.ready()
        def lost(plan, **kw):
            self.memory.base = plan["head"]
            raise train.TrainError("successful push response lost")
        self.repository.promote.side_effect = lost
        self.assertEqual(self.engine.step(3602)["result"], "refused")
        prior_epoch = self.engine.epoch
        restarted = self.engine_for(self.runtime_for())
        outcome = restarted.step(3603)
        self.assertEqual(outcome["result"], "landed", outcome)
        self.repository.promote.assert_called_once()
        completed = self.ledger.ops.train_fire_read(self.ledger.key, plan["id"])
        self.assertEqual(completed["state"], "landed")
        self.assertGreater(completed["epoch"], prior_epoch)

    def test_wrong_ci_source_or_target_or_missing_provider_cannot_construct_runtime(self):
        for kw in (dict(ci=mock.Mock(slug="other/repo")), dict(target="develop"),
                   dict(admissions=None)):
            with self.subTest(kw=kw), self.assertRaises(train.TrainError):
                self.runtime_for(**kw)

    def test_unknown_historical_proof_refuses_before_publication(self):
        self.memory.admitted = False
        outcome = self.engine.step(3600)
        self.assertEqual(outcome["result"], "refused", outcome)
        self.repository.publish.assert_not_called()
        self.ci.start.assert_not_called()

    def test_reconciliation_for_another_source_cannot_use_this_targets_observation(self):
        plan, fire = self.ready()
        self.repository.target.reset_mock()
        with self.assertRaisesRegex(train.TrainError, "another runtime source"):
            self.runtime.reconcile(dict(plan, source="github.com/other/repo"), fire, self.engine.epoch)
        self.repository.target.assert_not_called()

    def test_missing_epoch_never_allows_quarantine_when_target_is_busy(self):
        with mock.patch.object(self.ledger, "acquire", return_value=None):
            with self.assertRaisesRegex(train.TrainError, "reservation lost"):
                self.runtime.quarantine([row(1)], "isolated failure", None)
        self.admissions.quarantine.assert_not_called()
