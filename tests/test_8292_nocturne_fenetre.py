# SPDX-License-Identifier: AGPL-3.0-only
"""#8292 — le nocturne psql borne ses parts par la fenêtre 06Z, pas 32.

#8291 relève le nombre de parts sous le plafond de 60 min du job ; sa tête
corrigée porte 20 en dur après la relecture de #8291. #8292 dérive ce
plafond de la fenêtre : le nocturne part à 01:47Z, la porte ferme
l'admission à 06:00Z et `tests.yml` ne laisse courir que quatre parts de
front, donc cinq vagues de 60 min tiennent (la dernière est admise à
05:47Z) et la sixième (06:47Z) serait refusée. Une table plus lente ne doit
pas planifier de parts que la porte rejetterait ; quand les 20 parts ne
tiennent toujours pas la cible, la saturation de la fenêtre est signalée.
Témoins sans base : le plan, son résumé, son avertissement et les trois
sources de la fenêtre.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import pathlib
import re
import tempfile
import unittest
from unittest import mock

from ameesh import canon, psql_nocturne, role_timers

REPO = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime.datetime(2026, 11, 26, 1, 47, tzinfo=datetime.timezone.utc)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan = _load("test_plan_8292", "test-plan.py")


def _workflow(name: str) -> dict:
    return canon.load_yaml((REPO / ".github" / "workflows" / name)
                           .read_text(encoding="utf-8")) or {}


class FenetreTest(unittest.TestCase):
    def setUp(self):
        self.mods = plan.parts.modules()
        self.cible = plan.psql_full_target_s()
        self.state = {
            "map": None, "sure": True, "phase2_safe": True, "lot_safe": True,
            "phase2_why": "", "unsure": [], "untested": [], "unknown_blocks": [],
            "foundation": [], "changed": 1, "always": [], "contract": [],
            "classes": {m: ["P3", ""] for m in self.mods}}

    def _table(self, poids: dict[str, float]):
        table = {"_generated": "2026-11-26T00:00:00Z", "psql": {},
                 "psql_full": poids}
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
            return plan.make_plan("workflow_dispatch", {}, NOW,
                                  contract_policy=None, psql_full=True)

    def _nuit(self, p):
        nuit = p["parts"]["psql"]
        self.assertTrue(nuit)
        self.assertEqual([e["part"] for e in nuit], list(range(1, len(nuit) + 1)))
        self.assertEqual({e["of"] for e in nuit}, {len(nuit)})
        self.assertEqual(sorted(m for e in nuit for m in e["modules"]), self.mods)
        self.assertEqual(plan.outputs(p)["psql_parts"], str(len(nuit)))
        return nuit

    def test_une_table_plus_lente_est_bornee_par_la_fenetre(self):
        """Une table plus lente remplit la fenêtre : 20 parts, pas 32."""
        self._table({m: 300.0 for m in self.mods})
        p = self._plan()
        nuit = self._nuit(p)
        self.assertEqual(len(nuit), plan.PSQL_FULL_PARTS_MAX)
        self.assertEqual(len(nuit), 20)
        self.assertLess(len(nuit), 32)
        self.assertGreater(max(e["estimate_s"] for e in nuit), self.cible)
        self.assertIn("the 06Z window admits at most 20 parts", plan.summary(p))
        alertes = [a for a in plan.annotations(p) if "#8292" in a]
        self.assertEqual(len(alertes), 1, alertes)
        self.assertTrue(alertes[0].startswith(
            "::warning title=Fenêtre nocturne saturée::20 parts"), alertes[0])
        self.assertIn("refusée à l'admission", alertes[0])

    def test_le_repli_borne_aussi_les_parts(self):
        self._table({m: 300.0 for m in self.mods})
        p = plan.fallback_plan("schedule", "boom", psql_full=True)
        nuit = self._nuit(p)
        self.assertEqual(len(nuit), plan.PSQL_FULL_PARTS_MAX)

    def test_une_table_qui_tient_a_vingt_parts_n_est_pas_saturee(self):
        # 20 parts suffisent (sous la cible) : le plafond de la fenêtre n'est
        # pas la raison de l'arrêt, aucun avertissement #8292.
        total = 19.5 * self.cible
        self._table({m: total / len(self.mods) for m in self.mods})
        p = self._plan()
        nuit = self._nuit(p)
        self.assertEqual(len(nuit), 20)
        self.assertLessEqual(max(e["estimate_s"] for e in nuit), self.cible)
        self.assertEqual([a for a in plan.annotations(p) if "#8292" in a], [])

    def test_les_sources_de_la_fenetre_sont_accordees(self):
        """La minuterie, la porte et le workflow portent les constantes."""
        calendrier = re.search(r"\s(\d{2}):(\d{2}):",
                               role_timers.NOCTURNE_CALENDAR)
        self.assertIsNotNone(calendrier, role_timers.NOCTURNE_CALENDAR)
        debut = int(calendrier.group(1)) * 60 + int(calendrier.group(2))
        self.assertEqual(debut, plan.PSQL_FULL_WINDOW_START_MIN)
        self.assertEqual(psql_nocturne.END_HOUR * 60, plan.PSQL_FULL_WINDOW_END_MIN)
        job = _workflow("tests.yml")["jobs"]["psql"]
        self.assertIn("inputs.psql-full && %d" % plan.PSQL_FULL_ADMISSIONS,
                      str(job["strategy"]["max-parallel"]))
        self.assertEqual(job["timeout-minutes"], plan.PSQL_FULL_PLAFOND_MIN)
        self.assertEqual(plan.PSQL_FULL_WAVES,
                         (plan.PSQL_FULL_WINDOW_END_MIN
                          - plan.PSQL_FULL_WINDOW_START_MIN - 1)
                         // plan.PSQL_FULL_PLAFOND_MIN + 1)
        self.assertEqual(plan.PSQL_FULL_PARTS_MAX,
                         plan.PSQL_FULL_ADMISSIONS * plan.PSQL_FULL_WAVES)
        self.assertEqual(plan.PSQL_FULL_ADMISSIONS, 4)
        self.assertEqual(plan.PSQL_FULL_PARTS_MAX, 20)


if __name__ == "__main__":
    unittest.main()
