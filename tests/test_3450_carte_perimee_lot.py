# SPDX-License-Identifier: AGPL-3.0-only
"""#3450 — une carte périmée ne renvoie plus chaque CI de lot à la suite complète.

La carte du 17/10 est restée seule 176 h : `scripts/test-plan.py` refusait
toute carte de plus de 48 h, chaque CI de lot lançait la suite complète, la
suite saturait le pool, la mesure de `carte.yml` était coupée à son plafond de
60 min et la carte ne se republiait pas. La boucle s'entretenait.

Le plan juge maintenant, sur une carte périmée, le **diff propre** de la
branche (base de fusion → tête) : les changements déjà fusionnés sur `main`
ont passé sa suite complète et la suite complète du paquet reste la preuve
d'intégration (canon §12). Un lot garde donc les impactés + le contrat ; le
doute d'un fichier que la branche change elle-même (Python hors `src/ameesh`,
bloc sans test) reste, lui, un repli sur la suite complète.

Témoins : carte de 176 h → sélection impactés + contrat (PR et `--mode lot`) ;
un fichier non mesurable hérité de `main` ne force plus le repli ; une carte
de 30 h garde le comportement d'avant (entre 24 h et 48 h) ; un fichier non
mesurable changé par la branche force toujours la suite complète.
"""
from __future__ import annotations

import datetime
import pathlib
import unittest
from unittest import mock

from .test_l161f_plan import _Repo, _git, NOW, plan

#: l'âge de la carte du 17/10 dans le constat du lot
PERIMEE_H = 176


