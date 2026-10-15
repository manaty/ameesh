# SPDX-License-Identifier: AGPL-3.0-only
"""#1822 — un lot léger attend la fusion, pas un verdict.

Le calcul du champ `waiting` rendait « verdict » pour tout lot en qa sans
verdict courant, même quand le pilote avait déjà route l'avis après fusion
(lots légers, canon §2) : l'alerte `stale_lot` et les notes de triage
réclamaient un verdict que personne ne devait. `stagnation.waiting` prend
désormais la classe de revue déjà résolue (argument pur), et les vues groupées
(`stagnant_lots`, `work list`/`work show`, API, `progress`) la lisent — une fois
par passage — pour les seuls lots qui attendraient un verdict. Un doute ou une
classe inconnue garde l'attente prudente : aucune classe n'est devinée.
"""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from ameesh import cost, lot_class, mesh_cli, plan, progress, stagnation, work

from .support import PgTestCase


def _jalons(**extra):
    jalons = {"verdict_kind": None, "verdict_current": False, "reviewer": None,
              "blocked_verdicts": 0}
    jalons.update(extra)
    return jalons


class WaitingClasseTest(unittest.TestCase):
    """`waiting` reste pur : la classe est un argument, None garde le verdict."""

    def _item(self, state="qa", **extra):
        item = {"state": state, "assignee": "auteur", "created_ts": 0.0}
        item.update(extra)
        return item

    def test_leger_sans_verdict_attend_la_fusion(self):
        detail = stagnation.waiting(self._item(), _jalons(), [], review_class="light")
        self.assertEqual(detail["what"], "merge")

    def test_classe_inconnue_garde_le_verdict(self):
        detail = stagnation.waiting(self._item(), _jalons(), [])
        self.assertEqual(detail["what"], "verdict")

    def test_sensible_garde_le_verdict(self):
        detail = stagnation.waiting(self._item(), _jalons(), [],
                                    review_class=lot_class.SENSITIVE)
        self.assertEqual(detail["what"], "verdict")

    def test_leger_avec_relecteur_designe_attend_aussi_la_fusion(self):
        detail = stagnation.waiting(self._item(), _jalons(reviewer="relecteur"), [],
                                    review_class=lot_class.LIGHT)
        self.assertEqual(detail["what"], "merge")

    def test_leger_verdict_bloquant_courant_attend_la_correction(self):
        detail = stagnation.waiting(
            self._item(), _jalons(verdict_kind="blocked", verdict_current=True), [],
            review_class=lot_class.LIGHT)
        self.assertEqual(detail["what"], "fix")

    def test_leger_verdict_ok_courant_inchange(self):
        act = {"connector": "git-merge", "state": "approved", "proposed_by": "pilote"}
        detail = stagnation.waiting(
            self._item(), _jalons(verdict_kind="ok", verdict_current=True), [act],
            review_class=lot_class.LIGHT)
        self.assertEqual((detail["what"], detail["who"]), ("merge", "pilote"))

    def test_leger_action_humaine_en_attente_inchangee(self):
        act = {"connector": "git-merge", "state": "proposed", "approvers": ["human:resp"]}
        detail = stagnation.waiting(self._item(), _jalons(), [act],
                                    review_class=lot_class.LIGHT)
        self.assertEqual(detail["what"], "merge-approval")

    def test_leger_en_build_inchange(self):
        detail = stagnation.waiting(self._item(state="build"), _jalons(), [],
                                    review_class=lot_class.LIGHT)
        self.assertEqual(detail["what"], "build")

    def test_leger_ferme_ne_rend_rien(self):
        self.assertIsNone(stagnation.waiting(self._item(state="merged"), _jalons(), [],
                                             review_class=lot_class.LIGHT))


class ReviewClassOfTest(unittest.TestCase):
    """La lecture de classe : un doute ne devine rien, le mémo borne le passage."""

    def test_doute_rend_none(self):
        with mock.patch.object(lot_class, "of_lot",
                               return_value={"class": "light", "doubt": True}):
            self.assertIsNone(stagnation.review_class_of(None, {"id": 7}))

    def test_exception_rend_none(self):
        with mock.patch.object(lot_class, "of_lot", side_effect=RuntimeError("boom")):
            self.assertIsNone(stagnation.review_class_of(None, {"id": 7}))

    def test_classe_lue_une_fois_par_passage(self):
        memo: dict = {}
        with mock.patch.object(lot_class, "of_lot",
                               return_value={"class": "light", "doubt": False}) as of_lot:
            self.assertEqual(stagnation.review_class_of(None, {"id": 7}, memo=memo), "light")
            self.assertEqual(stagnation.review_class_of(None, {"id": 7}, memo=memo), "light")
        self.assertEqual(of_lot.call_count, 1)


