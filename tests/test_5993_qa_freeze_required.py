# SPDX-License-Identifier: AGPL-3.0-only
"""#5993 — `work move … qa` sans gel déclaré est refusé au geste.

Constat (#5985) : un lot déplacé en `qa` sans jalon `frozen` n'était plus
désigné — la désignation automatique part du gel (#3467) — et restait sans
relecteur ; rien ne le disait avant le seuil de `stale_lot` (6 h). Le geste
attendu est le gel, qui entre en `qa` et désigne. Les vues, la capacité et le
réveil affichaient déjà « waiting: freeze » (#3986) : ce lot ajoute le refus
de la commande.
"""
from __future__ import annotations

from ameesh import work

from .support import PgTestCase

FROZEN = "a" * 40


class QaFreezeRequiredTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register("auteur", "deepseek", cwd=self.tmp)
        self.item = work.add(self.db, title="revue sans gel", assignee="auteur")
        self.lot = int(self.item["id"])
        work.move(self.db, self.lot, "build", actor="auteur")

    def _freeze_row(self):
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor)"
            " VALUES (%s, 'frozen', %s, 'auteur')", (self.lot, FROZEN))

    def _move(self, state, *args):
        return self.mesh("work", "move", str(self.lot), state, *args,
                         "--actor", "auteur")

    def test_sans_gel_refuse_avec_le_geste_exact(self):
        proc = self._move("qa")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("without a declared freeze", proc.stderr)
        self.assertIn("ameesh work milestone %d frozen --sha" % self.lot,
                      proc.stderr)
        self.assertEqual(work.get(self.db, self.lot)["state"], "build")

    def test_apres_gel_inchange(self):
        self._freeze_row()
        proc = self._move("qa")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")

    def test_gel_anterieur_au_dernier_build_est_refuse(self):
        # N1 (#5993) : gel A, qa → build, branche retravaillée sans regeler.
        # Le gel A ne couvre pas la tête courante : la désignation ne suivrait
        # pas, et le lot retomberait dans le cas de #5985.
        self._freeze_row()
        self.assertEqual(self._move("qa").returncode, 0)
        self.assertEqual(self._move("build").returncode, 0)
        proc = self._move("qa")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("predates its last move to build", proc.stderr)
        self.assertIn("ameesh work milestone %d frozen --sha" % self.lot,
                      proc.stderr)
        self.assertEqual(work.get(self.db, self.lot)["state"], "build")

    def test_regele_apres_le_build_est_accepte(self):
        self._freeze_row()
        self.assertEqual(self._move("qa").returncode, 0)
        self.assertEqual(self._move("build").returncode, 0)
        self._freeze_row()  # nouveau gel, postérieur au dernier build
        proc = self._move("qa")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")

    def test_reviewer_explicite_ne_dispense_pas_du_gel(self):
        self.register("relecteur", "codex", cwd=self.tmp)
        proc = self.mesh("work", "move", str(self.lot), "qa",
                         "--reviewer", "relecteur", "--actor", "human:smichea")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("without a declared freeze", proc.stderr)
        item = work.get(self.db, self.lot)
        self.assertEqual((item["state"], item["reviewer"]), ("build", ""))

    def test_correction_n_est_pas_concernee(self):
        # `--correct` est hors machine à états : le refus attendu reste celui
        # de la correction (build → qa n'en est pas une), pas celui du gel.
        proc = self.mesh("work", "move", str(self.lot), "qa",
                         "--correct", "état posé par erreur",
                         "--actor", "human:smichea")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("correction refused", proc.stderr)
        self.assertNotIn("without a declared freeze", proc.stderr)
        self.assertEqual(work.get(self.db, self.lot)["state"], "build")

    def test_work_get_porte_la_tete_du_dernier_gel(self):
        # #5993 : le contrat médié n'a pas d'op `work.milestones` ; la tête
        # gelée voyage donc avec `work.get`, pour que le refus fonctionne
        # aussi depuis une session de VM. `frozen_at`/`build_at` permettent de
        # refuser un gel antérieur au dernier passage en build (N1).
        item = work.get(self.db, self.lot)
        self.assertIsNone(item["frozen_sha"])
        self.assertIsNone(item["frozen_at"])
        self.assertIsNotNone(item["build_at"])
        self._freeze_row()
        item = work.get(self.db, self.lot)
        self.assertEqual(item["frozen_sha"], FROZEN)
        self.assertGreaterEqual(item["frozen_at"], item["build_at"])
