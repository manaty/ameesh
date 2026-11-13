# SPDX-License-Identifier: AGPL-3.0-only
"""#7239 — un retrait #3801 est confirmé sur le préfixe de son unité.

Paquet h-20261113-11 : la couverture (#3801) désignait #7066 (unité 1) pour
un contrat `test_cluster` que seul #7108 (unité 2) cassait, et #7229 (unité 3)
pour un test TLS rouge sous charge qui passait au rejeu. Avant un retrait
#3801, les tests rouges sont désormais rejoués sur le préfixe qui s'arrête à
l'unité désignée, puis sur les préfixes suivants : vert au préfixe de l'unité
= innocente (la première unité dont le préfixe le rend rouge est retirée à sa
place), vert partout = instable (#4221), rouge dès son préfixe = retrait
confirmé. Un rejeu non concluant garde le retrait antérieur. Aucune base,
aucun réseau ; rejeu simulé sur un vrai empilement git.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
AUTO = ROOT / "scripts" / "paquet-auto.py"


def _load_script(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


paquet_auto = _load_script(AUTO, "test_paquet_auto_7239")

TARGET = "tests.test_own_a.T.test_value"


def _red(label: str, modules, test: str = TARGET) -> "paquet_auto.TestRun":
    return paquet_auto.TestRun(
        label, list(modules), 1,
        "FAIL: test_value (%s)\nAssertionError: 2 != 1\n" % test, 0.1)


def _green(label: str, modules) -> "paquet_auto.TestRun":
    return paquet_auto.TestRun(label, list(modules), 0,
                               "Ran 1 test in 0.1s\n\nOK", 0.1)


def _timed_out(label: str, modules) -> "paquet_auto.TestRun":
    return paquet_auto.TestRun(label, list(modules), None,
                               "did not finish in 1 s", 1.0, timed_out=True)


class StackCase(unittest.TestCase):
    """Un empilement git réel : base, unité a, unité b, unité c."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-7239-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "banc")
        self.git("config", "user.email", "banc@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write("VALUE.txt", "1\n")
        self.write("tests/__init__.py", "")
        self.write("tests/test_own_a.py",
                   "import pathlib\n"
                   "import unittest\n"
                   "\n"
                   "class T(unittest.TestCase):\n"
                   "    def test_value(self):\n"
                   "        root = pathlib.Path(__file__).resolve().parents[1]\n"
                   "        self.assertEqual(\n"
                   "            (root / 'VALUE.txt').read_text().strip(), '1')\n")
        self.base = self.commit("base")
        self.unit_a = self.commit_unit("a", "docs/a.md", "a\n")
        self.unit_b = self.commit_unit("b", "VALUE.txt", "2\n")
        self.unit_c = self.commit_unit("c", "docs/c.md", "c\n")
        self.composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, self.unit_a),
                   paquet_auto.Unit(2, "b" * 40, self.unit_b),
                   paquet_auto.Unit(3, "c" * 40, self.unit_c)],
            discarded=[], head=self.unit_c)

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

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def commit_unit(self, name: str, path: str, content: str) -> str:
        self.write(path, content)
        return self.commit("unité %s" % name)


class FakePrefix:
    """Le rejeu des préfixes, simulé : `red_at` donne le premier rouge."""

    def __init__(self, red_at: dict[str, int], why: str = ""):
        self.red_at = dict(red_at)
        self.why = why
        self.calls: list[tuple[int, list[str]]] = []

    def __call__(self, _worktree, composition, failures, *, timeout, start=1,
                 log=None, **_kwargs):
        self.calls.append((start, [failure["test"] for failure in failures]))
        if self.why:
            return {}, self.why, []
        unresolved = list(failures)
        found: dict[int, list[dict]] = {}
        for index in range(start, len(composition.units) + 1):
            unit = composition.units[index - 1]
            red = [failure for failure in unresolved
                   if self.red_at.get(failure["test"]) == index]
            if red:
                found.setdefault(unit.lot, []).extend(red)
                unresolved = [failure for failure in unresolved
                              if failure not in red]
            if not unresolved:
                break
        return found, "", unresolved


