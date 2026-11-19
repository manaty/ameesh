# SPDX-License-Identifier: AGPL-3.0-only
"""#7823 — la prévalidation au gel conclut sur une sélection incertaine.

Constat (auditeur, 19/11 01:14Z) : chaque lot gelé le 18/11 avait une
prévalidation « run-incomplete » (0 test exécuté, sortie 124). La carte
8f47b41f avait 46 h : le plan jugeait 511 fichiers de dérive de `main`, ses P0
portaient 247 modules que le lot ne touchait pas, et le repli contractuel les
jouait tous en une seule part, coupée par le budget du gel sans rien rendre.
#7813 changeait `src/ameesh/ci.py` et test_3611 n'a pas tourné avant le paquet.

Témoins : le repli ne prend que les tests du diff propre du lot, plus les
importateurs statiques de ses fichiers sous budget ; il joue le lot dans une
première part, les contrats ensuite ; une part rouge finie avant l'échéance
reste un rouge ; le gel affiche une prévalidation non concluante.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import replace

from ameesh import prevalidation as pv
from ameesh import prevalidation_calls as calls
from tests.test_2644_prevalidation_calls import FULL_PLAN_SCRIPT, Repo, params


class BoundedFallbackTest(unittest.TestCase):

    def setUp(self):
        # le plan factice classe en P0 un module que le lot ne touche pas (la
        # dérive de `main` depuis la carte)
        script = FULL_PLAN_SCRIPT.replace(
            "'classes': {}",
            "'classes': {'test_derive': ['P0', 'tests/test_derive.py'],"
            " 'test_3611_fire': ['P0', 'tests/test_3611_fire.py']}")
        self.repo = Repo(script=script)
        self.addCleanup(self.repo.cleanup)
        self.repo.write("tests/parts/toujours.txt", "test_cluster\n")
        for name in ("test_cluster", "test_l107_executor_contract",
                     "test_l161f_plan", "test_migrations_additives",
                     "test_derive", "test_lent"):
            self.repo.write("tests/%s.py" % name, "x = 1\n")
        self.repo.write("tests/test_lent.py", "import ameesh.ci\n")
        self.repo.write("tests/test_cible.py", "from ameesh import ci, land\n")
        self.repo.write("tests/test_autre.py", "from ameesh.ci import run\n")
        self.repo.write("tests/test_sans_lien.py", "from ameesh import circle\n")
        self.repo.write("tests/parts/durees.json", json.dumps(
            {"psycopg": {"test_cible": 5.0, "test_autre": 10.0,
                         "test_lent": calls.FALLBACK_IMPORTERS_BUDGET_S}}))
        self.repo.write("src/ameesh/ci.py", "x = 1\n")
        self.repo.write("src/ameesh/circle.py", "x = 1\n")
        self.base = self.repo.commit("base du lot")
        self.repo.write("src/ameesh/ci.py", "x = 2\n")
        self.repo.write("tests/test_3611_fire.py", "from ameesh import ci\n")
        self.head = self.repo.commit("lot")

    def parameters(self):
        block = replace(params(hosts=("vm1",)),
                        contracts=("tests/parts/toujours.txt",))
        return calls.Parameters(params=block, repo="/canon", commit="c" * 40,
                                project="ameesh")

    def fallback(self):
        prepared = calls.prepare(self.repo.root, self.head, self.base)
        self.addCleanup(prepared.close)
        got = calls.contract_fallback(prepared, self.parameters(),
                                      head=self.head, base=self.base)
        self.assertIsNotNone(got)
        return got, prepared.plan

    def test_drift_p0_is_not_played_own_tests_are(self):
        got, plan = self.fallback()
        self.assertEqual(got["changed_tests"], ["test_3611_fire"])
        self.assertNotIn("test_derive", pv.planned_modules(plan))

    def test_static_importers_within_budget(self):
        got, plan = self.fallback()
        # test_lent dépasse le budget ; test_sans_lien importe `circle`, pas `ci`
        self.assertEqual(got["importers"], ["test_autre", "test_cible"])
        self.assertEqual(got["importers_dropped"], 1)
        self.assertNotIn("test_sans_lien", pv.planned_modules(plan))

    def test_the_lot_part_runs_before_the_contracts(self):
        _, plan = self.fallback()
        parts = plan["parts"]["psycopg"]
        self.assertEqual([p["label"] for p in parts],
                         ["psycopg lot", "psycopg contrats"])
        self.assertEqual(parts[0]["modules"],
                         ["test_3611_fire", "test_autre", "test_cible"])
        self.assertIn("test_cluster", parts[1]["modules"])
        self.assertEqual([(p["part"], p["of"]) for p in parts], [(1, 2), (2, 2)])
        chosen = pv.selection(plan, head=self.head, base=self.base)
        self.assertTrue(chosen.ok, chosen.why)
        self.assertEqual(len(chosen.parts), 2)

    def test_dotted_names(self):
        self.assertEqual(calls._dotted("src/ameesh/ci.py"), "ameesh.ci")
        self.assertEqual(calls._dotted("src/ameesh/storage/__init__.py"),
                         "ameesh.storage")
        self.assertEqual(calls._dotted("tests/support.py"), "tests.support")
        self.assertEqual(calls._dotted("tests/test_a.py"), "")
        self.assertEqual(calls._dotted("docs/a.md"), "")


class RedBeforeDeadlineTest(unittest.TestCase):

    def chosen(self):
        return pv.Selection(
            head="a" * 40, base="b" * 40, modules=("test_lot", "test_cluster"),
            parts=({"driver": "psycopg", "part": 1, "of": 2, "modules": ("test_lot",)},
                   {"driver": "psycopg", "part": 2, "of": 2,
                    "modules": ("test_cluster",)}),
            fallback={"cause": "selection-full", "causes": ["selection-full"],
                      "changed_tests": ["test_lot"], "importers": [],
                      "importers_dropped": 0})

    def proof(self, fin: str, failed=()):
        flow = {"modules": ("test_lot",), "tests": 4, "failed": tuple(failed),
                "fins_par_part": {"psycopg:1": fin},
                "failed_by_part": {"psycopg:1": tuple(failed)} if failed else {},
                "environment": (), "parts": ("psycopg:1", "psycopg:2")}
        return self.chosen().proof(
            lot=7823, host="vm1",
            run={"outcome": "run-incomplete", "exit": 124, "seconds": 280},
            flow=flow)

    def test_a_finished_red_part_is_red_despite_the_deadline(self):
        got = self.proof("echec", failed=("tests.test_lot.T.test_x",))
        self.assertEqual(got["outcome"], "run-red")
        self.assertEqual(got["causes"], ["run-red"])
        self.assertIn("tests.test_lot.T.test_x", " ".join(got["detail"]))

    def test_a_green_lot_part_cut_during_contracts_stays_incomplete(self):
        got = self.proof("succes")
        self.assertEqual(got["outcome"], "run-incomplete")
        self.assertEqual(got["modules"], ["test_lot"])
        self.assertIn("lot : 1 test(s) changé(s)", " ".join(got["detail"]))


class FreezeLineTest(unittest.TestCase):

    def test_unavailable_is_said_with_what_ran(self):
        line = calls.freeze_line({"state": "unavailable", "report": {
            "modules": [], "planned": ["m%d" % i for i in range(267)],
            "causes": ["run-incomplete"], "missing": ["test_1003_merge_pr"]}})
        self.assertIn("NON CONCLUANTE", line)
        self.assertIn("0/267", line)
        self.assertIn("ameesh work prevalidate", line)

    def test_green_red_and_unknown(self):
        self.assertIn("verte", calls.freeze_line(
            {"state": "green", "report": {"modules": ["a"], "planned": ["a"]}}))
        self.assertIn("ROUGE", calls.freeze_line(
            {"state": "red", "report": {"causes": ["run-red"],
                                        "failed": ["tests.test_a.T.t"]}}))
        self.assertEqual(calls.freeze_line(None), "")
        self.assertIn("NON CONCLUANTE", calls.freeze_line(
            {"state": "unavailable", "why": "VM absente"}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
