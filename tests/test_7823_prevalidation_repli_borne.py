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
import subprocess
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

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
        # test_lent dépasse le budget ; test_sans_lien importe `circle`, pas `ci`.
        # L'ordre est la durée mesurée (test_cible 5 s avant test_autre 10 s),
        # plus l'ordre alphabétique.
        self.assertEqual(got["importers"], ["test_cible", "test_autre"])
        self.assertEqual(got["importers_dropped"], 1)
        self.assertEqual(got["importers_skipped"], ["test_lent"])
        self.assertNotIn("test_sans_lien", pv.planned_modules(plan))

    def test_importer_patterns_are_posix_ere(self):
        # #7930 : sous macOS, `git grep -E` suit la regex BSD, qui ignore les
        # extensions GNU `\s`, `\b`, `\w`. Le graphe #8046 lit les imports en
        # une passe : son motif après -E doit rester en ERE POSIX.
        with mock.patch.object(calls.subprocess, "run", wraps=subprocess.run) as run:
            got, _ = self.fallback()
        greps = [c.args[0] for c in run.call_args_list
                 if c.args and isinstance(c.args[0], list) and "grep" in c.args[0]]
        self.assertTrue(greps)
        for argv in greps:
            patterns = [argv[i + 1] for i, a in enumerate(argv)
                        if a in ("-e", "-E") and i + 1 < len(argv)]
            self.assertTrue(patterns, argv)
            for pattern in patterns:
                for gnu in ("\\s", "\\b", "\\w", "\\<", "\\>"):
                    self.assertNotIn(gnu, pattern)
        self.assertEqual(got["importers"], ["test_cible", "test_autre"])

    def test_the_lot_part_runs_before_the_contracts(self):
        _, plan = self.fallback()
        parts = plan["parts"]["psycopg"]
        self.assertEqual([p["label"] for p in parts],
                         ["psycopg lot", "psycopg contrats"])
        self.assertEqual(parts[0]["modules"],
                         ["test_3611_fire", "test_cible", "test_autre"])
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


class ImportGraphTest(unittest.TestCase):
    """#8046 : le graphe d'imports, sa distance et la coupe par racine."""

    def test_import_targets_relative_and_package(self):
        known = {"ameesh.ci", "ameesh.runner", "tests.support"}
        self.assertEqual(calls._import_targets(
            "from . import ci", "ameesh.runner", known), {"ameesh.ci"})
        self.assertEqual(calls._import_targets(
            "from .ci import run", "ameesh.runner", known), {"ameesh.ci"})
        self.assertEqual(calls._import_targets(
            "from ameesh import ci", "tests.test_a", known), {"ameesh.ci"})
        self.assertEqual(calls._import_targets(
            "from ameesh import ci as c", "tests.test_a", known), {"ameesh.ci"})
        self.assertEqual(calls._import_targets(
            "import ameesh.ci as c", "tests.test_a", known), {"ameesh.ci"})
        self.assertEqual(calls._import_targets(
            "from .support import PgTestCase", "tests.test_a", known),
            {"tests.support"})
        # un nom inconnu n'est pas un module du SHA : pas d'arête inventée
        self.assertEqual(calls._import_targets(
            "from ameesh import inconnu", "tests.test_a", known), set())

    def test_a_transitive_importer_comes_after_a_direct_one(self):
        repo = Repo()
        self.addCleanup(repo.cleanup)
        repo.write("src/ameesh/ci.py", "x = 1\n")
        repo.write("src/ameesh/helper.py", "from . import ci\n")
        repo.write("tests/test_direct.py", "from ameesh import ci\n")
        repo.write("tests/test_transitif.py", "from ameesh import helper\n")
        head = repo.commit("lot")
        prepared = SimpleNamespace(repo=repo.root, tree=repo.root)
        tree_modules = calls._tree_modules(prepared, head)
        chosen, skipped = calls._importers(
            prepared, head, [("M", "src/ameesh/ci.py")], tree_modules, set())
        self.assertEqual(chosen, ["test_direct", "test_transitif"])
        self.assertEqual(skipped, [])

    def test_the_per_root_quota_keeps_a_moderate_test(self):
        # Une coupe globale « plus court d'abord » prendrait les 48 modules
        # à 0,1 s puis laisserait test_2820 (6 s) hors d'un budget de 30 s.
        # Le quota par racine le garde (9e de housekeeping) ; la coupe
        # globale ne retire ensuite que les plus éloignés ou les plus longs.
        queues = {
            "ameesh.housekeeping": dict(
                [("test_court_%d" % i, 1) for i in range(8)]
                + [("test_2820_events_rotation", 1)]),
            "ameesh.runner": {"test_runner_%d" % i: 1 for i in range(20)},
            "ameesh.work": {"test_work_%d" % i: 1 for i in range(20)},
        }
        durations = {name: 60.0 for queue in queues.values()
                     for name in queue}
        for i in range(8):
            durations["test_court_%d" % i] = 0.1
        durations["test_2820_events_rotation"] = 6.0
        order, skipped = calls._budget_importers(
            queues, durations, set(), 30.0)
        self.assertIn("test_2820_events_rotation", order)
        self.assertLessEqual(len(order), 3 * calls.FALLBACK_IMPORTERS_PER_ROOT)
        self.assertTrue(skipped)
        self.assertEqual(skipped, sorted(set(skipped)))

    def test_the_global_cut_drops_the_farthest_first(self):
        # À budget serré, un direct coûteux passe avant un transitif bon
        # marché : la distance d'import prime sur la durée.
        queues = {
            "ameesh.ci": {"test_direct": 1},
            "ameesh.runner": {"test_transitif": 2, "test_profond": 3},
        }
        durations = {"test_direct": 6.0, "test_transitif": 0.1,
                     "test_profond": 0.1}
        order, skipped = calls._budget_importers(
            queues, durations, set(), 6.0)
        self.assertEqual(order, ["test_direct"])
        self.assertEqual(skipped, ["test_profond", "test_transitif"])

    def test_a_test_support_change_is_not_an_importer_root(self):
        repo = Repo()
        self.addCleanup(repo.cleanup)
        repo.write("tests/support.py", "x = 1\n")
        repo.write("tests/test_a.py", "from .support import x\n")
        head = repo.commit("lot")
        prepared = SimpleNamespace(repo=repo.root, tree=repo.root)
        tree_modules = calls._tree_modules(prepared, head)
        chosen, skipped = calls._importers(
            prepared, head, [("M", "tests/support.py")], tree_modules, set())
        self.assertEqual((chosen, skipped), ([], []))


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
