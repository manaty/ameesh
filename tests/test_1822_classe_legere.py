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

from ameesh import (canon, cost, lot_class, mesh_cli, plan, plan_git, progress,
                    stagnation, storage, work)

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

    def test_jalons_groupes_et_canon_partage_entre_lots(self):
        freeze = [{"kind": "frozen", "sha": "a" * 40}]
        memo: dict = {}
        with mock.patch.object(lot_class, "repository", return_value="repo"), \
             mock.patch.object(lot_class, "default_base", return_value="main"), \
             mock.patch.object(plan_git, "_commit", return_value="a" * 40), \
             mock.patch.object(canon, "git_range_paths", return_value=["README.md"]), \
             mock.patch.object(lot_class, "class_of_paths", return_value={"class": "light", "doubt": False}), \
             mock.patch.object(work, "configured_canons", return_value=[object()]) as canons, \
             mock.patch.object(work, "milestones", side_effect=AssertionError("lecture unitaire")) as reads:
            classes = [stagnation.review_class_of(None, {"id": rid}, memo=memo,
                                                  milestones=freeze) for rid in range(3)]
        self.assertEqual(classes, ["light"] * 3)
        reads.assert_not_called()
        canons.assert_called_once()

    def test_absence_de_gel_groupe_ne_relit_pas_la_base(self):
        with mock.patch.object(lot_class, "repository", return_value="repo"), \
             mock.patch.object(work, "milestones", side_effect=AssertionError("lecture unitaire")) as reads:
            self.assertIsNone(stagnation.review_class_of(None, {"id": 7}, milestones=[]))
        reads.assert_not_called()


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
        with self._classe(lot_class.LIGHT) as reading:
            plan.annotate(self.db, rows, cfg=self.cfg)
        self.assertEqual(rows[0]["waiting_for"]["what"], "merge")
        self.assertIn("milestones", reading.call_args.kwargs)

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

    def test_projection_alerte_conserve_la_cible_release(self):
        work.set_branch(self.db, self.lot, "lot/demo", "release/1.12", actor="auteur")
        row = next(r for r in storage.of(self.db).operations.open_lots_activity(None)
                   if int(r["id"]) == self.lot)
        self.assertEqual(stagnation.review_class_of(self.db, row, cfg=self.cfg), lot_class.RELEASE)

    def test_instantane_leger_attend_la_fusion(self):
        with self._classe(lot_class.LIGHT) as reading:
            snap = progress.snapshot(self.db, self.cfg, since="24h", book=self._book())
        lots = {int(r["id"]): r for r in snap["lots"]}
        self.assertEqual(lots[self.lot]["waiting_for"]["what"], "merge")
        self.assertIn("milestones", reading.call_args.kwargs)
