# SPDX-License-Identifier: AGPL-3.0-only
"""#705 : désigner le relecteur quand un lot entre en qa.

Avant tout verdict, ameesh ne connaissait le relecteur qu'après coup (l'auteur
du dernier verdict) : un lot en qa attendait « verdict d'un relecteur (non
désigné) », donc personne. `work move <id> qa --reviewer <agent>` le désigne ;
l'attente, la vue de progrès et `work show` le nomment ; un verdict courant
garde son auteur réel, et la revue suivante peut redésigner quelqu'un.
"""
from __future__ import annotations

import unittest

from ameesh import plan, progress, work

from .support import PgTestCase


class ReviewerMilestonesTest(unittest.TestCase):
    """Le relecteur lu par `progress` : désigné, ou l'auteur du verdict courant."""

    def _item(self, **extra):
        item = {"state": "qa", "reviewer": "", "created_ts": 0.0}
        item.update(extra)
        return item

    def test_designe_avant_tout_verdict(self):
        jalons = progress._work_item_milestones(self._item(reviewer="relecteur"), [], [])
        self.assertEqual(jalons["reviewer"], "relecteur")
        self.assertIsNone(jalons["verdict"])

    def test_verdict_courant_garde_son_auteur(self):
        events = [
            {"state": "qa", "created_ts": 1.0},
            {"state": "build", "created_ts": 2.0, "actor": "autre"},
        ]
        jalons = progress._work_item_milestones(self._item(reviewer="designe"), events, [])
        self.assertTrue(jalons["verdict_current"])
        self.assertEqual(jalons["reviewer"], "autre")

    def test_regel_rend_la_parole_au_designe(self):
        events = [
            {"state": "qa", "created_ts": 1.0},
            {"state": "build", "created_ts": 2.0, "actor": "autre"},
            {"state": "qa", "created_ts": 3.0},
        ]
        jalons = progress._work_item_milestones(self._item(reviewer="designe"), events, [])
        self.assertFalse(jalons["verdict_current"])
        self.assertEqual(jalons["reviewer"], "designe")

    def test_sans_designation_ni_verdict_personne(self):
        jalons = progress._work_item_milestones(self._item(), [], [])
        self.assertIsNone(jalons["reviewer"])


class ReviewerDesignationTest(PgTestCase):
    def _lot(self, title="un lot", assignee="auteur"):
        self.register(assignee, "claude", cwd=self.tmp)
        return work.add(self.db, title=title, assignee=assignee)

    def _qa(self, lot, *args):
        proc = self.mesh("work", "move", str(lot["id"]), "build")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.mesh("work", "move", str(lot["id"]), "qa", *args)

    def test_relecteur_designe_nomme_avant_tout_verdict(self):
        self.register("relecteur", "codex", cwd=self.tmp)
        lot = self._lot()
        proc = self._qa(lot, "--reviewer", "relecteur")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("reviewer relecteur", proc.stdout)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "relecteur")

        rows = work.list_items(self.db, limit=10)
        plan.annotate(self.db, rows)
        self.assertEqual(rows[0]["waiting_for"]["what"], "verdict")
        self.assertEqual(rows[0]["waiting_for"]["who"], "relecteur")
        self.assertEqual(rows[0]["waiting_for"]["label"], "verdict by relecteur")

        proc = self.mesh("work", "show", str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("reviewer : relecteur", proc.stdout)
        self.assertIn("waiting  : verdict by relecteur", proc.stdout)

    def test_sans_relecteur_l_attente_le_dit(self):
        lot = self._lot()
        proc = self._qa(lot)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "")
        rows = work.list_items(self.db, limit=10)
        plan.annotate(self.db, rows)
        self.assertIsNone(rows[0]["waiting_for"]["who"])
        self.assertEqual(rows[0]["waiting_for"]["label"],
                         "verdict from a reviewer (none designated)")

    def test_refus_hors_qa_et_pour_un_nom_inconnu(self):
        self.register("relecteur", "codex", cwd=self.tmp)
        lot = self._lot()
        proc = self.mesh("work", "move", str(lot["id"]), "build",
                         "--reviewer", "relecteur")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--reviewer", proc.stderr)
        proc = self.mesh("work", "move", str(lot["id"]), "build")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "move", str(lot["id"]), "qa",
                         "--reviewer", "fantome")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not in the registry", proc.stderr)
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["state"], item["reviewer"]), ("build", ""))

    def test_revue_suivante_garde_puis_redesigne(self):
        self.register("premier", "codex", cwd=self.tmp)
        self.register("second", "codex", cwd=self.tmp)
        lot = self._lot()
        proc = self._qa(lot, "--reviewer", "premier")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # retour en build (verdict bloquant) : le relecteur connu reste posé
        proc = self.mesh("work", "move", str(lot["id"]), "build")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "move", str(lot["id"]), "qa")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "premier")
        # une nouvelle revue peut redésigner quelqu'un d'autre
        proc = self.mesh("work", "move", str(lot["id"]), "build")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "move", str(lot["id"]), "qa",
                         "--reviewer", "second")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "second")
        rows = work.list_items(self.db, limit=10)
        plan.annotate(self.db, rows)
        self.assertEqual(rows[0]["waiting_for"]["label"], "verdict by second")
        # le journal garde la désignation
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any("reviewer second" in n for n in notes), notes)
