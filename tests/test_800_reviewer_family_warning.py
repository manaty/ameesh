# SPDX-License-Identifier: AGPL-3.0-only
"""#800 : avertir (sans refuser) un relecteur de la famille de l'auteur.

Le canon §2 demande une relecture par une autre famille de modèle ; la
désignation (#705) laissait choisir un agent du **même harnais déclaré** que
l'auteur. `work move qa --reviewer` et `work reviewer` rendent désormais un
avertissement indicatif sur stderr : la désignation est conservée, un humain
ou un harnais inconnu n'avertissent pas, et la famille réelle se juge au tour
de relecture (décision 18855a).
"""
from __future__ import annotations

from ameesh import work

from .support import PgTestCase


class ReviewerFamilyWarningTest(PgTestCase):
    def _lot(self, title="un lot", assignee="auteur", **extra):
        self.register(assignee, "claude", cwd=self.tmp)
        # #705 : la désignation est un geste de pilote (`work.corrector`) : les
        # tests désignent en tant qu'orchestrateur déclaré, jamais en tant
        # qu'assigné du lot.
        self.register("chef", "claude", cwd=self.tmp)
        return work.add(self.db, title=title, assignee=assignee, **extra)

    def _env(self):
        return self.env(AMEESH_ALERT_ORCHESTRATORS="chef")

    def _qa(self, lot, *args):
        proc = self.mesh("work", "move", str(lot["id"]), "build", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.mesh("work", "move", str(lot["id"]), "qa", *args,
                         "--actor", "chef", env=self._env())

    def test_meme_harnais_avertit_et_conserve_la_designation(self):
        self.register("relecteur", "claude", cwd=self.tmp)
        lot = self._lot()
        proc = self._qa(lot, "--reviewer", "relecteur")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("reviewer relecteur", proc.stdout)
        self.assertIn("warning:", proc.stderr)
        self.assertIn("harness 'claude'", proc.stderr)
        self.assertIn("designation is kept", proc.stderr)
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["state"], item["reviewer"]), ("qa", "relecteur"))

    def test_autre_harnais_sans_avertissement(self):
        self.register("relecteur", "deepseek", cwd=self.tmp)
        lot = self._lot()
        proc = self._qa(lot, "--reviewer", "relecteur")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("warning:", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "relecteur")

    def test_relecteur_humain_sans_avertissement(self):
        lot = self._lot()
        proc = self._qa(lot, "--reviewer", "human:smichea")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("warning:", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "human:smichea")

    def test_harnais_inconnu_sans_avertissement(self):
        self.register("relecteur", "claude", cwd=self.tmp)
        # Le registre exige un harnais (NOT NULL) : le cas « inconnu » est la
        # valeur vide, que `reviewer_family_warning` ne compare pas.
        self.db.execute("UPDATE agent_registry SET harness = '' WHERE name = %s",
                        ("relecteur",))
        lot = self._lot()
        proc = self._qa(lot, "--reviewer", "relecteur")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("warning:", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "relecteur")

    def test_sans_relecteur_aucun_avertissement(self):
        lot = self._lot()
        proc = self._qa(lot)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("warning:", proc.stderr)

    def test_work_reviewer_avertit_puis_change_de_famille(self):
        self.register("meme", "claude", cwd=self.tmp)
        self.register("autre", "deepseek", cwd=self.tmp)
        lot = self._lot()
        proc = self._qa(lot)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        proc = self.mesh("work", "reviewer", str(lot["id"]), "meme",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("reviewer meme", proc.stdout)
        self.assertIn("warning:", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "meme")

        proc = self.mesh("work", "reviewer", str(lot["id"]), "autre",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("warning:", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "autre")

    def test_helper_sans_auteur_ou_auteur_humain(self):
        # Aucun appel au registre n'est nécessaire : le vide et l'humain
        # n'avertissent pas, même sans relecteur inscrit.
        self.assertEqual(work.reviewer_family_warning(
            self.db, {"id": 1, "assignee": "human:smichea"}, "relecteur"), "")
        self.assertEqual(work.reviewer_family_warning(
            self.db, {"id": 1, "assignee": ""}, "relecteur"), "")
        self.assertEqual(work.reviewer_family_warning(
            self.db, {"id": 1, "assignee": "auteur"}, ""), "")