class StaleMapLotTest(unittest.TestCase):
    MODULES = ["test_a", "test_b", "test_c", "test_db", "test_old"]

    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(self.repo.close)
        self.card = self.repo.card()
        # des durées pour les deux pilotes : sans elles, le découpage lit
        # `tests/<module>.py`, qui n'existe pas pour ces modules synthétiques.
        self.card["modules"] = {m: {"durees": {"psycopg": 1.0, "psql": 1.0},
                                    "echecs": []} for m in self.MODULES}
        self.base = None

    # -- un dépôt avec une cible qui avance après la carte, puis la branche --
    def _merge_branch(self, target=None, own=None) -> str:
        """Cible `main` (carte M → base B), branche de lot (B → H), mergée.

        Rend le SHA de la base B : c'est `HEAD^1` du commit de fusion que la CI
        GitHub teste, donc le range propre de la branche (`--base`).
        """
        repo = self.repo.path
        _git(repo, "checkout", "-q", "-b", "main", self.repo.base)
        if target is not None:
            target()
        self.repo.commit()
        base_b = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-q", "-b", "lot/3450-carte-boucle")
        (own or self._own_change)()
        self.repo.commit()
        _git(repo, "checkout", "-q", "main")
        _git(repo, "merge", "--no-ff", "-q", "lot/3450-carte-boucle",
             "-m", "merge lot 3450")
        return base_b

    def _own_change(self):
        self.repo.write("src/ameesh/code.py",
                        self.repo.code.replace("x + 1", "x + 2"))

    def _own_unmeasured_change(self):
        self._own_change()
        self.repo.write("scripts/outil_du_lot.py", "VALUE = 1\n")

    def _target_unmeasured_change(self):
        self.repo.write("scripts/outil_de_main.py", "VALUE = 1\n")

    def _plan(self, age_h: int, base: str, mode: str = "auto",
              event: str = "pull_request") -> dict:
        card = dict(self.card,
                    genere=(NOW - datetime.timedelta(hours=age_h)).isoformat())

        def read_list(path, known, required=False):
            name = pathlib.PurePosixPath(path).name
            if name == "toujours.txt":
                return ["test_db"]
            if name == "pilote-psql.txt":
                return ["test_db"]
            return []

        nature = {"version_only": False, "docs_only": False, "why": "",
                  "files": []}
        with mock.patch.object(plan.parts, "modules", lambda *a: list(self.MODULES)), \
                mock.patch.object(plan.parts, "test_count",
                                  lambda module, tests=None: 1), \
                mock.patch.object(plan, "read_list", read_list), \
                mock.patch.object(plan, "textual", lambda *a, **k: {}), \
                mock.patch.object(plan, "nature", lambda *a, **k: dict(nature)):
            return plan.make_plan(event, card, NOW, base=base, head="HEAD",
                                  repo=self.repo.path, base_ref="main",
                                  head_ref="lot/3450-carte-boucle", mode=mode)

    def _ran(self, p: dict) -> list[str]:
        return sorted(m for ps in p["parts"]["psycopg"] for m in ps["modules"])

    # -- la carte périmée juge le diff propre de la branche ------------------
    def test_carte_de_176_h_garde_les_impactes_et_le_contrat(self):
        self.base = self._merge_branch()
        p = self._plan(PERIMEE_H, self.base)
        self.assertTrue(p["map"]["used"], p["map"])
        self.assertTrue(p["map"]["stale"])
        self.assertFalse(p["phase2_safe"])
        self.assertTrue(p["lot_safe"], p["phase2_why"])
        self.assertEqual(p["diff_since"], "base")
        self.assertEqual(p["selection"]["mode"], "impacted")
        self.assertEqual(p["selection"]["why"], "")
        # test_a exécute le bloc `used` changé ; test_db est le contrat.
        self.assertEqual(self._ran(p), ["test_a", "test_db"])
        self.assertNotIn("test_b", self._ran(p), "la branche n'a pas changé son bloc")
        self.assertNotIn("test_c", self._ran(p))
        self.assertNotIn("test_old", self._ran(p))
        # le résumé dit la carte périmée et l'avertissement ne parle plus de
        # « suite complète » pour un lot.
        text = plan.summary(p)
        self.assertIn("carte périmée", text)
        self.assertIn("#3450", text)
        self.assertIn("base commit", text)
        self.assertIn("gardent les impactés", " ".join(plan.annotations(p)))

    def test_le_ci_de_gel_sur_la_carte_perimee_garde_les_impactes(self):
        # #1343 : la CI demandée au gel (mode lot) suit la même sélection.
        self.base = self._merge_branch()
        p = self._plan(PERIMEE_H, self.base, mode="lot")
        self.assertEqual(p["selection"]["mode"], "impacted")
        self.assertEqual(self._ran(p), ["test_a", "test_db"])

    def test_un_fichier_non_mesurable_de_main_ne_force_plus_le_repli(self):
        # Le jugement depuis le commit de la carte verrait ce `scripts/*.py`
        # ajouté par la cible : doute « Python outside src/ameesh », donc suite
        # complète à chaque lot — la boucle #3450. Le diff propre de la branche
        # ne le contient pas ; la cible l'a déjà passé en suite complète.
        self.base = self._merge_branch(target=self._target_unmeasured_change)
        p = self._plan(PERIMEE_H, self.base)
        self.assertTrue(p["lot_safe"], p["phase2_why"])
        self.assertEqual(p["selection"]["mode"], "impacted", p["selection"])
        self.assertEqual(self._ran(p), ["test_a", "test_db"])

    # -- les garde-fous restent ------------------------------------------------
    def test_carte_de_30_h_garde_le_comportement_d_avant(self):
        # Entre 24 h et 48 h, la sélection réduite reste refusée : la carte est
        # trop vieille pour elle, mais pas encore « périmée » au sens de
        # l'alerte. Le lot garde donc la suite complète.
        self.base = self._merge_branch()
        p = self._plan(30, self.base)
        self.assertFalse(p["map"]["stale"])
        self.assertFalse(p["lot_safe"], p["phase2_why"])
        self.assertEqual(p["selection"]["mode"], "full")
        self.assertIn("map older than 24 h", p["selection"]["why"])
        self.assertEqual(self._ran(p), sorted(self.MODULES))

    def test_un_fichier_non_mesurable_du_lot_force_toujours_la_suite_complete(self):
        self.base = self._merge_branch(own=self._own_unmeasured_change)
        p = self._plan(PERIMEE_H, self.base)
        self.assertFalse(p["lot_safe"])
        self.assertEqual(p["selection"]["mode"], "full")
        self.assertIn("not measured", p["selection"]["why"])
        self.assertEqual(self._ran(p), sorted(self.MODULES))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
