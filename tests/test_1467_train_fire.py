# SPDX-License-Identifier: AGPL-3.0-only
"""Persisted exact fire, withdrawal/claim races and uncertain promotion."""
import concurrent.futures
import threading
import types
from unittest import mock

from ameesh import storage, train, train_fire, train_store, work
from .support import PgTestCase
from .test_1467_train import row


def plan():
    return dict(schema=train.SCHEMA, source="github.com/o/r", target="main",
                base="a" * 40, head="b" * 40, tree="c" * 40,
                members=[row(1)], proof={"historical": "exact-record"},
                id="d" * 64, nonce="e" * 64, branch="integration/train-test",
                diagnostic=False, created_ts=1)


class TrainFireTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE merge_locks RESTART IDENTITY CASCADE")

    def prepare(self):
        ledger = train_store.Ledger(self.db, types.SimpleNamespace(name="fire-test"),
                                    "main", "runner")
        epoch = ledger.acquire(None)
        item = plan()
        document = ledger.save(dict(phase="ci", plan=item), 0, epoch)
        fires = train_fire.Fires(None, ledger)
        with mock.patch.object(work, "corrector", return_value="pilot") as guard:
            fire = fires.give(item, actor="pilot")
        guard.assert_called_once_with(None, self.db, "pilot", history=False)
        return ledger, epoch, item, document, fires, fire

    def intent(self, ledger, epoch, document):
        return ledger.save(dict(document, phase="promoting"), document["revision"], epoch)

    def test_withdrawn_receipt_cannot_promote_even_from_stale_authorization(self):
        ledger, epoch, item, document, fires, fire = self.prepare()
        with mock.patch.object(fires, "_still_pilot", return_value=True), \
                mock.patch.object(work, "corrector", return_value="pilot"):
            self.assertEqual(fires.authorize(item), fire)
            fires.withdraw(item, fire["id"], actor="pilot")
            self.intent(ledger, epoch, document)
            effect = mock.Mock()
            with self.assertRaisesRegex(train.TrainError, "withdrawn"):
                fires.promote(item, fire, epoch, validate=mock.Mock(), effect=effect)
            effect.assert_not_called()
            self.assertIsNone(fires.authorize(item))

    def test_complete_manifest_not_just_head_is_covered(self):
        ledger, epoch, item, document, fires, fire = self.prepare()
        for key, value in (("source", "github.com/elsewhere/repo"), ("target", "develop"),
                           ("base", "f" * 40), ("tree", "f" * 40),
                           ("proof", {"historical": "changed-record"}),
                           ("members", [dict(row(1), generation=99)]),
                           ("branch", "integration/train-new")):
            with self.subTest(key=key), self.assertRaisesRegex(train.TrainError, "manifest"):
                fires.authorize(dict(item, **{key: value}))
        for bad in (dict(item, diagnostic=True), dict(item, head="b" * 8),
                    dict(item, members=[row(1), row(1)]), dict(item, proof={})):
            with self.assertRaisesRegex(train.TrainError, "complete non-diagnostic"):
                train_fire.binding(bad)

    def test_withdraw_then_regrant_keeps_history_and_never_reuses_id(self):
        ledger, epoch, item, document, fires, first = self.prepare()
        with mock.patch.object(work, "corrector", return_value="pilot"):
            fires.withdraw(item, first["id"], actor="pilot")
            second = fires.give(item, actor="pilot")
            self.assertGreater(second["id"], first["id"])
            with self.assertRaisesRegex(train.TrainError, "current exact"):
                fires.withdraw(item, first["id"], actor="pilot")
        self.intent(ledger, epoch, document)
        self.assertIsNone(ledger.ops.train_fire_change(ledger.key, first["id"], "promoting",
                          actor=ledger.owner, owner=ledger.owner, epoch=epoch))
        self.assertEqual(self.db.query("SELECT state FROM integration_train_fires ORDER BY id"),
                         [{"state": "revoked"}, {"state": "active"}])

    def test_claim_is_durable_before_effect_and_lost_response_never_replays(self):
        ledger, epoch, item, document, fires, fire = self.prepare()
        self.intent(ledger, epoch, document)
        def lost():
            current = ledger.ops.train_fire_read(ledger.key, item["id"])
            self.assertEqual(current["state"], "promoting")
            raise OSError("push response lost")
        with mock.patch.object(fires, "_still_pilot", return_value=True):
            with self.assertRaises(OSError):
                fires.promote(item, fire, epoch, validate=mock.Mock(), effect=lost)
        # A new service instance sees the committed claim after a crash.
        restarted = train_fire.Fires(None, ledger)
        with mock.patch.object(work, "corrector", return_value="pilot"):
            with self.assertRaisesRegex(train.TrainError, "reconcile"):
                restarted.withdraw(item, fire["id"], actor="pilot")
            with self.assertRaisesRegex(train.TrainError, "uncertain"):
                restarted.give(item, actor="pilot")
        effect = mock.Mock()
        with self.assertRaisesRegex(train.TrainError, "withdrawn or superseded"):
            restarted.promote(item, fire, epoch, validate=mock.Mock(), effect=effect)
        effect.assert_not_called()

    def test_promotion_revalidates_then_marks_same_receipt_landed(self):
        ledger, epoch, item, document, fires, fire = self.prepare()
        self.intent(ledger, epoch, document)
        calls = []
        with mock.patch.object(fires, "_still_pilot", return_value=True):
            fires.promote(item, fire, epoch, validate=lambda: calls.append("fresh proofs"),
                          effect=lambda: calls.append("exact push"))
        self.assertEqual(calls, ["fresh proofs", "exact push"])
        current = ledger.ops.train_fire_read(ledger.key, item["id"])
        self.assertEqual((current["id"], current["state"]), (fire["id"], "landed"))

    def test_stale_lease_changed_plan_or_missing_intent_cannot_claim(self):
        ledger, epoch, item, document, fires, fire = self.prepare()
        self.assertIsNone(ledger.ops.train_fire_change(ledger.key, fire["id"], "promoting",
                          actor=ledger.owner, owner=ledger.owner, epoch=epoch))
        document = self.intent(ledger, epoch, document)
        changed = dict(item, tree="f" * 40)
        document = ledger.save(dict(document, plan=changed), document["revision"], epoch)
        self.assertIsNone(ledger.ops.train_fire_change(ledger.key, fire["id"], "promoting",
                          actor=ledger.owner, owner=ledger.owner, epoch=epoch))
        document = ledger.save(dict(document, plan=item), document["revision"], epoch)
        self.db.execute("UPDATE merge_locks SET expires_at = clock_timestamp() - interval '1 second'"
                        " WHERE target = %s", (ledger.key,))
        self.assertIsNone(ledger.ops.train_fire_change(ledger.key, fire["id"], "promoting",
                          actor=ledger.owner, owner=ledger.owner, epoch=epoch))
        epoch2 = ledger.acquire(None)
        self.assertGreater(epoch2, epoch)
        self.assertIsNone(ledger.ops.train_fire_change(ledger.key, fire["id"], "promoting",
                          actor=ledger.owner, owner=ledger.owner, epoch=epoch))

    def test_lease_lost_during_validation_prevents_effect_and_retains_claim(self):
        ledger, epoch, item, document, fires, fire = self.prepare()
        self.intent(ledger, epoch, document)
        def takeover():
            ledger.acquire(None)  # same owner restarting at another epoch
        effect = mock.Mock()
        with mock.patch.object(fires, "_still_pilot", return_value=True):
            with self.assertRaisesRegex(train.TrainError, "reservation lost"):
                fires.promote(item, fire, epoch, validate=takeover, effect=effect)
        effect.assert_not_called()
        self.assertEqual(ledger.ops.train_fire_read(ledger.key, item["id"])["state"], "promoting")

    def test_session_guard_and_declared_pilot_are_required(self):
        ledger, epoch, item, document, fires, fire = self.prepare()
        with mock.patch.object(work, "corrector", side_effect=work.WorkError("wrong session")):
            with self.assertRaisesRegex(train.TrainError, "declared role"):
                fires.withdraw(item, fire["id"], actor="pilot")
        with mock.patch.object(train_fire.assignments, "is_orchestrator", return_value=False) as role:
            with self.assertRaisesRegex(train.TrainError, "no longer"):
                fires.authorize(item)
        role.assert_called_once_with(None, self.db, "pilot", roles=work.CORRECTOR_ROLES,
                                     history=False)

    def test_concurrent_withdrawal_and_claim_have_exactly_one_winner(self):
        ledger, epoch, item, document, fires, fire = self.prepare()
        self.intent(ledger, epoch, document)
        barrier = threading.Barrier(2, timeout=10)
        def change(state):
            db = self.connect()
            try:
                barrier.wait()
                return storage.of(db).work.train_fire_change(ledger.key, fire["id"], state,
                           actor="pilot", owner=ledger.owner if state == "promoting" else None,
                           epoch=epoch if state == "promoting" else None)
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(change, state) for state in ("promoting", "revoked")]
            results = [future.result(timeout=20) for future in futures]
        winners = [r for r in results if r is not None]
        self.assertEqual(len(winners), 1)
        self.assertIn(winners[0]["state"], ("promoting", "revoked"))

    def test_withdrawal_between_authorize_and_claim_prevents_effect(self):
        ledger, epoch, item, document, fires, fire = self.prepare()
        self.intent(ledger, epoch, document)
        original = fires.ops.train_fire_change
        def withdraw_first(key, fire_id, state, **kwargs):
            self.assertIsNotNone(original(key, fire_id, "revoked", actor="pilot"))
            return original(key, fire_id, state, **kwargs)
        effect = mock.Mock()
        with mock.patch.object(fires, "_still_pilot", return_value=True), \
                mock.patch.object(fires.ops, "train_fire_change", side_effect=withdraw_first):
            with self.assertRaisesRegex(train.TrainError, "withdrawal.*prevented"):
                fires.promote(item, fire, epoch, validate=mock.Mock(), effect=effect)
        effect.assert_not_called()

    def test_json_roundtrip_of_admission_proofs_preserves_binding(self):
        ledger, epoch, item, document, fires, fire = self.prepare()
        with mock.patch.object(work, "corrector", return_value="pilot"):
            fires.withdraw(item, fire["id"], actor="pilot")
            # Engine qualification may use tuples, storage uses JSON arrays.
            item["proof"] = dict(members=[train.identity(row(1))])
            ledger.save(dict(document, plan=item), document["revision"], epoch)
            fire = fires.give(item, actor="pilot")
        with mock.patch.object(fires, "_still_pilot", return_value=True):
            self.assertEqual(fires.authorize(item), fire)

    def test_crash_after_claim_before_push_keeps_uncertainty_on_restart(self):
        ledger, epoch, item, document, fires, fire = self.prepare()
        self.intent(ledger, epoch, document)
        effect = mock.Mock()
        with mock.patch.object(fires, "_still_pilot", return_value=True):
            with self.assertRaises(SystemExit):
                fires.promote(item, fire, epoch, validate=mock.Mock(side_effect=SystemExit),
                              effect=effect)
        effect.assert_not_called()
        epoch2 = ledger.acquire(None)
        restarted = train_fire.Fires(None, ledger)
        self.assertIsNone(restarted.reconcile(item, fire["id"], epoch2, target=lambda: item["base"]))
        with self.assertRaisesRegex(train.TrainError, "diverged"):
            restarted.reconcile(item, fire["id"], epoch2, target=lambda: "f" * 40)
        with mock.patch.object(work, "corrector", return_value="pilot"):
            with self.assertRaisesRegex(train.TrainError, "reconcile"):
                restarted.withdraw(item, fire["id"], actor="pilot")
        self.assertEqual(ledger.ops.train_fire_read(ledger.key, item["id"])["state"], "promoting")

    def test_successful_push_lost_response_reconciles_at_new_epoch_without_replay(self):
        ledger, epoch, item, document, fires, fire = self.prepare()
        self.intent(ledger, epoch, document)
        remote = {"target": item["base"], "pushes": 0}
        def pushed_but_lost():
            remote["target"] = item["head"]
            remote["pushes"] += 1
            raise OSError("push succeeded, response lost")
        with mock.patch.object(fires, "_still_pilot", return_value=True):
            with self.assertRaises(OSError):
                fires.promote(item, fire, epoch, validate=mock.Mock(), effect=pushed_but_lost)
        epoch2 = ledger.acquire(None)
        restarted = train_fire.Fires(None, ledger)
        with self.assertRaisesRegex(train.TrainError, "lease/intent changed"):
            restarted.reconcile(item, fire["id"], epoch, target=lambda: remote["target"])
        receipt = restarted.reconcile(item, fire["id"], epoch2, target=lambda: remote["target"])
        self.assertEqual((receipt["state"], receipt["epoch"]), ("landed", epoch2))
        self.assertEqual(remote["pushes"], 1)
        self.assertEqual(restarted.reconcile(item, fire["id"], epoch2,
                         target=lambda: remote["target"]), receipt)
