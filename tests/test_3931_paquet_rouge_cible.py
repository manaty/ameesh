# SPDX-License-Identifier: AGPL-3.0-only
"""#3931 — un test ciblé rouge à la composition retire l'unité fautive.

Témoins : la lecture d'une sortie unittest (noms pointés, `setUpClass`), la
décision #3801 (preuve forte, une seule unité au sommet, aucun échec sans
attribution), l'outil `scripts/paquet-attribution.py` sur une vraie carte
d'impact (une migration désigne son unité), et la composition elle-même :
deux unités, la seconde casse un test ciblé, le paquet se compose avec la
première et l'auteur de la seconde est prévenu. Aucune base, aucun réseau.
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
AUTO = ROOT / "scripts" / "paquet-auto.py"
HELPER = ROOT / "scripts" / "paquet-attribution.py"


def _load_script(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


paquet_auto = _load_script(AUTO, "test_paquet_auto_3931")


def red(output: str, label: str = "contrats",
        modules=("tests.test_cluster",)) -> "paquet_auto.TestRun":
    return paquet_auto.TestRun(label, list(modules), 1, output, 0.1)


def green(label: str = "contrats",
          modules=("tests.test_cluster",)) -> "paquet_auto.TestRun":
    return paquet_auto.TestRun(label, list(modules), 0,
                               "Ran 1 test in 0.1s\n\nOK", 0.1)


def attribution(*entries) -> dict:
    return {"status": "ok", "tests": list(entries), "card": {}}


def entry(test: str, *units) -> dict:
    return {
        "test": test,
        "status": "probable",
        "units": [
            {"lot": lot, "best": best,
             "evidence": [{"kind": "migration",
                           "file": "src/ameesh/migrations/0492_x.sql"}]}
            for lot, best in units
        ],
    }


class FailureReaderTest(unittest.TestCase):
    def test_fail_et_error_donnent_le_nom_pointe(self) -> None:
        text = (
            "======================================================================\n"
            "FAIL: test_x (tests.test_cluster.SuperviseurRoleTest.test_x) (i=1)\n"
            "AssertionError: rouge\n"
            "======================================================================\n"
            "ERROR: setUpClass (tests.test_unit.TestX)\n"
            "RuntimeError: boum\n"
            "FAIL: ligne écrite par le test, pas un en-tête\n"
        )
        self.assertEqual(
            paquet_auto.unittest_failures(text),
            [{"test": "tests.test_cluster.SuperviseurRoleTest.test_x",
              "module": "test_cluster"},
             {"test": "tests.test_unit.TestX", "module": "test_unit"}])

    def test_resume_non_lu_comme_un_echec(self) -> None:
        self.assertEqual(paquet_auto.unittest_failures("FAILED (failures=1)"), [])

    def test_erreur_d_environnement_ignoree(self) -> None:
        text = (
            "======================================================================\n"
            "FAIL: test_x (tests.test_cluster.T.test_x)\n"
            "AssertionError: rouge\n"
            "\n"
            "======================================================================\n"
            "ERROR: test_db (tests.test_unit.T.test_db)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "psycopg.OperationalError: connection refused\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "FAILED (failures=1, errors=1)\n"
        )
        self.assertEqual(
            paquet_auto.unittest_failures(text),
            [{"test": "tests.test_cluster.T.test_x", "module": "test_cluster"}])


class DesignationTest(unittest.TestCase):
    def test_une_preuve_forte_et_une_seule_unite(self) -> None:
        removals, why = paquet_auto.designated_removals(
            attribution(entry("tests.test_cluster.T.t", (2, 4), (1, 1))),
            [{"test": "tests.test_cluster.T.t"}])
        self.assertEqual([removal.lot for removal in removals], [2])
        self.assertTrue(removals[0].attributed)
        self.assertEqual(removals[0].tests, ["tests.test_cluster.T.t"])
        self.assertIn("migration", removals[0].evidence[0])
        self.assertEqual(why, "")

    def test_deux_unites_a_egalite_ne_retirent_rien(self) -> None:
        removals, why = paquet_auto.designated_removals(
            attribution(entry("tests.test_cluster.T.t", (2, 4), (1, 4))),
            [{"test": "tests.test_cluster.T.t"}])
        self.assertEqual(removals, [])
        self.assertIn("égalité", why)

    def test_preuve_faible_ne_retire_rien(self) -> None:
        removals, why = paquet_auto.designated_removals(
            attribution(entry("tests.test_cluster.T.t", (2, 2))),
            [{"test": "tests.test_cluster.T.t"}])
        self.assertEqual(removals, [])
        self.assertIn("preuve faible", why)

    def test_un_echec_non_attribuable_annule_tout(self) -> None:
        removals, why = paquet_auto.designated_removals(
            {"status": "ok", "tests": [
                entry("tests.test_cluster.T.t", (2, 4)),
                {"test": "tests.test_autre.T.t", "status": "unattributable",
                 "units": []}]},
            [{"test": "tests.test_cluster.T.t"},
             {"test": "tests.test_autre.T.t"}])
        self.assertEqual(removals, [])
        self.assertIn("non attribuable", why)

    def test_attribution_incomplete_ne_retire_rien(self) -> None:
        removals, why = paquet_auto.designated_removals(
            attribution(entry("tests.test_cluster.T.t", (2, 4))),
            [{"test": "tests.test_cluster.T.t"},
             {"test": "tests.test_autre.T.t"}])
        self.assertEqual(removals, [])
        self.assertIn("incomplète", why)

    def test_unite_hors_composition_refusee(self) -> None:
        removals, why = paquet_auto.designated_removals(
            attribution(entry("tests.test_cluster.T.t", (9, 6))),
            [{"test": "tests.test_cluster.T.t"}], known_lots={1, 2})
        self.assertEqual(removals, [])
        self.assertIn("hors de la composition", why)

    def test_entree_etrangere_ou_double_refusee(self) -> None:
        removals, why = paquet_auto.designated_removals(
            attribution(entry("tests.test_autre.T.t", (2, 6))),
            [{"test": "tests.test_cluster.T.t"}])
        self.assertEqual(removals, [])
        self.assertIn("hors des échecs", why)
        removals, why = paquet_auto.designated_removals(
            attribution(entry("tests.test_cluster.T.t", (2, 6)),
                        entry("tests.test_cluster.T.t", (2, 6))),
            [{"test": "tests.test_cluster.T.t"}])
        self.assertEqual(removals, [])
        self.assertIn("incomplète", why)


class HelperTest(unittest.TestCase):
    """L'outil réel lit une carte et désigne la migration de l'unité."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-attribution-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "banc")
        self.git("config", "user.email", "banc@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.unit1 = self.commit_on("lot/1", "docs/one.md", "un\n")
        self.unit2 = self.commit_on(
            "lot/2", "src/ameesh/migrations/0492_x.sql", "select 1;\n")
        self.state = pathlib.Path(self.tmp.name) / "state"

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", "-C", str(self.repo), *args],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def commit_on(self, branch: str, name: str, content: str) -> str:
        self.git("switch", "-q", "-c", branch, "main")
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", branch)
        return self.git("rev-parse", "HEAD")

    def card(self, *, dossiers=True, tables=True) -> pathlib.Path:
        path = self.state / "packet-red" / "carte-manaty-ameesh.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "schema": "ameesh-carte/1", "commit": "c" * 40,
            "genere": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "blocs": {}, "donnees": {},
            "dossiers": ({"src/ameesh/migrations": ["test_cluster"]}
                         if dossiers else {}),
            "tables": ({"agent_registry": ["test_cluster"]} if tables else {}),
        }
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            json.dump(body, stream)
        return path

    def run_helper(self, *, state: pathlib.Path | None = None) -> dict:
        request = {
            "schema": "ameesh-paquet-attribution-request/1",
            "units": [{"lot": 1, "unit_sha": self.unit1},
                      {"lot": 2, "unit_sha": self.unit2}],
            "failures": [{"test": "tests.test_cluster.T.test_x",
                          "module": "test_cluster"}],
        }
        proc = subprocess.run(
            [sys.executable, str(HELPER), "--repo", str(self.repo),
             "--slug", "manaty/ameesh",
             "--state-dir", str(state or self.state)],
            input=json.dumps(request), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_la_migration_designe_l_unite(self) -> None:
        self.card()
        out = self.run_helper()
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["slug"], "manaty/ameesh")
        first = out["tests"][0]
        self.assertEqual(first["status"], "probable")
        self.assertEqual([unit["lot"] for unit in first["units"]], [2])
        self.assertEqual([evidence["kind"]
                          for evidence in first["units"][0]["evidence"]],
                         ["migration"])

    def test_sans_carte_aucune_preuve_de_migration(self) -> None:
        out = self.run_helper()
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["card"]["status"], "absente")
        self.assertEqual(out["tests"][0]["status"], "unattributable")

    def test_carte_sans_table_ne_designe_pas_une_migration(self) -> None:
        self.card(tables=False)
        out = self.run_helper()
        self.assertEqual(out["tests"][0]["status"], "unattributable")


