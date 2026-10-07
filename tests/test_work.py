# SPDX-License-Identifier: AGPL-3.0-only
"""work_items : machine à états, boucle QA bornée, journal des transitions."""
from __future__ import annotations

import threading
import time
import unittest

from ameesh import registry, work

from .support import PgTestCase


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
        self.assertIn("transition refusée", str(ctx.exception))
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
        self.assertIn("épuisée", str(ctx.exception))
        # on peut encore bloquer ou attendre un humain
        self.assertEqual(work.move(self.db, item["id"], "blocked")["state"], "blocked")
        self.assertEqual(
            work.move(self.db, item["id"], "waiting_human")["state"], "waiting_human")

    def test_journal_des_transitions(self):
        item = self._add()
        work.move(self.db, item["id"], "build", note="je prends", actor="deepseek7")
        work.note(self.db, item["id"], "question au propriétaire", actor="deepseek7")
        events = work.events(self.db, item["id"])
        self.assertEqual([e["state"] for e in events], ["build", "build", "intake"])
        self.assertEqual(events[0]["note"], "question au propriétaire")
        self.assertEqual(events[1]["note"], "je prends")
        self.assertEqual(events[2]["note"], "création")

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
        item = self._add()
        db1, db2 = self.connect(), self.connect()
        barrier = threading.Barrier(2)
        resultats: list = [None, None]

        def bouger(index: int, db, state: str) -> None:
            barrier.wait(timeout=10)
            try:
                resultats[index] = work.move(db, item["id"], state, actor="t%d" % index)
            except work.WorkError as exc:
                resultats[index] = exc

        try:
            threads = [
                threading.Thread(target=bouger, args=(0, db1, "build")),
                threading.Thread(target=bouger, args=(1, db2, "blocked")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            gagnants = [r for r in resultats if isinstance(r, dict)]
            perdants = [r for r in resultats if isinstance(r, work.WorkError)]
            self.assertEqual(len(gagnants), 1, resultats)
            self.assertEqual(len(perdants), 1, resultats)
            self.assertEqual(work.get(self.db, item["id"])["state"], gagnants[0]["state"])
        finally:
            db1.close()
            db2.close()

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
        self.assertEqual(work.last_milestone(row)[0], "fusion")
        self.assertEqual([label for label, _v in work.durations(row)],
                         ["demande → gel", "gel → revue", "gel → fusion", "total"])

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
        self.assertEqual(work.last_milestone(row)[0], "demande")

    def test_jalon_courant_gel_puis_revue(self):
        item = self._add("En revue")
        work.move(self.db, item["id"], "build")
        work.milestone(self.db, item["id"], "frozen", sha="a" * 40)
        row = work.timeline(self.db, item["id"])
        self.assertEqual(work.last_milestone(row)[0], "gel")
        self.assertIsNotNone(row["frozen_ts"])
        self.assertIsNone(row["reviewed_ts"])
        work.milestone(self.db, item["id"], "verdict", verdict="blocked")
        row = work.timeline(self.db, item["id"])
        self.assertEqual(work.last_milestone(row)[0], "revue")
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
