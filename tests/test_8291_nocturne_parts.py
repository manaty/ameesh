# SPDX-License-Identifier: AGPL-3.0-only
"""#8291 — le nocturne psql dimensionne ses parts sur le plafond du job.

Les 25 et 26/11, une part coupée à 60 min par nuit (7/16 puis 10/16, run
45738064364) : la table `psql_full` du 08/11 annonçait 16 parts égales
(~12 050 s de tests) que le run a mesurées de 2 799 à 12 113 s. Comme la
carte depuis #8261, 16 n'est plus qu'un plancher : le plan relève le nombre
de parts tant que la plus lourde ne tient pas sous la cible, et le signale
quand un module seul la dépasse. Témoins sans base : le plan et tests.yml.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from ameesh import canon

REPO = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime.datetime(2026, 11, 26, 2, 0, tzinfo=datetime.timezone.utc)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan = _load("test_plan_8291", "test-plan.py")


class NocturnePartsTest(unittest.TestCase):
    def setUp(self):
        self.mods = plan.parts.modules()
        self.cible = plan.psql_full_target_s()
        self.state = {
            "map": None, "sure": True, "phase2_safe": True, "lot_safe": True,
            "phase2_why": "", "unsure": [], "untested": [], "unknown_blocks": [],
            "foundation": [], "changed": 1, "always": [], "contract": [],
            "classes": {m: ["P3", ""] for m in self.mods}}

    def _table(self, poids: dict[str, float]):
        table = {"_generated": "2026-11-26T00:00:00Z", "psql": {}, "psql_full": poids}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        chemin = pathlib.Path(tmp.name) / "durees.json"
        chemin.write_text(json.dumps(table), encoding="utf-8")
        patch = mock.patch.object(plan.parts, "TABLE", chemin)
        patch.start()
        self.addCleanup(patch.stop)

    def _plan(self):
        nature = {"version_only": False, "docs_only": False, "why": "", "files": []}
        with mock.patch.object(plan, "classify", return_value=dict(self.state)), \
                mock.patch.object(plan, "nature", return_value=nature):
            return plan.make_plan("workflow_dispatch", {}, NOW, contract_policy=None,
                                  psql_full=True)

    def _coherent(self, p):
        nuit = p["parts"]["psql"]
        n = len(nuit)
        self.assertEqual([e["part"] for e in nuit], list(range(1, n + 1)))
        self.assertEqual({e["of"] for e in nuit}, {n})
        self.assertEqual(sorted(m for e in nuit for m in e["modules"]), self.mods)
        self.assertEqual(plan.outputs(p)["psql_parts"], str(n))
        self.assertEqual(json.loads(plan.outputs(p)["psql"]), list(range(1, n + 1)))
        return n, max(e["estimate_s"] for e in nuit)

    def test_les_parts_sont_relevees_sous_la_cible(self):
        # le 26/11 : 16 parts de ~12 050 s, au-delà de la cible
        total = 16 * 12_050.0
        self._table({m: total / len(self.mods) for m in self.mods})
        p = self._plan()
        n, lourde = self._coherent(p)
        self.assertGreater(n, plan.PARTS["psql"][1])
        self.assertLessEqual(n, plan.PSQL_FULL_PARTS_MAX)
        self.assertLessEqual(lourde, self.cible)
        self.assertIn(f"psql night parts (#8291): {n} (floor 16)", plan.summary(p))
        self.assertFalse([a for a in plan.annotations(p) if "#8291" in a])

    def test_le_repli_releve_les_memes_parts(self):
        total = 16 * 12_050.0
        self._table({m: total / len(self.mods) for m in self.mods})
        nominal, _ = self._coherent(self._plan())
        repli = plan.fallback_plan("workflow_dispatch", "boom", psql_full=True)
        n, lourde = self._coherent(repli)
        self.assertEqual(n, nominal)
        self.assertLessEqual(lourde, self.cible)

    def test_une_suite_legere_garde_le_plancher(self):
        self._table({m: 1.0 for m in self.mods})
        n, _ = self._coherent(self._plan())
        self.assertEqual(n, plan.PARTS["psql"][1])

    def test_un_module_insecable_est_signale_sans_multiplier_les_parts(self):
        lent = self.mods[0]
        poids = {m: 1.0 for m in self.mods}
        poids[lent] = self.cible * 1.2
        self._table(poids)
        p = self._plan()
        n, lourde = self._coherent(p)
        self.assertEqual(n, plan.PARTS["psql"][1])
        self.assertGreater(lourde, self.cible)
        alertes = [a for a in plan.annotations(p) if "#8291" in a]
        self.assertEqual(len(alertes), 1, alertes)
        self.assertTrue(alertes[0].startswith("::warning "), alertes[0])

    def test_la_table_mesuree_tient_sous_la_cible(self):
        """La table livrée (run 45738064364) : aucune part au-delà de la cible."""
        _, lourde = self._coherent(self._plan())
        self.assertLessEqual(lourde, self.cible)

    def test_le_plafond_est_celui_du_job_psql(self):
        doc = canon.load_yaml((REPO / ".github" / "workflows" / "tests.yml")
                              .read_text(encoding="utf-8"))
        job = doc["jobs"]["psql"]
        self.assertEqual(job["timeout-minutes"], plan.PSQL_FULL_PLAFOND_MIN)
        self.assertEqual(job["env"]["AMEESH_PLAFOND_MIN"], str(plan.PSQL_FULL_PLAFOND_MIN))


if __name__ == "__main__":
    unittest.main()
