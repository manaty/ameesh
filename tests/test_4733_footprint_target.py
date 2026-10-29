# SPDX-License-Identifier: AGPL-3.0-only
"""#4733 — la base de l'empreinte `diff` est la cible d'intégration du projet.

Constat (orchestrateur #166603, 29/10 23:10Z) : un lot Nexlink sans
`branch_target` (`#4651`, « repository default target ») portait 354 chemins
`diff` pour 29 réels. `_clone_and_target` retombait sur `origin/HEAD` du clone
(`main`, la prod), pas sur la cible d'intégration déclarée (`develop`) : le
diff `main...branche` comptait tout l'écart develop/main, et `--refresh`
recalculait la même chose. Effets : faux chevauchements et assignations
refusées.

Attendu :
- la base est `branch_target`, sinon la cible du projet
  (`radar.projects.<p>.target`), sinon la branche par défaut du dépôt ;
- `--refresh` lit la pointe **publiée** de la cible (clone en retard : il le
  dit au lieu de faire passer sa ref pour la cible du jour) ;
- des lignes `diff` fausses se remplacent (`--refresh`) ou se retirent
  (`--clear-diff`, geste de pilote).

Témoin : un dépôt dont `main` et `develop` divergent, un lot sans cible
explicite, et le diff ne contient que le fichier du lot.
"""
from __future__ import annotations

import dataclasses
import os
import unittest

from ameesh import storage, work, work_footprints as fp

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo


class FootprintTargetTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, self._testMethodName)
        os.makedirs(self.root, exist_ok=True)
        self.repo = _Repo(os.path.join(self.root, "nexlink"))
        self.repo.write("README.md", "base\n")
        self.repo.commit("base")
        self.repo.git("branch", "-M", "main")
        # `develop` a divergé de `main` : le diff `main...branche` porte tout
        # cet écart, pas les fichiers du lot.
        self.repo.git("checkout", "-qb", "develop")
        for name in ("a", "b", "c"):
            self.repo.write("backend/develop-only-%s.ts" % name, "%s\n" % name)
        self.repo.commit("develop only")
        self.develop = self.repo.git("rev-parse", "develop")
        self.repo.git("checkout", "-qb", "lot/4651")
        self.repo.write("backend/lot.ts", "lot\n")
        self.head = self.repo.commit("lot")
        self.cfg = dataclasses.replace(self.cfg, radar={"projects": {
            "nexlink": {"repo": self.repo.root, "target": "develop"}}})

    def lot(self, branch="lot/4651"):
        return work.add(self.db, title="lot sans cible explicite", app="nexlink",
                        branch=branch)

    def test_un_lot_sans_cible_explicite_se_lit_sur_la_cible_du_projet(self):
        item = self.lot()
        # Le bug : depuis `main` (origin/HEAD), le diff porte l'écart
        # develop/main en plus du fichier du lot.
        from_main = fp.diff_paths(self.repo.root, "main", self.head)
        self.assertIn("backend/develop-only-a.ts", from_main)

        result = fp.refresh(self.db, self.cfg, item["id"], actor="test",
                            repo=self.repo.root)

        self.assertEqual(result["paths"], ["backend/lot.ts"])
        self.assertEqual(result["target"], "develop")
        self.assertEqual(fp.paths_of(self.db, item["id"]), ["backend/lot.ts"])

    def test_refresh_lit_la_pointe_publiee_d_une_cible_en_retard(self):
        origin = os.path.join(self.root, "origin.git")
        self.repo.git("init", "--bare", "-q", origin)
        self.repo.git("remote", "add", "origin", origin)
        self.repo.git("push", "-q", "origin", "main", "develop")
        reader = _Repo(os.path.join(self.root, "reader"))
        reader.git("remote", "add", "origin", origin)
        reader.git("fetch", "-q", "origin", "main:refs/remotes/origin/main",
                   "develop:refs/remotes/origin/develop")
        self.assertEqual(reader.git("rev-parse", "refs/remotes/origin/develop"),
                         self.develop)
        # La cible publiée avance après le dernier fetch du clone de lecture.
        self.repo.git("checkout", "-q", "develop")
        self.repo.write("backend/livre.ts", "another lot\n")
        delivered = self.repo.commit("another lot delivered on develop")
        self.repo.git("push", "-q", "origin", "develop")
        # Le lot part de cette nouvelle tête et ne touche qu'un fichier.
        self.repo.git("checkout", "-qb", "lot/4651-frais")
        self.repo.write("backend/propre.ts", "propre\n")
        head = self.repo.commit("lot")
        self.repo.git("push", "-q", "origin", "lot/4651-frais")
        reader.git("fetch", "-q", "origin",
                   "lot/4651-frais:refs/remotes/origin/lot/4651-frais")
        item = self.lot("lot/4651-frais")

        # Sans la lecture publiée, la ref du clone en retard compterait le
        # fichier déjà livré dans la cible.
        self.assertIn("backend/livre.ts",
                      fp.diff_paths(reader.root, "origin/develop", head))

        result = fp.refresh(self.db, self.cfg, item["id"], actor="test",
                            repo=reader.root)

        self.assertEqual(result["paths"], ["backend/propre.ts"])
        self.assertEqual(result["base"], delivered)
        self.assertTrue(result["fresh"])


class ClearDiffTest(PgTestCase):
    def test_clear_diff_retire_les_lignes_fausses_et_garde_l_estimation(self):
        item = work.add(self.db, title="faux diff", app="ameesh")
        storage.of(self.db).work_footprints.replace(
            int(item["id"]), "intake", ["docs"], actor="test")
        storage.of(self.db).work_footprints.replace(
            int(item["id"]), "diff", ["main-only/a.ts"], actor="test")
        self.assertEqual(fp.paths_of(self.db, item["id"]),
                         ["docs", "main-only/a.ts"])

        proc = self.mesh("work", "footprint", str(item["id"]), "--clear-diff",
                         "--actor", "ameesh-dev99")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("pilot gesture", proc.stderr)
        self.assertEqual(fp.paths_of(self.db, item["id"]),
                         ["docs", "main-only/a.ts"])

        proc = self.mesh("work", "footprint", str(item["id"]), "--clear-diff",
                         "--actor", "human:proprio")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(fp.paths_of(self.db, item["id"]), ["docs"])


if __name__ == "__main__":
    unittest.main()