class DesignatedPrefixTest(StackCase):
    """`designated_prefix` : confirmer, innocenter ou consigner l'instable."""

    def removal(self, lot: int = 1, tests=(TARGET,),
                proof: str = "3801") -> "paquet_auto.Removal":
        return paquet_auto.Removal(
            lot=lot, tests=list(tests), attributed=True, proof=proof,
            evidence=["test tests/test_own_a.py"])

    def run_designated(self, removals, prefix):
        return paquet_auto.designated_prefix(
            self.repo, self.composition, removals, timeout=5, prefix=prefix,
            log=lambda _line: None)

    def test_unite_en_tete_innocente_laisse_la_fautive_suivante(self) -> None:
        # Témoin #7066/#7108 : l'unité 1 est couverte, mais le rouge n'apparaît
        # qu'au préfixe de l'unité 2 ; c'est elle qui est retirée.
        prefix = FakePrefix({TARGET: 2})
        removals, flaky, why = self.run_designated([self.removal(1)], prefix)
        self.assertEqual((flaky, why), ([], ""))
        self.assertEqual([removal.lot for removal in removals], [2])
        self.assertEqual(removals[0].proof, "prefixes")
        self.assertEqual(removals[0].tests, [TARGET])
        self.assertEqual(prefix.calls, [(1, [TARGET])])

    def test_vert_partout_est_instable_sans_retrait(self) -> None:
        # Témoin #7229 : rouge sous charge sur la tête, vert sur tous les
        # préfixes ; consigné non concluant, aucun retrait.
        prefix = FakePrefix({})
        removals, flaky, why = self.run_designated([self.removal(1)], prefix)
        self.assertEqual(removals, [])
        self.assertEqual(why, "")
        self.assertEqual([failure["test"] for failure in flaky], [TARGET])
        self.assertEqual(prefix.calls, [(1, [TARGET])])

    def test_rouge_au_prefixe_de_l_unite_confirme_le_retrait(self) -> None:
        prefix = FakePrefix({TARGET: 1})
        removals, flaky, why = self.run_designated([self.removal(1)], prefix)
        self.assertEqual((flaky, why), ([], ""))
        self.assertEqual([removal.lot for removal in removals], [1])
        self.assertEqual(removals[0].proof, "3801")
        self.assertEqual(removals[0].evidence, ["test tests/test_own_a.py"])

    def test_rejeu_non_concluant_garde_le_retrait_anterieur(self) -> None:
        prefix = FakePrefix({}, why="worktree de préfixe impossible")
        removals, flaky, why = self.run_designated([self.removal(1)], prefix)
        self.assertEqual([removal.lot for removal in removals], [1])
        self.assertEqual(removals[0].proof, "3801")
        self.assertEqual((flaky, why), ([], "worktree de préfixe impossible"))

    def test_le_rejeu_commence_au_prefixe_de_l_unite_designee(self) -> None:
        # Une unité en position 3 ne rejoue pas les préfixes 1 et 2.
        prefix = FakePrefix({TARGET: 3})
        removals, flaky, why = self.run_designated([self.removal(3)], prefix)
        self.assertEqual((flaky, why), ([], ""))
        self.assertEqual([removal.lot for removal in removals], [3])
        self.assertEqual(prefix.calls, [(3, [TARGET])])

    def test_un_retrait_d_une_autre_preuve_n_est_pas_rejoue(self) -> None:
        def jamais(*_args, **_kwargs):
            raise AssertionError("aucun rejeu attendu")

        removal = self.removal(1, proof="prefixes")
        removals, flaky, why = self.run_designated([removal], jamais)
        self.assertEqual(removals, [removal])
        self.assertEqual((flaky, why), ([], ""))

    def test_deux_retraits_confirmes_chacun_sur_leur_prefixe(self) -> None:
        other = "tests.test_own_c.T.test_c"
        prefix = FakePrefix({TARGET: 1, other: 3})
        removals, flaky, why = self.run_designated(
            [self.removal(1), self.removal(3, tests=[other])], prefix)
        self.assertEqual((flaky, why), ([], ""))
        self.assertEqual([removal.lot for removal in removals], [1, 3])
        self.assertEqual([start for start, _tests in prefix.calls], [1, 3])

    def test_un_test_confirme_et_un_instable_dans_le_meme_retrait(self) -> None:
        other = "tests.test_own_a.T.test_other"
        prefix = FakePrefix({TARGET: 1})
        removals, flaky, why = self.run_designated(
            [self.removal(1, tests=[TARGET, other])], prefix)
        self.assertEqual(why, "")
        self.assertEqual([removal.lot for removal in removals], [1])
        self.assertEqual(removals[0].tests, [TARGET])
        self.assertEqual([failure["test"] for failure in flaky], [other])

    def test_deux_tests_situes_plus_loin_designent_deux_unites(self) -> None:
        other = "tests.test_own_a.T.test_other"
        prefix = FakePrefix({TARGET: 2, other: 3})
        removals, flaky, why = self.run_designated(
            [self.removal(1, tests=[TARGET, other])], prefix)
        self.assertEqual((flaky, why), ([], ""))
        self.assertEqual([(removal.lot, removal.tests) for removal in removals],
                         [(2, [TARGET]), (3, [other])])

    def test_budget_epuise_garde_le_retrait_anterieur(self) -> None:
        def budget(*_args, **_kwargs):
            raise paquet_auto.BudgetSpent("borne totale atteinte")

        removals, flaky, why = self.run_designated([self.removal(1)], budget)
        self.assertEqual([removal.lot for removal in removals], [1])
        self.assertEqual(removals[0].proof, "3801")
        self.assertIn("borne totale", why)
        self.assertEqual(flaky, [])


