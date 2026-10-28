# SPDX-License-Identifier: AGPL-3.0-only
"""#3218 — latence de début de tour d'un agent au repos.

Un consommateur mesurait 25 s de médiane entre son message et le début du
tour sans voir où le temps passait. Désormais : un reçu d'étapes
(`turn.stages`) par tour de courrier, et un worker qui se réveille à la fin
datée d'une attente (fenêtre de regroupement, écart de début du compte) au
lieu du sondage suivant.
"""
from __future__ import annotations

import dataclasses
import json
import os
import time
import unittest

from ameesh import account_window, mail, registry, turn_stages
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase


class RecuTest(unittest.TestCase):
    """`turn_stages.receipt`, sans base."""

    def test_chaine_et_dominante(self):
        recu = turn_stages.receipt({
            "ids": [7], "mail_ts": 100.0, "seen_ts": 100.2, "due_ts": 101.0,
            "admitted_ts": 101.1, "spawned_ts": 101.4, "delivered_ts": 101.45,
            "first_output_ts": 109.4})
        self.assertEqual(recu["messages"], [7])
        self.assertEqual(recu["stages_s"]["wake"], 0.2)
        self.assertEqual(recu["stages_s"]["batch"], 0.8)
        self.assertEqual(recu["stages_s"]["admit"], 0.1)
        self.assertEqual(recu["stages_s"]["spawn"], 0.3)
        self.assertEqual(recu["stages_s"]["harness_start"], 8.0)
        self.assertEqual(recu["dominant"], "harness_start")
        self.assertEqual(recu["total_s"], 9.4)
        self.assertEqual(recu["ts"]["delivered_ts"], 101.45)
        self.assertEqual(recu["held_reasons"], [])

    def test_tour_precedent_et_garde(self):
        # message à 100, tour précédent fini à 104, garde de fenêtre courte
        # de 104 à 120 : lu à 120, dû, admis à 120.5, lancé à 121
        recu = turn_stages.receipt(
            {"mail_ts": 100.0, "seen_ts": 120.0, "due_ts": 120.0,
             "admitted_ts": 120.5, "spawned_ts": 121.0, "prev_turn_end_ts": 104.0},
            [(104.0, 120.0, "short_window"), (10.0, 20.0, "budget")])
        self.assertEqual(recu["stages_s"]["busy"], 4.0)
        self.assertEqual(recu["stages_s"]["held"], 16.0)
        self.assertEqual(recu["stages_s"]["wake"], 0.0)
        self.assertEqual(recu["dominant"], "held")
        # la garde d'avant le message n'est pas une raison de ce tour
        self.assertEqual(recu["held_reasons"], ["short_window"])
        self.assertEqual(recu["total_s"], 21.0)

    def test_decalage_d_horloge_et_jalons_absents(self):
        # base en avance sur l'hôte : l'étape négative vaut zéro
        recu = turn_stages.receipt({"mail_ts": 105.0, "seen_ts": 100.0,
                                    "due_ts": 100.0, "admitted_ts": 100.1})
        self.assertEqual(recu["stages_s"]["wake"], 0.0)
        self.assertEqual(recu["stages_s"]["admit"], 0.1)
        self.assertNotIn("spawned_ts", recu["ts"])
        self.assertEqual(recu["dominant"], "admit")
        self.assertIsNone(turn_stages.receipt({})["dominant"])

    def test_identifiants_bornes(self):
        recu = turn_stages.receipt({"ids": list(range(50))})
        self.assertEqual(len(recu["messages"]), turn_stages.MAX_IDS)


class LeveeTest(unittest.TestCase):
    def test_lift_at(self):
        self.assertEqual(account_window.lift_at({"next_start_at": 130.0}, 100.0), 130.0)
        self.assertEqual(account_window.lift_at(
            {"cooldown_until": 400.0, "next_start_at": 130.0}, 100.0), 400.0)
        self.assertIsNone(account_window.lift_at({"next_start_at": 90.0}, 100.0))
        # plafond de tours : levé par la fin d'un autre tour, non daté
        self.assertIsNone(account_window.lift_at({"active_count": 2, "max_active": 2},
                                                 100.0))
        self.assertEqual(account_window.reason_and_lift(None, "claude", "x"), ("", None))


