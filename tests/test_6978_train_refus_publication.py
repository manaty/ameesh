# SPDX-License-Identifier: AGPL-3.0-only
"""#6978 : un refus déterministe en publishing termine le plan ; un pilote fait
abandonner par le service vivant un plan non donné dont il tient le bail."""
import types
import unittest
from unittest import mock

from ameesh import train, train_cli, work
from . import test_1467_train_runtime


class Base(test_1467_train_runtime.TrainRuntimeTest):
    """Le banc réel du runtime (SQL), sans rejouer ses propres tests."""
    def setUp(self):
        super().setUp()
        self.project = types.SimpleNamespace(name="runtime-test", github="o/r",
                                             target="main")


class PublicationRefusedTest(Base):
    def owner(self):
        return self.db.query("SELECT owner FROM merge_locks WHERE target = %s",
                             (self.ledger.key,))[0]["owner"]

    def test_deterministic_refusal_invalidates_and_releases_the_reservation(self):
        self.repository.publish.side_effect = train.PublicationRefused(
            "full-suite source differs from audited CI contract: .github/workflows/lot-plan.yml")
        outcome = self.engine.step(3600)
        self.assertEqual(outcome["result"], "invalidated", outcome)
        state = self.ledger.load()
        self.assertEqual(state["phase"], "invalidated")
        self.assertEqual(state["diagnosis"]["stage"], "publication")
        self.assertIn("lot-plan.yml", state["diagnosis"]["why"])
        self.assertIsNone(self.owner())
        self.ci.start.assert_not_called()
        # Aucune boucle : le pas suivant ne republie pas le même plan.
        self.engine.step(3601)
        self.repository.publish.assert_called_once()

    def test_a_true_uncertainty_keeps_publishing_and_the_reservation(self):
        self.repository.publish.side_effect = train.TrainError("atomic train publication refused")
        self.assertEqual(self.engine.step(3600)["result"], "publication-uncertain")
        state = self.ledger.load()
        self.assertEqual((state["phase"], state["publication_uncertain"]), ("publishing", True))
        self.assertEqual(self.owner(), self.ledger.owner)
        # Une passe antérieure a pu pousser : un refus ensuite ne l'invalide pas.
        self.repository.publish.side_effect = train.PublicationRefused("source differs")
        self.assertEqual(self.engine.step(3601)["result"], "publication-uncertain")
        self.assertEqual(self.ledger.load()["phase"], "publishing")
        self.assertEqual(self.owner(), self.ledger.owner)


class RequestedAbandonTest(Base):
    def held(self):
        """Un plan en publishing, bail tenu par le service (train:runner)."""
        self.repository.publish.side_effect = train.TrainError("atomic train publication refused")
        self.assertEqual(self.engine.step(3600)["result"], "publication-uncertain")
        return self.ledger.load()

    def abandon(self, plan):
        with mock.patch.object(work, "corrector", return_value="pilot"):
            return train_cli.execute(None, self.db, self.project, "abandon",
                                     manifest=plan, reason="plan tenu")

    def test_pilot_request_is_applied_by_the_holder_at_its_next_step(self):
        state = self.held()
        requested = self.abandon(state["plan"])
        self.assertEqual(requested["phase"], "publishing")
        self.assertEqual(requested["revision"], state["revision"])
        self.assertEqual((requested["abandon_request"]["actor"],
                          requested["abandon_request"]["plan"]), ("pilot", state["plan"]["id"]))
        outcome = self.engine.step(3700)
        self.assertEqual(outcome["result"], "abandoned", outcome)
        saved = self.ledger.load()
        self.assertEqual(saved["phase"], "invalidated")
        self.assertIsNone(saved["abandon_request"])
        self.assertEqual((saved["abandoned"]["actor"], saved["abandoned"]["reason"],
                          saved["abandoned"]["previous_phase"]),
                         ("pilot", "plan tenu", "publishing"))
        self.assertIsNone(self.db.query("SELECT owner FROM merge_locks WHERE target = %s",
                                        (self.ledger.key,))[0]["owner"])
        self.repository.publish.assert_called_once()

    def test_request_survives_a_holder_write_loaded_before_it(self):
        state = self.held()
        self.abandon(state["plan"])
        stale = dict(state, note="écrit par le détenteur")
        stale.pop("revision")
        self.ledger.save(stale, state["revision"], self.engine.epoch)
        self.assertEqual(self.ledger.load()["abandon_request"]["actor"], "pilot")
        # Une nouvelle composition (autre plan) ne la porte pas.
        fresh = dict(stale, plan=None, phase="composing", abandon_request=None)
        self.ledger.save(fresh, state["revision"] + 1, self.engine.epoch)
        self.assertIsNone(self.ledger.load()["abandon_request"])

    def test_request_needs_the_exact_plan_and_no_fire(self):
        state = self.held()
        with self.assertRaisesRegex(train.TrainError, "exact persisted plan"):
            self.abandon(dict(state["plan"], nonce="0" * 64))
        with mock.patch.object(work, "corrector", return_value="pilot"):
            self.fires.give(state["plan"], actor="pilot")
        with self.assertRaisesRegex(train.TrainError, "fire receipt"):
            self.abandon(state["plan"])
        self.assertIsNone(self.ledger.ops.train_abandon_request(
            self.ledger.key, state["plan"]["id"], dict(plan=state["plan"]["id"])))

    def test_fire_given_after_the_request_keeps_the_plan(self):
        state = self.held()
        self.abandon(state["plan"])
        with mock.patch.object(work, "corrector", return_value="pilot"):
            self.fires.give(state["plan"], actor="pilot")
        outcome = self.engine.step(3700)
        self.assertEqual(outcome["result"], "abandon-refused", outcome)
        saved = self.ledger.load()
        self.assertEqual(saved["phase"], "publishing")
        self.assertIn("fire changed", saved["abandon_request"]["refused"])
        # Marquée refusée : le pas suivant ne la rejoue pas.
        self.assertNotEqual(self.engine.step(3701)["result"], "abandon-refused")


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for case in (PublicationRefusedTest, RequestedAbandonTest):
        for name in sorted(vars(case)):
            if name.startswith("test_"):
                suite.addTest(case(name))
    return suite
