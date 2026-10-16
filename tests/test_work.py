# SPDX-License-Identifier: AGPL-3.0-only
"""work_items : machine à états, boucle QA bornée, journal des transitions."""
from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from ameesh import registry, work

from .support import PgTestCase, hold_for_owner


class WorkTest(PgTestCase):
    def _add(self, title: str = "un lot", **kwargs) -> dict:
        return work.add(self.db, title=title, actor="orchestrateur", **kwargs)

    def test_cycle_complet(self):
        item = self._add("Banc v1")
        self.assertEqual(item["state"], "intake")
        self.assertEqual(item["type"], "evolution")
        for state in ("build", "qa", "merged", "promoted"):
            item = work.move(self.db, item["id"], state, actor="deepseek7")
        self.assertEqual(item["state"], "promoted")
        self.assertIsNotNone(item["closed_ts"])
        self.assertEqual(work.list_items(self.db, state="promoted")[0]["id"], item["id"])

    def test_transitions_refusees(self):
        item = self._add()
        with self.assertRaises(work.WorkError) as ctx:
            work.move(self.db, item["id"], "merged")  # intake → merged : sauté
        self.assertIn("transition refused", str(ctx.exception))
        work.move(self.db, item["id"], "build")
        with self.assertRaises(work.WorkError):
            work.move(self.db, item["id"], "intake")  # retour interdit
        with self.assertRaises(work.WorkError):
            work.move(self.db, item["id"], "promoted")
        with self.assertRaises(work.WorkError):
            work.move(self.db, item["id"], "build")  # déjà (état inchangé)
        promoted = work.move(self.db, work.move(
            self.db, work.move(self.db, item["id"], "qa")["id"], "merged")["id"], "promoted")
        self.assertEqual(promoted["state"], "promoted")
        with self.assertRaises(work.WorkError):
            work.move(self.db, item["id"], "blocked")  # terminal

    def test_boucle_qa_bornee(self):
        item = self._add()
        work.move(self.db, item["id"], "build")
        work.move(self.db, item["id"], "qa")
        work.move(self.db, item["id"], "build")  # 1er retour
        work.move(self.db, item["id"], "qa")
        item = work.move(self.db, item["id"], "build")  # 2e retour
        self.assertEqual(int(item["loops"]), work.MAX_QA_LOOPS)
        work.move(self.db, item["id"], "qa")
        with self.assertRaises(work.WorkError) as ctx:
            work.move(self.db, item["id"], "build")  # 3e retour refusé
        self.assertIn("exhausted", str(ctx.exception))
        # on peut encore bloquer ou attendre un humain
        self.assertEqual(work.move(self.db, item["id"], "blocked")["state"], "blocked")
        hold_for_owner(self.db, item["id"])
        self.assertEqual(work.get(self.db, item["id"])["state"], "waiting_human")

    def test_journal_des_transitions(self):
        item = self._add()
        work.move(self.db, item["id"], "build", note="je prends", actor="deepseek7")
        work.note(self.db, item["id"], "question au propriétaire", actor="deepseek7")
        events = work.events(self.db, item["id"])
        self.assertEqual([e["state"] for e in events], ["build", "build", "intake"])
        self.assertEqual(events[0]["note"], "question au propriétaire")
        self.assertEqual(events[1]["note"], "je prends")
        self.assertEqual(events[2]["note"], "created")

    def test_validations(self):
        with self.assertRaises(work.WorkError):
            self._add(title="   ")
        with self.assertRaises(work.WorkError):
            self._add(type="truc")
        with self.assertRaises(work.WorkError):
            work.list_items(self.db, state="inconnu")
        with self.assertRaises(work.WorkError):
            work.move(self.db, 999, "build")
        with self.assertRaises(work.WorkError):
            work.note(self.db, 999, "coucou")
        with self.assertRaises(work.WorkError):
            work.note(self.db, self._add()["id"], "  ")

    def test_assignation_et_budget(self):
        # L37 (0030) : l'assigné d'un lot doit exister (attribution gardée)
        registry.upsert(self.db, "deepseek7", harness="deepseek")
        item = self._add("avec budget", type="bug", app="nexlink", assignee="deepseek7",
                         budget_usd=3.5, source="rollbar", issue_ref="#42",
                         workstream="v1")
        self.assertEqual(item["type"], "bug")
        self.assertEqual(item["app"], "nexlink")
        self.assertEqual(item["assignee"], "deepseek7")
        self.assertEqual(float(item["budget_usd"]), 3.5)
        self.assertEqual(item["source"], "rollbar")
        self.assertEqual(work.list_items(self.db, assignee="deepseek7")[0]["id"], item["id"])
        self.assertEqual(work.list_items(self.db, assignee="personne"), [])

    def test_deplacement_concurrent_un_seul_gagnant(self):
        """Deux `move` partis du MÊME état : le CAS n'en garde qu'un.

        La barrière est posée APRÈS la lecture de l'état (dans `work.get`),
        pas au départ des fils. Sans elle, le second appel peut lire l'état
        déjà déplacé par le premier et appliquer une transition séquentielle
        parfaitement légale — deux gagnants, course d'ordonnancement (CI du
        13/10, run 38357726701 : bloqué puis build, `updated_ts` du premier
        antérieur). Le test mesurerait alors l'ordonnanceur, pas le CAS (§3,
        défaut dépendant de l'environnement : corrigé à la source)."""
        item = self._add()
        db1, db2 = self.connect(), self.connect()
        lus = threading.Barrier(2, timeout=10)
        vrai_get = work.get
        lectures: list = []
        resultats: list = [None, None]

        def get_puis_attendre(db, item_id):
            row = vrai_get(db, item_id)
            lectures.append(item_id)
            if len(lectures) <= 2:
                lus.wait(timeout=10)  # les deux lectures d'entrée tiennent le même état
            return row

        def bouger(index: int, db, state: str) -> None:
            try:
                resultats[index] = work.move(db, item["id"], state, actor="t%d" % index)
            except work.WorkError as exc:
                resultats[index] = exc

        try:
            with mock.patch.object(work, "get", side_effect=get_puis_attendre):
                threads = [
                    threading.Thread(target=bouger, args=(0, db1, "build")),
                    threading.Thread(target=bouger, args=(1, db2, "blocked")),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
                self.assertFalse(any(thread.is_alive() for thread in threads))
                # le témoin garde son sens : si `move` cesse de lire par
                # `work.get`, la barrière ne sert plus et le test redevient
                # une course d'ordonnancement — on le dit ici, pas en flaky.
                # 3 lectures : une par move, plus la relecture du perdant
                # (#1922/N14), qui distingue l'adoption concurrente du lot
                # (workflow_ref) du simple changement d'état avant de rendre
                # son refus.
                self.assertEqual(len(lectures), 3, "les deux move doivent lire par work.get")
        finally:
            db1.close()
            db2.close()
        gagnants = [r for r in resultats if isinstance(r, dict)]
        perdants = [r for r in resultats if isinstance(r, work.WorkError)]
        self.assertEqual(len(gagnants), 1, resultats)
        self.assertEqual(len(perdants), 1, resultats)
        self.assertIn("moved in the meantime", str(perdants[0]), resultats)
        self.assertEqual(work.get(self.db, item["id"])["state"], gagnants[0]["state"])

    def test_deux_moves_sequentiels_reussissent(self):
        """Le CAS garde la transition lue, il n'interdit pas l'enchaînement.

        Si le second `move` lit l'état DÉJÀ déplacé, sa transition est légale
        et réussit : c'est l'autre visage de la course CI du 13/10 (bloqué
        puis build, deux gagnants) — d'où la barrière posée après la lecture
        dans le test de course, qui seule mesure le CAS."""
        item = self._add()
        premier = work.move(self.db, item["id"], "blocked", actor="t1")
        second = work.move(self.db, item["id"], "build", actor="t2")
        self.assertEqual((premier["state"], second["state"]), ("blocked", "build"))
        self.assertEqual(work.get(self.db, item["id"])["state"], "build")

    def test_notify_sur_changement_d_etat(self):
        listener = self.db.listen(["work_item"])
        try:
            time.sleep(0.6)
            item = self._add("notifie-moi")
            recu = None
            deadline = time.monotonic() + 5.0
            while recu is None and time.monotonic() < deadline:
                recu = listener.wait(timeout=1.0)
            self.assertIsNotNone(recu)
            self.assertEqual(recu["channel"], "work_item")
            self.assertIn('"state"', recu["payload"])
            self.assertIn(str(item["id"]), recu["payload"])
        finally:
            listener.close()

    def test_close_delivered_hors_cible(self):
        """#988 : livré hors cible — la tête relue est exigée et le jalon
        `delivered` la porte ; le lot reste fermé, jamais rouvert."""
        item = self._add("rapport hors cible")
        work.move(self.db, item["id"], "build", actor="dev1")
        with self.assertRaises(work.WorkError) as ctx:
            work.close(self.db, item["id"], delivered=True)
        self.assertIn("--sha", str(ctx.exception))
        with self.assertRaises(work.WorkError):
            work.close(self.db, item["id"], delivered=True, sha="pas-un-sha")
        with self.assertRaises(work.WorkError) as ctx:
            work.close(self.db, item["id"], sha="abcdef1")       # sans --delivered
        self.assertIn("--delivered", str(ctx.exception))
        with self.assertRaises(work.WorkError):
            work.close(self.db, item["id"])                       # aucune porte
        with self.assertRaises(work.WorkError):
            work.close(self.db, item["id"], abandoned=True, delivered=True,
                       sha="abcdef1")                             # deux portes
        row = work.close(self.db, item["id"], delivered=True, sha="ABCdef1234567",
                         note="rapport remis", actor="dev1")
        self.assertEqual((row["state"], row["close_reason"]), ("closed", "delivered"))
        self.assertIsNone(row["superseded_by"])
        marks = {m["kind"]: m for m in work.milestones(self.db, item["id"])}
        self.assertEqual(marks["delivered"]["sha"], "abcdef1234567")
        self.assertEqual(marks["delivered"]["actor"], "dev1")
        self.assertIn("rapport remis", marks["delivered"]["note"])
        self.assertIn("closed", marks)
        self.assertEqual(work.close_label(row), "delivered outside the target")
        with self.assertRaises(work.WorkError) as ctx:
            work.close(self.db, item["id"], abandoned=True)
        self.assertIn("already closed", str(ctx.exception))
        with self.assertRaises(work.WorkError) as ctx:
            work.merged(self.db, item["id"], sha="abcdef1")       # jamais rouvert
        self.assertIn("delivered outside the target", str(ctx.exception))

    def test_close_delivered_par_la_cli(self):
        """#988 : la porte `--delivered --sha` est exposée par la CLI."""
        item = self._add("rapport cli")
        proc = self.mesh("work", "close", str(item["id"]), "--delivered",
                         "--sha", "abcdef1234567")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("closed: delivered outside the target", proc.stdout)
        closed = work.get(self.db, item["id"])
        self.assertEqual(closed["close_reason"], "delivered")
        proc = self.mesh("work", "close", str(item["id"]), "--delivered",
                         "--sha", "abcdef1")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("already closed", proc.stderr)
        other = self._add("sans tête")
        proc = self.mesh("work", "close", str(other["id"]), "--delivered")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--sha", proc.stderr)
        self.assertEqual(work.get(self.db, other["id"])["state"], "intake")


if __name__ == "__main__":
    unittest.main()


class DelaysTest(PgTestCase):
    """Les jalons et délais par lot (0013, R19) : écrits, jamais devinés."""

    def _add(self, title: str = "un lot") -> dict:
        return work.add(self.db, title=title, actor="orchestrateur")

    def test_jalons_automatiques_et_declares(self):
        item = self._add("Frise")
        rows = work.milestones(self.db, item["id"])
        self.assertEqual([row["kind"] for row in rows], ["requested"])
        self.assertEqual(rows[0]["note"], "création")
        # gel, verdict bloqué, nouveau gel, verdict ok, puis fusion
        work.milestone(self.db, item["id"], "frozen", sha="a" * 40, actor="deepseek7")
        work.milestone(self.db, item["id"], "verdict", verdict="blocked",
                       sha="b" * 40, note="corrections")
        work.move(self.db, item["id"], "build")
        work.milestone(self.db, item["id"], "frozen", sha="c" * 40)
        work.milestone(self.db, item["id"], "verdict", verdict="ok", sha="d" * 40)
        for state in ("qa", "merged"):
            work.move(self.db, item["id"], state)
        kinds = [row["kind"] for row in work.milestones(self.db, item["id"])]
        self.assertEqual(kinds.count("frozen"), 2)
        self.assertEqual(kinds.count("verdict"), 2)
        self.assertEqual(kinds.count("merged"), 1)   # automatique, une seule fois

        row = work.timeline(self.db, item["id"])
        for key in ("requested_ts", "frozen_ts", "reviewed_ts", "merged_ts"):
            self.assertIsNotNone(row[key], key)
        self.assertEqual(row["reviewed_verdict"], "blocked")   # le premier verdict
        self.assertEqual(row["verdicts"], 2)
        self.assertEqual(row["blocked_verdicts"], 1)
        self.assertGreaterEqual(row["request_to_freeze_s"], 0)
        self.assertGreaterEqual(row["freeze_to_review_s"], 0)
        self.assertGreaterEqual(row["freeze_to_merge_s"], row["freeze_to_review_s"])
        self.assertGreaterEqual(row["total_s"], row["request_to_freeze_s"])
        self.assertEqual(work.last_milestone(row)[0], "merge")
        self.assertEqual([label for label, _v in work.durations(row)],
                         ["request → freeze", "freeze → review", "freeze → merge", "total"])

    def test_jalons_manuels_refuses(self):
        item = self._add("Refus")
        for kind in ("requested", "merged"):
            with self.assertRaises(work.WorkError):
                work.milestone(self.db, item["id"], kind)
        with self.assertRaises(work.WorkError):   # verdict sans verdict
            work.milestone(self.db, item["id"], "verdict")
        with self.assertRaises(work.WorkError):   # verdict sur un gel
            work.milestone(self.db, item["id"], "frozen", verdict="ok")
        with self.assertRaises(work.WorkError):   # verdict inconnu
            work.milestone(self.db, item["id"], "verdict", verdict="peut-être")
        with self.assertRaises(work.WorkError):   # kind inconnu
            work.milestone(self.db, item["id"], "publié")
        with self.assertRaises(work.WorkError):   # lot inconnu
            work.milestone(self.db, 999_999, "frozen")

    def test_etapes_absentes_valent_null(self):
        item = self._add("Pas gelé")
        row = work.timeline(self.db, item["id"])
        self.assertIsNotNone(row["requested_ts"])
        self.assertIsNone(row["frozen_ts"])
        self.assertIsNone(row["reviewed_ts"])
        self.assertIsNone(row["merged_ts"])
        self.assertIsNone(row["total_s"])
        self.assertEqual(row["verdicts"], 0)
        self.assertEqual(work.last_milestone(row)[0], "request")

    def test_jalon_courant_gel_puis_revue(self):
        item = self._add("En revue")
        work.move(self.db, item["id"], "build")
        work.milestone(self.db, item["id"], "frozen", sha="a" * 40)
        row = work.timeline(self.db, item["id"])
        self.assertEqual(work.last_milestone(row)[0], "freeze")
        self.assertIsNotNone(row["frozen_ts"])
        self.assertIsNone(row["reviewed_ts"])
        work.milestone(self.db, item["id"], "verdict", verdict="blocked")
        row = work.timeline(self.db, item["id"])
        self.assertEqual(work.last_milestone(row)[0], "review")
        self.assertIsNotNone(row["reviewed_ts"])
        self.assertIsNone(row["merged_ts"])

    def test_liste_des_delais_et_ordre(self):
        first = self._add("Premier")
        second = self._add("Second")
        work.move(self.db, second["id"], "build")
        rows = work.delays(self.db)
        self.assertEqual({row["work_item_id"] for row in rows}, {first["id"], second["id"]})
        self.assertEqual(rows[0]["work_item_id"], second["id"])   # le plus récemment modifié
        self.assertIsNone(work.timeline(self.db, 999_999))

    def test_delais_restreints_a_des_lots(self):
        """`ids` borne le balayage aux lots demandés (B3, revue codex2)."""
        first = self._add("Premier")
        second = self._add("Second")
        rows = work.delays(self.db, ids=[first["id"]])
        self.assertEqual([row["work_item_id"] for row in rows], [first["id"]])
        self.assertEqual(work.delays(self.db, ids=[]), [])
        deux = {row["work_item_id"]
                for row in work.delays(self.db, ids=[second["id"], first["id"]])}
        self.assertEqual(deux, {first["id"], second["id"]})

    def test_jalon_delivered_et_delais(self):
        """#988 : `delivered_ts` date la livraison hors cible, `merged_ts` reste
        nul et `total` court jusqu'à elle."""
        item = self._add("Rapport livré")
        work.move(self.db, item["id"], "build")
        work.close(self.db, item["id"], delivered=True, sha="a" * 40, actor="dev1")
        row = work.timeline(self.db, item["id"])
        self.assertIsNotNone(row["delivered_ts"])
        self.assertIsNone(row["merged_ts"])
        self.assertIsNotNone(row["total_s"])
        self.assertGreaterEqual(row["total_s"], 0.0)
        self.assertEqual(work.last_milestone(row)[0], "delivered")
