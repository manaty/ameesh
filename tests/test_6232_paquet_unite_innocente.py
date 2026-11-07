# SPDX-License-Identifier: AGPL-3.0-only
"""#6232 — une unité innocente n'est pas retirée avec la fautive antérieure.

Paquet h-20261107-28 (#6230) : #6183 casse `paquet-horaire.py`, les tests
propres de #6228, empilée après, rougissent aussi sur la tête ; le repli
retirait les deux. Le rouge de #6228 est désormais rejoué sur le préfixe qui
s'arrête à #6183 : rouge là aussi, #6228 est gardée et n'est pas marquée
`packet-unit-red`. Un rejeu vert ou non concluant garde le retrait. Aucune
base, aucun réseau ; rejeu simulé sur un vrai empilement git.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
AUTO = ROOT / "scripts" / "paquet-auto.py"


def _load_script(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


paquet_auto = _load_script(AUTO, "test_paquet_auto_6232")

TEST_A = "tests.test_own_a.T.test_ok"
TEST_B = "tests.test_own_b.T.test_ok"


def _red(label, modules, tests=(TEST_B,)) -> "paquet_auto.TestRun":
    return paquet_auto.TestRun(
        label, list(modules), 1,
        "".join("FAIL: test_ok (%s)\nAssertionError: x\n" % test
                for test in tests), 0.1)


def _green(label, modules) -> "paquet_auto.TestRun":
    return paquet_auto.TestRun(label, list(modules), 0,
                               "Ran 1 test in 0.1s\n\nOK", 0.1)


class StackCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-6232-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "banc")
        self.git("config", "user.email", "banc@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write("tests/__init__.py", "")
        self.base = self.commit("base")
        self.unit_a = self.commit_unit("a")
        self.unit_b = self.commit_unit("b")
        self.composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, self.unit_a),
                   paquet_auto.Unit(2, "b" * 40, self.unit_b)],
            discarded=[], head=self.unit_b)

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

    def commit_unit(self, name: str) -> str:
        self.write("tests/test_own_%s.py" % name,
                   "import unittest\n\nclass T(unittest.TestCase):\n"
                   "    def test_ok(self):\n        self.assertTrue(True)\n")
        return self.commit("unité %s" % name)


class EarlierRedTest(StackCase):
    """`earlier_red` sur l'empilement A (fautive), B (rouge hérité)."""

    def removals(self):
        return [paquet_auto.Removal(lot=1, tests=[TEST_A]),
                paquet_auto.Removal(lot=2, tests=[TEST_B])]

    def earlier(self, replay):
        calls = []

        def recorded(path, label, modules, *, timeout):
            calls.append((label, list(modules),
                          self.git("-C", str(path), "rev-parse", "HEAD")))
            return replay(label, modules)

        found = paquet_auto.earlier_red(
            self.repo, self.composition, self.removals(), timeout=5,
            replay=recorded, log=lambda _line: None)
        return found, calls

    def test_rouge_deja_sur_le_prefixe_anterieur_innocente(self) -> None:
        found, calls = self.earlier(lambda label, modules: _red(label, modules))
        self.assertEqual(found, {2: 1})
        # Un seul rejeu : les tests de B, sur le préfixe qui s'arrête à A.
        self.assertEqual(calls, [("préfixe de #1 (rouge de #2)", [TEST_B],
                                  self.unit_a)])

    def test_vert_sur_le_prefixe_garde_le_retrait(self) -> None:
        found, _calls = self.earlier(
            lambda label, modules: _green(label, modules))
        self.assertEqual(found, {})

    def test_non_concluant_garde_le_retrait(self) -> None:
        def timed_out(label, modules):
            return paquet_auto.TestRun(label, list(modules), None, "", 5.0,
                                       timed_out=True)

        found, _calls = self.earlier(timed_out)
        self.assertEqual(found, {})

    def test_rouge_en_partie_garde_le_retrait(self) -> None:
        other = "tests.test_own_b.T.test_other"
        removals = [paquet_auto.Removal(lot=1, tests=[TEST_A]),
                    paquet_auto.Removal(lot=2, tests=[TEST_B, other])]
        found = paquet_auto.earlier_red(
            self.repo, self.composition, removals, timeout=5,
            replay=lambda _p, label, modules, *, timeout: _red(label, modules),
            log=lambda _line: None)
        self.assertEqual(found, {})

    def test_un_seul_retrait_ne_rejoue_rien(self) -> None:
        def never(*_args, **_kwargs):
            raise AssertionError("aucun rejeu attendu")

        found = paquet_auto.earlier_red(
            self.repo, self.composition, self.removals()[1:], timeout=5,
            replay=never, log=lambda _line: None)
        self.assertEqual(found, {})


class CheckLocalTestsTest(StackCase):
    """Le vrai `check_local_tests` : le repli ne retire plus que A."""

    def check(self, innocent):
        def run_tests(_worktree, label, modules, **_kwargs):
            if label == "unité #1":
                return _red(label, modules, tests=(TEST_A,))
            if label == "unité #2":
                return _red(label, modules, tests=(TEST_B,))
            return _green(label, modules)

        seen = []

        def earlier(_worktree, _composition, removals, **_kwargs):
            seen.append([removal.lot for removal in removals])
            return innocent

        with mock.patch.object(paquet_auto, "run_tests", side_effect=run_tests), \
                mock.patch.object(paquet_auto, "contract_modules",
                                  return_value=[]):
            result = paquet_auto.check_local_tests(
                self.repo, self.composition, timeout=30, base_sha=self.base,
                attribute=lambda *_a: {"status": "unreadable"},
                select=lambda *_a: ({}, ""), earlier=earlier,
                parallel=1, log=lambda _line: None)
        return result, seen

    def test_l_unite_innocente_reste(self) -> None:
        (_runs, faulty, removals), seen = self.check({2: 1})
        self.assertEqual(seen, [[1, 2]])
        self.assertEqual(faulty, [1])
        self.assertEqual([removal.lot for removal in removals], [1])

    def test_sans_preuve_les_deux_sont_retirees(self) -> None:
        (_runs, faulty, _removals), _seen = self.check({})
        self.assertEqual(faulty, [1, 2])


if __name__ == "__main__":
    unittest.main()