class RealCompositionTest(unittest.TestCase):
    """Le vrai `check_local_tests`, le vrai helper et une carte d'impact."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-3931-reel-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "worktree"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "banc")
        self.git("config", "user.email", "banc@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("remote", "add", "origin",
                 "https://github.com/manaty/ameesh.git")
        self.write("scripts/paquet-attribution.py",
                   HELPER.read_text(encoding="utf-8"))
        self.write("tests/parts/toujours.txt", "test_cluster\n")
        self.write("tests/test_l161f_plan.py", "# vert\n")
        self.write("tests/test_cluster.py",
                   "import unittest\n"
                   "\n"
                   "class T(unittest.TestCase):\n"
                   "    def test_x(self):\n"
                   "        self.fail('colonne non classée')\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.unit1 = self.commit_on("lot/1", "docs/one.md", "un\n")
        self.unit2 = self.commit_on(
            "lot/2", "src/ameesh/migrations/0492_x.sql", "select 1;\n")
        # Le paquet importe le projet depuis son arbre ; le lien n'est pas
        # commité (il n'appartient à aucune unité).
        (self.repo / "src").mkdir()
        (self.repo / "src" / "ameesh").symlink_to(
            ROOT / "src" / "ameesh", target_is_directory=True)
        self.state = pathlib.Path(self.tmp.name) / "state"
        self.write_card()
        self.composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, self.unit1),
                   paquet_auto.Unit(2, "c" * 40, self.unit2)],
            discarded=[], head=self.base)

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", "-C", str(self.repo), *args],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def write(self, name: str, content: str) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit_on(self, branch: str, name: str, content: str) -> str:
        self.git("switch", "-q", "-c", branch, "main")
        self.write(name, content)
        self.git("add", "-A")
        self.git("commit", "-qm", branch)
        sha = self.git("rev-parse", "HEAD")
        self.git("switch", "-q", "main")
        return sha

    def write_card(self) -> None:
        path = self.state / "packet-red" / "carte-manaty-ameesh.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "schema": "ameesh-carte/1", "commit": "c" * 40,
            "genere": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "blocs": {}, "donnees": {},
            "dossiers": {"src/ameesh/migrations": ["test_cluster"]},
            "tables": {"agent_registry": ["test_cluster"]},
        }
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            json.dump(body, stream)

    def test_un_contrat_rouge_reel_designe_l_unite_et_l_autre_reste(self) -> None:
        with mock.patch.object(paquet_auto, "project_python",
                               return_value=sys.executable), \
                mock.patch.dict(os.environ,
                                {"AGENT_MESH_STATE": str(self.state)}):
            runs, faulty, removals = paquet_auto.check_local_tests(
                self.repo, self.composition, timeout=60,
                log=lambda _line: None)
        self.assertEqual([run.label for run in runs], ["contrats"])
        self.assertEqual(faulty, [2])
        self.assertEqual([removal.lot for removal in removals], [2])
        self.assertTrue(removals[0].attributed)
        self.assertEqual(removals[0].evidence,
                         ["migration src/ameesh/migrations/0492_x.sql"])
        remaining = [unit.lot for unit in self.composition.units
                     if unit.lot not in set(faulty)]
        self.assertEqual(remaining, [1])


class AttributeCompositionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-3931-attr-")
        self.addCleanup(self.tmp.cleanup)
        self.worktree = pathlib.Path(self.tmp.name)
        (self.worktree / "scripts").mkdir()
        (self.worktree / "scripts" / "paquet-attribution.py").write_text(
            "", encoding="utf-8")
        self.composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(2, "c" * 40, "d" * 40)],
            discarded=[], head="e" * 40)

    def test_le_helper_recoit_les_unites_et_les_echecs(self) -> None:
        report = {"status": "ok", "tests": [{"test": "t", "status": "probable",
                                             "units": []}]}
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(report).encode(),
            stderr=b"")
        with mock.patch.object(paquet_auto, "project_python",
                               return_value="python3"), \
                mock.patch.object(paquet_auto, "run_command",
                                  return_value=completed) as run:
            got = paquet_auto.attribute_composition(
                self.worktree, self.composition,
                [{"test": "tests.test_cluster.T.t", "module": "test_cluster"}])
        self.assertEqual(got, report)
        payload = json.loads(run.call_args.kwargs["input_text"])
        self.assertEqual(payload["units"],
                         [{"lot": 2, "unit_sha": "d" * 40}])
        self.assertEqual(payload["failures"][0]["module"], "test_cluster")

    def test_un_helper_en_echec_ne_conclut_pas(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b"", stderr=b"carte illisible")
        with mock.patch.object(paquet_auto, "project_python",
                               return_value="python3"), \
                mock.patch.object(paquet_auto, "run_command",
                                  return_value=completed):
            got = paquet_auto.attribute_composition(
                self.worktree, self.composition,
                [{"test": "t", "module": "test_cluster"}])
        self.assertEqual(got["status"], "error")
        self.assertIn("carte illisible", got["why"])


class LocalTestCompositionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-3931-")
        self.addCleanup(self.tmp.cleanup)
        self.worktree = pathlib.Path(self.tmp.name)
        self.composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "b" * 40),
                   paquet_auto.Unit(2, "c" * 40, "d" * 40)],
            discarded=[], head="e" * 40)

    def check(self, *, unit_runs=(), contract_run=None, attribute=None,
              unit_modules=None, contract_module_list=("tests.test_cluster",)):
        # Les unités sans module propre ne produisent aucun run : le témoin
        # contracts-only reste lisible.
        modules = unit_modules or {}
        side = list(unit_runs) + ([contract_run] if contract_run else [])

        def unit_modules_for(_worktree, unit):
            return modules.get(unit.lot, [])

        with mock.patch.object(paquet_auto, "unit_test_modules",
                               side_effect=unit_modules_for), \
                mock.patch.object(paquet_auto, "contract_modules",
                                  return_value=list(contract_module_list)), \
                mock.patch.object(paquet_auto, "run_tests",
                                  side_effect=side):
            return paquet_auto.check_local_tests(
                self.worktree, self.composition, timeout=1,
                log=lambda _line: None, attribute=attribute)

    def test_contrat_rouge_attribue_retire_l_unite_et_garde_l_autre(self) -> None:
        self.assertEqual(self.composition.units[1].lot, 2)
        red_contract = red(
            "FAIL: test_x (tests.test_cluster.SuperviseurRoleTest.test_x)\n"
            "AssertionError: colonne non classée\n")

        def attribute(_worktree, _composition, failures):
            return attribution(entry(failures[0]["test"], (2, 4), (1, 1)))

        runs, faulty, removals = self.check(
            contract_run=red_contract, attribute=attribute)
        self.assertEqual([run.label for run in runs], ["contrats"])
        self.assertEqual(faulty, [2])
        self.assertEqual([removal.lot for removal in removals], [2])
        self.assertTrue(removals[0].attributed)
        # La recomposition garde l'autre unité : c'est le paquet composé.
        remaining = [unit.lot for unit in self.composition.units
                     if unit.lot not in set(faulty)]
        self.assertEqual(remaining, [1])

    def test_contrat_rouge_non_attribuable_garde_l_echec_actuel(self) -> None:
        red_contract = red(
            "FAIL: test_x (tests.test_cluster.SuperviseurRoleTest.test_x)\n"
            "AssertionError: x\n")

        def attribute(_worktree, _composition, _failures):
            return {"status": "ok", "tests": [
                {"test": "tests.test_cluster.SuperviseurRoleTest.test_x",
                 "status": "unattributable", "units": []}]}

        with self.assertRaises(paquet_auto.PaquetAutoError) as caught:
            self.check(contract_run=red_contract, attribute=attribute)
        self.assertIn("contrat rouges", str(caught.exception))

    def mixed_run(self) -> "paquet_auto.TestRun":
        return red(
            "======================================================================\n"
            "FAIL: test_a (tests.test_a.T.test_a)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "AssertionError: rouge réel de A\n"
            "\n"
            "======================================================================\n"
            "ERROR: test_b (tests.test_b.T.test_b)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "psycopg.OperationalError: connection refused\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "Ran 2 tests in 0.1s\n"
            "FAILED (failures=1, errors=1)\n")

    def test_une_error_infra_dans_un_run_mixte_ne_retire_que_le_fail(self) -> None:
        # #3931, B/P2 de la relecture cx2 : B n'a qu'une ERROR d'environnement,
        # son test est sain ; l'attribution ne doit voir que le FAIL de A.
        seen: list[dict] = []

        def attribute(_worktree, _composition, failures):
            seen.extend(failures)
            return attribution(entry(failures[0]["test"], (1, 6)))

        _runs, faulty, removals = self.check(
            contract_run=self.mixed_run(), attribute=attribute,
            contract_module_list=("tests.test_a", "tests.test_b"))
        self.assertEqual([failure["test"] for failure in seen],
                         ["tests.test_a.T.test_a"])
        self.assertEqual(faulty, [1])
        self.assertEqual([removal.lot for removal in removals], [1])
        remaining = [unit.lot for unit in self.composition.units
                     if unit.lot not in set(faulty)]
        self.assertEqual(remaining, [2])

    def test_error_infra_seule_est_non_concluante(self) -> None:
        env_only = paquet_auto.TestRun(
            "contrats", ["tests.test_b"], 1,
            "======================================================================\n"
            "ERROR: test_b (tests.test_b.T.test_b)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "psycopg.OperationalError: connection refused\n"
            "\n"
            "FAILED (errors=1)\n", 0.1)
        self.assertTrue(env_only.inconclusive)
        _runs, faulty, removals = self.check(
            contract_run=env_only, contract_module_list=("tests.test_b",))
        self.assertEqual((faulty, removals), ([], []))

    def test_run_mixte_fail_non_attribuable_ne_retire_pas(self) -> None:
        def attribute(_worktree, _composition, failures):
            return {"status": "ok", "tests": [
                {"test": failures[0]["test"], "status": "unattributable",
                 "units": []}]}

        with self.assertRaises(paquet_auto.PaquetAutoError):
            self.check(contract_run=self.mixed_run(), attribute=attribute,
                       contract_module_list=("tests.test_a", "tests.test_b"))

    def test_unite_propre_rouge_sans_attribution_est_retiree(self) -> None:
        unit_red = red(
            "FAIL: test_x (tests.test_unit.TestX)\nAssertionError", "unité #1",
            ("tests.test_unit",))
        red_contract = red(
            "FAIL: test_x (tests.test_cluster.T.test_x)\nAssertionError")

        def attribute(_worktree, _composition, _failures):
            return {"status": "error", "tests": [], "why": "helper absent"}

        runs, faulty, removals = self.check(
            unit_runs=[unit_red], contract_run=red_contract,
            attribute=attribute, unit_modules={1: ["tests.test_unit"]})
        # Le contrat rouge non attribuable ne bloque pas : l'unité au test
        # propre rouge est retirée et le paquet recompose (geste antérieur).
        self.assertEqual([run.label for run in runs], ["unité #1", "contrats"])
        self.assertEqual(faulty, [1])
        self.assertFalse(removals[0].attributed)

    def test_attribution_d_une_autre_unite_est_suivie(self) -> None:
        red_contract = red(
            "FAIL: test_x (tests.test_cluster.T.test_x)\nAssertionError")

        def attribute(_worktree, _composition, failures):
            return attribution(entry(failures[0]["test"], (2, 6)))

        _runs, faulty, removals = self.check(
            contract_run=red_contract, attribute=attribute)
        self.assertEqual(faulty, [2])
        self.assertEqual(removals[0].evidence[0],
                         "migration src/ameesh/migrations/0492_x.sql")

    def test_contrat_non_concluant_ne_retire_rien(self) -> None:
        inconclusive = paquet_auto.TestRun(
            "contrats", ["tests.test_cluster"], None,
            "did not finish in 1 s", 1.0, timed_out=True)
        _runs, faulty, removals = self.check(contract_run=inconclusive)
        self.assertEqual((faulty, removals), ([], []))


class AuthorMailTest(unittest.TestCase):
    def test_l_auteur_du_lot_est_prevenu(self) -> None:
        removal = paquet_auto.Removal(
            3885, tests=["tests.test_cluster.T.test_x"],
            evidence=["migration src/ameesh/migrations/0492_x.sql"],
            attributed=True)
        with mock.patch.object(paquet_auto, "lot_author",
                               return_value="mty-amh-impl-06"), \
                mock.patch.object(paquet_auto, "send_mail") as send:
            failed = paquet_auto.notify_removals(
                pathlib.Path("."), "h-20261027-36", [removal],
                sender="mesh-design", log=lambda _line: None)
        self.assertEqual(failed, [])
        recipient, lot, body, sender = send.call_args.args
        self.assertEqual((recipient, lot), ("mty-amh-impl-06", 3885))
        self.assertIn("retiré du paquet h-20261027-36", body)
        self.assertIn("0492_x.sql", body)
        self.assertIn("la lecture #3801 désigne ton unité", body)
        self.assertEqual(sender, "mesh-design")

    def test_un_retrait_de_repli_ne_revendique_pas_3801(self) -> None:
        # #3942 : sans attribution sûre, le courrier nomme le module propre
        # rouge et ne prétend pas que la lecture #3801 désigne l'unité.
        removal = paquet_auto.Removal(
            3885, tests=["tests.test_cluster.T.test_x"], attributed=False)
        with mock.patch.object(paquet_auto, "lot_author",
                               return_value="mty-amh-impl-06"), \
                mock.patch.object(paquet_auto, "send_mail") as send:
            failed = paquet_auto.notify_removals(
                pathlib.Path("."), "h-20261027-36", [removal],
                sender="mesh-design", log=lambda _line: None)
        self.assertEqual(failed, [])
        _recipient, _lot, body, _sender = send.call_args.args
        self.assertIn("module propre de ton unité est rouge", body)
        self.assertIn("sans attribution #3801 sûre", body)
        self.assertNotIn("la lecture #3801 désigne ton unité", body)
        self.assertIn("tests.test_cluster.T.test_x", body)

    def test_un_courrier_qui_echoue_ne_bloque_pas_la_recomposition(self) -> None:
        removal = paquet_auto.Removal(2, tests=["t"], attributed=True)
        with mock.patch.object(paquet_auto, "lot_author", return_value="auteur"), \
                mock.patch.object(paquet_auto, "send_mail",
                                  side_effect=paquet_auto.PaquetAutoError("refus")):
            failed = paquet_auto.notify_removals(
                pathlib.Path("."), "h", [removal], sender=None,
                log=lambda _line: None)
        self.assertEqual(len(failed), 1)
        self.assertIn("#2", failed[0])


class ComposePacketTest(unittest.TestCase):
    """Le flux complet : deux unités, la seconde rouge, l'autre est composée."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-3931-flux-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.worktree = pathlib.Path(self.tmp.name) / "wt"
        (self.worktree / "scripts").mkdir(parents=True)
        (self.worktree / "scripts" / "paquet-compose.py").write_text("")
        self.two = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "b" * 40),
                   paquet_auto.Unit(2, "c" * 40, "d" * 40)],
            discarded=[], head="e" * 40)
        self.one = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "b" * 40)],
            discarded=[], head="f" * 40)

    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            repo=str(self.repo), name="h-20261027-36", target="main",
            carrier=3929, base=None, local=False, no_packet_record=True,
            keep_worktree=False, keep_failed_hours=24.0, force=False,
            tests_timeout=10, pilot="mesh-design", sender="mesh-design")

    def test_le_paquet_se_compose_avec_l_autre_unite(self) -> None:
        red_contract = red(
            "FAIL: test_x (tests.test_cluster.SuperviseurRoleTest.test_x)\n"
            "AssertionError: x\n")
        removal = paquet_auto.Removal(
            2, tests=["tests.test_cluster.SuperviseurRoleTest.test_x"],
            evidence=["migration src/ameesh/migrations/0492_x.sql"],
            attributed=True)
        manifest = mock.Mock(return_value="manifeste")
        with mock.patch.object(paquet_auto, "fresh_base", return_value="a" * 40), \
                mock.patch.object(paquet_auto, "prune_stale_worktrees",
                                  return_value=[]), \
                mock.patch.object(paquet_auto, "make_worktree",
                                  return_value=self.worktree), \
                mock.patch.object(
                    paquet_auto, "compute_candidates",
                    return_value=([paquet_auto.Candidate(1, "a" * 40),
                                   paquet_auto.Candidate(2, "c" * 40)],
                                  {"ready": [1, 2], "waiting": []})), \
                mock.patch.object(paquet_auto, "compose_candidates",
                                  return_value=self.two), \
                mock.patch.object(paquet_auto, "run_freeze_checks",
                                  return_value=(0, "ok", 0.1)), \
                mock.patch.object(
                    paquet_auto, "check_local_tests",
                    side_effect=[([red_contract], [2], [removal]),
                                 ([green()], [], [])]), \
                mock.patch.object(paquet_auto, "recompose",
                                  return_value=self.one) as recompose, \
                mock.patch.object(paquet_auto, "notify_removals",
                                  return_value=[]) as notify, \
                mock.patch.object(paquet_auto, "composition_table",
                                  return_value="## Composition\n"), \
                mock.patch.object(paquet_auto, "build_manifest", manifest), \
                mock.patch.object(paquet_auto, "write_manifest",
                                  return_value="d" * 40), \
                mock.patch.object(paquet_auto, "run_inclusion",
                                  return_value=(0, "inclusion ok")), \
                mock.patch.object(paquet_auto, "push_branch",
                                  return_value="poussée"), \
                mock.patch.object(paquet_auto, "ensure_pull_request",
                                  return_value=(2987, "http://pr")), \
                mock.patch.object(paquet_auto, "freeze_carrier",
                                  return_value="gel"), \
                mock.patch.multiple(paquet_auto,
                                    request_full_ci=mock.Mock(return_value="ci-full"),
                                    note_carrier=mock.Mock(return_value="note"),
                                    pull_request_state=mock.Mock(return_value="OPEN")), \
                mock.patch.object(paquet_auto, "send_mail",
                                  return_value="envoyé"), \
                mock.patch.object(paquet_auto, "remove_worktree"):
            self.assertEqual(paquet_auto.compose_packet(self.args()), 0)
        candidates = recompose.call_args.args[2]
        self.assertEqual([candidate.lot for candidate in candidates], [1])
        self.assertEqual(
            [unit.lot for unit in manifest.call_args.kwargs["composition"].units],
            [1])
        self.assertEqual(notify.call_args.args[2], [removal])
        reasons = [item.reason
                   for item in manifest.call_args.kwargs["composition"].discarded]
        self.assertTrue(any("attribués à son unité" in reason
                            for reason in reasons), reasons)


if __name__ == "__main__":
    unittest.main()