class _Base(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM turn_resources")

    def _worker(self, name: str, *, batch: float):
        runner = Runner(dataclasses.replace(self.cfg), self.db, once=True)
        runner.mail_batch = batch
        runner.event_coalesce = 120.0
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return runner, worker


class ReveilALEcheanceTest(_Base):
    def test_fin_de_fenetre_datee(self):
        _runner, worker = self._worker("lat-batch", batch=30.0)
        mail.send(self.db, "dev1", "lat-batch", "prêt")
        self.assertIsNone(worker.pick())
        # l'échéance est la fin de la fenêtre, pas le sondage suivant
        self.assertAlmostEqual(worker._due_at - time.time(), 30.0, delta=2.0)
        self.assertEqual(worker._until_due(5.0), 5.0)  # au-delà du sondage : le sondage
        self.assertIsNone(worker._due_at)              # consommée
        worker.note_due_at(time.time() + 0.5)
        worker.note_due_at(time.time() + 3.0)          # la plus proche gagne
        self.assertLess(worker._until_due(5.0), 0.6)
        worker.note_due_at(time.time() - 10.0)         # passée : relecture aussitôt
        self.assertEqual(worker._until_due(5.0), 0.05)

    def test_reveil_dans_la_fenetre(self):
        _runner, worker = self._worker("lat-short", batch=3.0)
        mail.send(self.db, "dev1", "lat-short", "prêt")
        self.assertIsNone(worker.pick())
        attente = worker._until_due(5.0)
        self.assertLess(attente, 3.1)  # la fin de la fenêtre, avant le sondage
        time.sleep(attente)
        spec = worker.pick()
        self.assertEqual(spec["kind"], "mail")
        etapes = spec["_etapes"]
        self.assertEqual(etapes["ids"], spec["ids"])
        self.assertLessEqual(etapes["seen_ts"], etapes["due_ts"])
        self.assertGreater(etapes["due_ts"], etapes["seen_ts"])
        self.assertIsNotNone(etapes["mail_ts"])

    def test_humain_date_aussi(self):
        _runner, worker = self._worker("lat-h", batch=60.0)
        mail.send(self.db, "alice", "lat-h", "fais le point", payload={"human": True})
        spec = worker.pick()
        self.assertTrue(spec.get("human"))
        self.assertEqual(spec["_etapes"]["ids"], spec["ids"])


class RecuDeBoutEnBoutTest(_Base):
    def test_un_tour_publie_ses_etapes(self):
        name = "lat-run"
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        ident = mail.send(self.db, "dev1", name, "un message")
        proc = self.runner("--once", "--wait", "8", "--agents", name,
                           env=self.env(AMEESH_MAIL_BATCH="0"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rows = self.db.query(
            "SELECT object, data FROM ameesh_events WHERE type = 'turn.stages'")
        self.assertEqual(len(rows), 1, rows)
        data = rows[0]["data"]
        data = json.loads(data) if isinstance(data, str) else data
        self.assertEqual(rows[0]["object"], "agent:" + name)
        self.assertEqual(data["agent"], name)
        self.assertEqual(data["messages"], [ident])
        for jalon in ("mail_ts", "seen_ts", "due_ts", "admitted_ts", "spawned_ts",
                      "delivered_ts"):
            self.assertIn(jalon, data["ts"])
        self.assertEqual(set(data["stages_s"]), set(turn_stages.STAGES))
        self.assertIn(data["dominant"], turn_stages.STAGES + (None,))
        self.assertLess(data["total_s"], 8.0)


if __name__ == "__main__":
    unittest.main()
