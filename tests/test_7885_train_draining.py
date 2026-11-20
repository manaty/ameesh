# SPDX-License-Identifier: AGPL-3.0-only
"""#7885 : un train en draining n'attend pas une preuve qui n'existera jamais.

Le rapport : un plan jamais publié ni tiré (fire null, référence distante
absente, aucune CI) restait en `draining` pour toujours et le train ne
composait plus rien. Un plan dont tous les membres sont déjà livrés par une
autre voie est du même genre : sa CI, si elle existe, ne peut plus être
promue.
"""
import unittest
from unittest import mock

from ameesh import storage, train, train_runtime, work
from .support import PgTestCase
from .test_1467_train import MemoryPorts, row


class DrainingWithoutOutcomeTest(unittest.TestCase):
    """Le banc d'effets du train, sans SQL : états et effets exacts."""

    def make(self):
        self.ports = MemoryPorts([row(1)])
        # Aucune référence distante : le plan n'a jamais été publié.
        self.ports.published = lambda plan: ""
        self.engine = train.Engine(self.ports, source="github.com/o/r",
                                   target="main",
                                   policy=train.Policy(enabled=True, build_beta=False))
        return self.engine

    def draining(self, status="absent"):
        """La reproduction du rapport, par le vrai chemin d'erreur.

        La composition réussit, puis la preuve d'admission change avant la
        publication : `publishing` lève, le pas persiste `draining` sans feu
        et sans avoir rien publié ni lancé. `status` est ce que la lecture CI
        rend ensuite pour cette tête."""
        original = self.ports.qualify
        calls = {"n": 0}

        def changed(rows, base, mode):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise train.TrainError("admission evidence changed before train effect")
            return original(rows, base, mode)

        self.ports.qualify = changed
        outcome = self.engine.step(3600)
        self.assertEqual(outcome["result"], "refused", outcome)
        self.ports.qualify = original
        state = self.ports.document
        self.assertEqual(state["phase"], "draining")
        self.assertIsNone(state.get("fire"))
        self.assertEqual([effect[0] for effect in self.ports.effects], [])
        self.ports.ci_by_head[state["plan"]["head"]] = status
        return state

    def test_absent_ci_without_fire_or_publication_is_terminal_at_the_next_pass(self):
        self.make()
        state = self.draining()
        head = state["plan"]["head"]
        result = self.engine.step(3601)
        self.assertEqual(result["result"], "invalidated", result)
        self.assertEqual(result["plan"]["head"], head)
        stored = self.ports.document
        self.assertEqual(stored["phase"], "invalidated")
        self.assertFalse(self.ports.held)
        self.assertIn("no CI", stored["error"])
        self.assertEqual(stored["diagnosis"],
                         {"stage": "draining", "why": stored["error"]})
        self.assertEqual(self.ports.notices[-1], stored["error"])
        self.assertNotIn("promote", [effect[0] for effect in self.ports.effects])
        # La passe suivante recompose au créneau, pas dans la foulée.
        self.assertEqual(self.engine.step(3602)["result"], "idle")
        self.assertEqual([effect[0] for effect in self.ports.effects], [])

    def test_real_pending_ci_is_still_awaited(self):
        self.make()
        self.draining("pending")
        result = self.engine.step(3601)
        self.assertEqual(result["result"], "draining", result)
        self.assertEqual(self.ports.document["phase"], "draining")
        self.assertTrue(self.ports.held)

    def test_absent_ci_of_a_published_plan_is_not_terminal(self):
        self.make()
        self.draining()
        # La référence distante existe : la CI a pu partir ou peut arriver.
        self.ports.published = lambda plan: plan["head"]
        result = self.engine.step(3601)
        self.assertEqual(result["result"], "draining", result)
        self.assertEqual(self.ports.document["phase"], "draining")
        self.assertTrue(self.ports.held)

    def test_all_members_delivered_elsewhere_ends_even_a_pending_wait(self):
        self.make()
        self.draining("pending")
        self.ports.delivered = lambda plan: True
        result = self.engine.step(3601)
        self.assertEqual(result["result"], "invalidated", result)
        self.assertEqual(self.ports.document["phase"], "invalidated")
        self.assertFalse(self.ports.held)
        self.assertIn("delivered", self.ports.document["error"])
        self.assertNotIn("promote", [effect[0] for effect in self.ports.effects])

    def test_a_partially_delivered_plan_keeps_waiting(self):
        self.make()
        self.draining("pending")
        self.ports.delivered = lambda plan: False
        self.assertEqual(self.engine.step(3601)["result"], "draining")
        self.assertTrue(self.ports.held)

    def test_delivered_members_end_a_plan_while_its_ci_runs(self):
        # #7885 (2) : la livraison hors train termine le plan sans attendre
        # que `_current` refuse le membre sorti de la file ni la fin de la CI.
        self.make()
        outcome = self.engine.step(3600)
        self.assertEqual(outcome["result"], "ci", outcome)
        self.ports.rows = []          # les membres sont sortis de la file
        self.ports.delivered = lambda plan: True
        result = self.engine.step(3601)
        self.assertEqual(result["result"], "invalidated", result)
        self.assertEqual(self.ports.document["phase"], "invalidated")
        self.assertFalse(self.ports.held)