class PrefixStartTest(StackCase):
    """Le vrai `prefix_failures(start=…)` ne rejoue pas avant l'unité."""

    def test_start_2_ne_rejoue_que_les_prefixes_suivants(self) -> None:
        calls = []

        def replay(_worktree, label, modules, *, timeout):
            calls.append((label, list(modules)))
            return _red(label, modules) if label == "préfixe 2 (#2)" \
                else _green(label, modules)

        found, why, flaky = paquet_auto.prefix_failures(
            self.repo, self.composition, [{"test": TARGET}], timeout=1,
            start=2, replay=replay, log=lambda _line: None)
        self.assertEqual((why, flaky), ("", []))
        self.assertEqual(list(found), [2])
        self.assertEqual(calls, [("préfixe 2 (#2)", [TARGET])])

    def test_start_3_vert_partout_est_instable(self) -> None:
        calls = []

        def replay(_worktree, label, modules, *, timeout):
            calls.append(label)
            return _green(label, modules)

        found, why, flaky = paquet_auto.prefix_failures(
            self.repo, self.composition, [{"test": TARGET}], timeout=1,
            start=3, replay=replay, log=lambda _line: None)
        self.assertEqual((found, why), ({}, ""))
        self.assertEqual([failure["test"] for failure in flaky], [TARGET])
        self.assertEqual(calls, ["préfixe 3 (#3)"])


class CheckLocalTestsTest(StackCase):
    """Le vrai `check_local_tests` : l'unité innocente n'est plus retirée."""

    def setUp(self) -> None:
        super().setUp()
        # L'arbre composé est à la tête (l'unité 2 casse le test).
        self.git("checkout", "-q", self.unit_c)

    def check(self, *, run_tests=None, attribute=None):
        own = {1: ["tests.test_own_a"], 2: []}
        attribution = attribute or (lambda _worktree, _composition, failures: {
            "status": "ok", "tests": [{
                "test": failures[0]["test"], "status": "probable",
                "units": [{"lot": 1, "best": 9,
                           "evidence": [{"kind": "test",
                                         "file": "tests/test_own_a.py"}]}]}]})
        patches = [
            mock.patch.object(paquet_auto, "project_python",
                              return_value=sys.executable),
            mock.patch.object(paquet_auto, "unit_test_modules",
                              side_effect=lambda _w, unit: own.get(unit.lot, [])),
            mock.patch.object(paquet_auto, "contract_modules",
                              return_value=[]),
        ]
        if run_tests is not None:
            patches.append(mock.patch.object(paquet_auto, "run_tests",
                                             side_effect=run_tests))
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            return paquet_auto.check_local_tests(
                self.repo, self.composition, timeout=30, base_sha=self.base,
                select=lambda *_a: ({}, ""), attribute=attribution,
                parallel=1, log=lambda _line: None)

    def test_unite_en_tete_innocente_devant_la_fautive(self) -> None:
        # Rejeu réel : vert sur le préfixe de l'unité 1, rouge sur celui de
        # l'unité 2. La couverture désignait l'unité 1 ; c'est la 2 qui part.
        runs, faulty, removals = self.check()
        self.assertEqual(faulty, [2])
        self.assertEqual([removal.lot for removal in removals], [2])
        self.assertEqual(removals[0].proof, "prefixes")
        self.assertIn("préfixe base+u1", removals[0].evidence[0])
        self.assertNotIn(1, faulty)

    def test_rouge_sous_charge_vert_au_rejeu_est_instable(self) -> None:
        # Témoin #7229 : le run de l'unité est rouge sur la tête, mais le
        # rejeu des préfixes est vert ; l'unité n'est pas retirée et l'instable
        # est consigné non concluant.
        def fake_run(_worktree, label, modules, **_kwargs):
            if label == "unité #1":
                return _red(label, modules)
            return _green(label, modules)

        runs, faulty, removals = self.check(run_tests=fake_run)
        self.assertEqual((faulty, removals), ([], []))
        self.assertEqual(runs[0].flaky_tests, [TARGET])
        self.assertTrue(runs[0].inconclusive)
        self.assertFalse(runs[0].real_failure)

    def test_rejeu_non_concluant_garde_le_retrait_3801(self) -> None:
        def fake_run(_worktree, label, modules, **_kwargs):
            if label == "unité #1":
                return _red(label, modules)
            if label.startswith("préfixe"):
                return _timed_out(label, modules)
            return _green(label, modules)

        runs, faulty, removals = self.check(run_tests=fake_run)
        self.assertEqual(faulty, [1])
        self.assertEqual([removal.lot for removal in removals], [1])
        self.assertEqual(removals[0].proof, "3801")
        self.assertFalse(runs[0].inconclusive)


if __name__ == "__main__":
    unittest.main()