class VuesClasseTest(PgTestCase):
    """Les vues groupées : liste, show, alerte stale_lot, instantané de progrès."""

    def setUp(self) -> None:
        super().setUp()
        self.register("auteur", "claude", cwd=self.tmp)
        self.item = work.add(self.db, title="lot léger", app="demo", assignee="auteur")
        self.lot = int(self.item["id"])
        work.move(self.db, self.lot, "build", actor="auteur")
        work.move(self.db, self.lot, "qa", actor="auteur")

    def _book(self):
        return cost.CostBook(state_dir=self.state,
                             codex_sessions=os.path.join(self.tmp, "codex"),
                             db=self.db)

    def _classe(self, cls, *, doubt=False):
        return mock.patch.object(
            lot_class, "of_lot",
            return_value={"lot": self.lot, "class": cls, "doubt": doubt,
                          "why": "test", "files": []})

    def _rows(self):
        rows = work.list_items(self.db, limit=10)
        self.assertEqual([int(r["id"]) for r in rows], [self.lot])
        return rows

    def _age(self):
        """Vieillit la ligne, ses transitions et ses jalons au-delà du seuil."""
        for sql in ("UPDATE work_items SET created_at = now() - interval '8 hours',"
                    " updated_at = now() - interval '7 hours' WHERE id = %s",
                    "UPDATE work_item_events SET created_at = now() - interval '7 hours'"
                    " WHERE work_item_id = %s",
                    "UPDATE work_item_milestones SET at = now() - interval '7 hours'"
                    " WHERE work_item_id = %s"):
            self.db.execute(sql, (self.lot,))

    def test_liste_leger_attend_la_fusion(self):
        rows = self._rows()
        with self._classe(lot_class.LIGHT):
            plan.annotate(self.db, rows, cfg=self.cfg)
        self.assertEqual(rows[0]["waiting_for"]["what"], "merge")

    def test_liste_sensible_garde_le_verdict(self):
        rows = self._rows()
        with self._classe(lot_class.SENSITIVE):
            plan.annotate(self.db, rows, cfg=self.cfg)
        self.assertEqual(rows[0]["waiting_for"]["what"], "verdict")

    def test_liste_doute_garde_le_verdict(self):
        rows = self._rows()
        with self._classe(lot_class.LIGHT, doubt=True):
            plan.annotate(self.db, rows, cfg=self.cfg)
        self.assertEqual(rows[0]["waiting_for"]["what"], "verdict")

    def test_liste_sans_contexte_garde_le_verdict(self):
        # sans cfg, aucune classe n'est lisible : l'attente reste prudente
        rows = self._rows()
        plan.annotate(self.db, rows)
        self.assertEqual(rows[0]["waiting_for"]["what"], "verdict")

    def test_classe_deja_resolue_n_est_pas_relue(self):
        rows = self._rows()
        with mock.patch.object(lot_class, "of_lot",
                               side_effect=AssertionError("classe relue")):
            plan.annotate(self.db, rows, cfg=self.cfg,
                          review_class={self.lot: lot_class.LIGHT})
        self.assertEqual(rows[0]["waiting_for"]["what"], "merge")

    def test_show_leger_attend_la_fusion(self):
        with self._classe(lot_class.LIGHT):
            item = mesh_cli.work_detail(self.cfg, self.db, self.lot)
        self.assertEqual(item["review_class"]["class"], "light")
        self.assertEqual(item["waiting_for"]["what"], "merge")

    def test_alerte_stale_leger_attend_la_fusion(self):
        self._age()
        with self._classe(lot_class.LIGHT):
            lots = stagnation.stagnant_lots(self.db, now=time.time(), cfg=self.cfg)
        self.assertEqual([int(r["id"]) for r in lots], [self.lot])
        self.assertEqual(lots[0]["waiting"]["what"], "merge")

    def test_instantane_leger_attend_la_fusion(self):
        with self._classe(lot_class.LIGHT):
            snap = progress.snapshot(self.db, self.cfg, since="24h", book=self._book())
        lots = {int(r["id"]): r for r in snap["lots"]}
        self.assertEqual(lots[self.lot]["waiting_for"]["what"], "merge")