class DeliveredPortTest(unittest.TestCase):
    """Le port du runtime lit le cycle de vie des membres, sans file."""

    def runtime(self, states):
        runtime = object.__new__(train_runtime.Runtime)
        runtime.repository = type("Repo", (), {"source": "github.com/o/r"})()
        runtime.target_branch = "main"
        ops = type("Ops", (), {"get": lambda _self, lot: (
            {"id": lot, "state": states[lot]} if lot in states else None)})()
        runtime.ledger = type("Ledger", (), {"ops": ops})()
        return runtime

    def plan(self, *lots):
        return dict(source="github.com/o/r", target="main",
                    members=[row(lot) for lot in lots])

    def test_every_terminal_delivery_state_counts(self):
        for state in ("delivered", "merged", "promoted", "closed"):
            with self.subTest(state=state):
                self.assertTrue(self.runtime({1: state, 2: state}).delivered(
                    self.plan(1, 2)))

    def test_one_open_member_keeps_the_plan_alive(self):
        runtime = self.runtime({1: "delivered", 2: "build"})
        self.assertFalse(runtime.delivered(self.plan(1, 2)))

    def test_unreadable_or_empty_members_never_prove_delivery(self):
        runtime = self.runtime({1: "delivered"})
        self.assertFalse(runtime.delivered(self.plan(1, 2)))
        self.assertFalse(runtime.delivered(dict(self.plan(), members=[])))
        self.assertFalse(runtime.delivered(dict(self.plan(), members=[None])))

    def test_another_source_or_target_is_refused(self):
        runtime = self.runtime({1: "delivered"})
        with self.assertRaises(train.TrainError):
            runtime.delivered(dict(self.plan(1), target="develop"))


class RuntimeDeliveredSqlTest(PgTestCase):
    """Le port réel lit `work_items.state` dans la base du train."""

    def runtime(self):
        runtime = object.__new__(train_runtime.Runtime)
        runtime.repository = type("Repo", (), {"source": "github.com/o/r"})()
        runtime.target_branch = "main"
        runtime.ledger = type("Ledger", (), {"ops": storage.of(self.db).work})()
        return runtime

    def plan(self, *lots):
        return dict(source="github.com/o/r", target="main",
                    members=[row(lot) for lot in lots])

    def test_delivered_reads_the_real_terminal_state(self):
        first = work.add(self.db, title="delivered member", app="runtime-test",
                         source="fixture", actor="fixture")["id"]
        second = work.add(self.db, title="open member", app="runtime-test",
                          source="fixture", actor="fixture")["id"]
        self.db.execute("UPDATE work_items SET state = 'delivered',"
                        " workflow_stage = 'merged' WHERE id = %s", (first,))
        plan = self.plan(first, second)
        self.assertFalse(self.runtime().delivered(plan))
        self.db.execute("UPDATE work_items SET state = 'delivered',"
                        " workflow_stage = 'merged' WHERE id = %s", (second,))
        self.assertTrue(self.runtime().delivered(plan))


if __name__ == "__main__":
    unittest.main()
