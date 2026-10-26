# SPDX-License-Identifier: AGPL-3.0-only
"""#3687 : au gel, les contrats de la porte de fusion tournent quand le diff
touche la porte, et un rouge refuse le gel.

Cas d'origine (h-25) : #3584 changeait `_complete_authors` sans adapter
test_1374 ; ni les avis ni le composeur ne l'ont vu. Témoins : le script
(`scripts/freeze-checks.py` : chaque contrat existe et a une classe sans base,
seules ces classes tournent, `[]` ne lance rien, le doute lance, un contrat
rouge rend 1, et le vrai test_1374 refuse un `_complete_authors` changé) ; le
garde (`ameesh.freeze_checks` : les chemins de la porte lus à la politique par
la règle qui nomme `fire.py`, doute rendu None, variable transmise au script)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import freeze_checks

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "freeze_checks_script_3687", REPO / "scripts" / "freeze-checks.py")
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")

MODULE = '''import unittest
from .support import PgTestCase


class Simple(unittest.TestCase):
    def test_a(self):
        pass


class _Fixture:
    pass


class WithFixture(_Fixture, unittest.TestCase):
    def test_a(self):
        pass


class Db(PgTestCase):
    pass


class DbChild(Db):
    pass


class DbMixed(_Fixture, PgTestCase):
    pass


class _Private(unittest.TestCase):
    pass


class Helper:
    pass
'''


def _git(root, *args) -> str:
    return subprocess.run(["git", "-C", root, *args], check=True, env=ENV,
                          capture_output=True, text=True).stdout.strip()


class ScriptTest(unittest.TestCase):
    """Le script d'ameesh : le contrôle conditionnel des contrats de la porte."""

    def tree(self) -> str:
        root = tempfile.mkdtemp(prefix="ameesh-3687-arbre-")
        self.addCleanup(shutil.rmtree, root, True)
        for rel in ("src/ameesh", "scripts", "tests"):
            os.makedirs(os.path.join(root, rel))
        pathlib.Path(root, "tests", "__init__.py").write_text("", encoding="utf-8")
        return root

    def main(self, root: str, gate, *, contracts=None, checks=()) -> tuple[int, str]:
        environ = {k: v for k, v in os.environ.items() if k != script.GATE_ENV}
        if gate is not None:
            environ[script.GATE_ENV] = gate
        out = io.StringIO()
        with mock.patch.object(script.os, "getcwd", return_value=root), \
                mock.patch.dict(script.os.environ, environ, clear=True), \
                mock.patch.object(script, "CHECKS", tuple(checks)), \
                mock.patch.object(script, "GATE_CONTRACTS",
                                  script.GATE_CONTRACTS if contracts is None
                                  else tuple(contracts)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = script.main([])
        return code, out.getvalue()

    def test_chaque_contrat_existe_et_tourne_sans_base(self):
        """Un contrat renommé, retiré ou passé tout entier sur Postgres ne
        disparaît pas du gel en silence."""
        self.assertTrue(script.GATE_CONTRACTS)
        for module in script.GATE_CONTRACTS:
            fichier = REPO.joinpath(*module.split(".")).with_suffix(".py")
            self.assertTrue(fichier.is_file(), module)
            self.assertTrue(script.static_classes(str(REPO), module), module)
        # le cas d'origine (h-25) est couvert
        self.assertIn("tests.test_1374_fire.CompleteAuthorsTest",
                      script.static_classes(str(REPO), "tests.test_1374_fire"))

    def test_seules_les_classes_sans_base_tournent(self):
        root = self.tree()
        pathlib.Path(root, "tests", "test_x.py").write_text(MODULE, encoding="utf-8")
        self.assertEqual(script.static_classes(root, "tests.test_x"),
                         ["tests.test_x.Simple", "tests.test_x.WithFixture"])
        self.assertEqual(script.static_classes(root, "tests.test_absent"), [])

    def test_lecture_des_chemins_de_la_porte(self):
        self.assertEqual(script.gate_touched({}), (True, "gate paths unknown"))
        self.assertEqual(script.gate_touched({script.GATE_ENV: "[]"}),
                         (False, "merge gate not touched"))
        touched, why = script.gate_touched({script.GATE_ENV: '["src/ameesh/fire.py"]'})
        self.assertTrue(touched)
        self.assertIn("src/ameesh/fire.py", why)
        for raw in ("pas du json", '{"a": 1}', "[1]", '"src/ameesh/fire.py"'):
            self.assertEqual(script.gate_touched({script.GATE_ENV: raw}),
                             (True, "gate paths unreadable"), raw)

    def _red_contract(self, root: str) -> None:
        pathlib.Path(root, "tests", "test_porte.py").write_text(
            "import unittest\n\n\nclass Porte(unittest.TestCase):\n"
            "    def test_contrat(self):\n        self.fail('contrat cassé')\n",
            encoding="utf-8")

    def test_porte_non_touchee_ne_lance_aucun_module(self):
        root = self.tree()
        self._red_contract(root)
        code, out = self.main(root, "[]", contracts=["tests.test_porte"],
                              checks=[("vert", ["-c", "pass"])])
        self.assertEqual(code, 0, out)
        self.assertIn("gate contracts: none (merge gate not touched)", out)
        self.assertNotIn("contrat de la porte", out)

    def test_porte_touchee_contrat_rouge_refuse(self):
        root = self.tree()
        self._red_contract(root)
        code, out = self.main(root, '["src/ameesh/merge_queue.py"]',
                              contracts=["tests.test_porte"])
        self.assertEqual(code, 1, out)
        self.assertIn("RED contrat de la porte #3687", out)
        self.assertIn("contrat cassé", out)

    def test_doute_lance_les_contrats(self):
        root = self.tree()
        self._red_contract(root)
        code, out = self.main(root, None, contracts=["tests.test_porte"])
        self.assertEqual(code, 1, out)
        self.assertIn("gate contracts: run (gate paths unknown)", out)

    def test_contrats_tout_postgres_rien_a_lancer(self):
        root = self.tree()
        pathlib.Path(root, "tests", "test_pg.py").write_text(
            "from .support import PgTestCase\n\n\nclass Db(PgTestCase):\n    pass\n",
            encoding="utf-8")
        code, out = self.main(root, '["src/ameesh/fire.py"]', contracts=["tests.test_pg"])
        self.assertEqual(code, 0, out)
        self.assertIn("no database-free contract class", out)

    def test_cas_d_origine_complete_authors_change_refuse(self):
        """h-25 : un `_complete_authors` changé sans adapter test_1374 est
        refusé ; le même arbre sans le changement passe (témoin)."""
        root = tempfile.mkdtemp(prefix="ameesh-3687-reel-")
        self.addCleanup(shutil.rmtree, root, True)
        shutil.copytree(REPO / "src", pathlib.Path(root, "src"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        os.symlink(REPO / "tests", pathlib.Path(root, "tests"))
        os.makedirs(pathlib.Path(root, "scripts"))
        gate = '["src/ameesh/merge_queue.py"]'
        code, out = self.main(root, gate, contracts=["tests.test_1374_fire"])
        self.assertEqual(code, 0, out)
        with open(pathlib.Path(root, "src/ameesh/merge_queue.py"), "a",
                  encoding="utf-8") as fh:
            fh.write("\n\ndef _complete_authors(*_args, **_kwargs):\n"
                     "    return [], set()\n")
        code, out = self.main(root, gate, contracts=["tests.test_1374_fire"])
        self.assertEqual(code, 1, out)
        self.assertIn("RED contrat de la porte #3687", out)
        self.assertIn("CompleteAuthorsTest", out)


def _canon(rules, project="ameesh"):
    return types.SimpleNamespace(root="canon", federation={"review_policies": {
        "risk_classes": {"projects": {project: {"default": "light", "rules": rules}}}}})


GATE_RULE = {"paths": ["src/ameesh/fire.py", "src/ameesh/merge_queue.py",
                       "src/ameesh/review*.py"], "class": "sensitive"}
OTHER_RULE = {"paths": ["src/ameesh/merge_queue.py", "src/ameesh/work.py"],
              "class": "sensitive"}


class GuardTest(unittest.TestCase):
    """Le garde : les chemins de la porte lus à la politique, transmis au script."""

    def repo(self, *changed) -> tuple[str, str]:
        root = tempfile.mkdtemp(prefix="ameesh-3687-depot-")
        self.addCleanup(shutil.rmtree, root, True)
        subprocess.run(["git", "init", "-q", "-b", "main", root], check=True, env=ENV)
        _git(root, "commit", "-q", "--allow-empty", "-m", "socle")
        _git(root, "checkout", "-q", "-b", "lot/test")
        for rel in changed:
            full = os.path.join(root, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            pathlib.Path(full).write_text("x\n", encoding="utf-8")
            _git(root, "add", rel)
        _git(root, "commit", "-q", "--allow-empty", "-m", "lot")
        return root, _git(root, "rev-parse", "HEAD")

    ITEM = {"app": "ameesh", "branch_target": "main"}

    def test_chemins_de_la_porte_lus_a_la_politique(self):
        root, head = self.repo("src/ameesh/merge_queue.py", "src/ameesh/work.py",
                               "src/ameesh/review_designation.py", "docs/x.md")
        gate = freeze_checks.gate_paths(None, self.ITEM, root, head,
                                        canons=[_canon([OTHER_RULE, GATE_RULE])])
        self.assertEqual(gate, ["src/ameesh/merge_queue.py",
                                "src/ameesh/review_designation.py"])

    def test_porte_hors_du_diff(self):
        root, head = self.repo("src/ameesh/work.py")
        self.assertEqual(freeze_checks.gate_paths(
            None, self.ITEM, root, head, canons=[_canon([OTHER_RULE, GATE_RULE])]), [])

    def test_doutes(self):
        root, head = self.repo("src/ameesh/fire.py")
        canons = [_canon([GATE_RULE])]
        # aucune règle ne nomme l'ancre, aucun canon, projet non déclaré
        self.assertIsNone(freeze_checks.gate_paths(
            None, self.ITEM, root, head, canons=[_canon([OTHER_RULE])]))
        self.assertIsNone(freeze_checks.gate_paths(None, self.ITEM, root, head, canons=[]))
        self.assertIsNone(freeze_checks.gate_paths(
            None, self.ITEM, root, head, canons=[_canon([GATE_RULE], project="nexlink")]))
        # cible illisible
        self.assertIsNone(freeze_checks.gate_paths(
            None, {"app": "ameesh", "branch_target": "absente"}, root, head,
            canons=canons))

    def test_variable_transmise_au_script(self):
        root, _head = self.repo()
        pathlib.Path(root, "scripts").mkdir()
        pathlib.Path(root, freeze_checks.SCRIPT).write_text(
            "import os\nprint('gate=' + repr(os.environ.get('%s')))\n"
            % freeze_checks.GATE_ENV, encoding="utf-8")
        _git(root, "add", freeze_checks.SCRIPT)
        _git(root, "commit", "-q", "-m", "script")
        commit = _git(root, "rev-parse", "HEAD")
        result = freeze_checks.run(root, commit, gate=["src/ameesh/fire.py"])
        self.assertIn("gate=%r" % json.dumps(["src/ameesh/fire.py"]), result["output"])
        # le doute ne transmet rien, même hérité de l'environnement de l'appelant
        with mock.patch.dict(os.environ, {freeze_checks.GATE_ENV: "[]"}):
            result = freeze_checks.run(root, commit, gate=None)
        self.assertIn("gate=None", result["output"])


if __name__ == "__main__":
    unittest.main()
