# SPDX-License-Identifier: AGPL-3.0-only
"""#7736 : les témoins d'inventaire refusent le gel, pas seulement la CI de paquet.

Cas d'origine : le paquet h-08 a coûté quatre rouges et une CI complète
abandonnée (test_2449_garde_mecanique, test_contract_registry_ameesh,
test_l161f_plan, test_l107_executor_contract), tous attribués par préfixe. Ces
témoins durent moins d'une seconde mais ne tournaient qu'en CI de paquet :
l'auteur ne les rencontrait qu'après le gel, au prix d'un paquet.

Témoins : les quatre contrôles sont branchés dans `COHERENCE_CHECKS` et chaque
identifiant désigne une classe ou une méthode réelle ; un arbre propre passe ;
un arbre où un module entre dans la liste « toujours » sans le registre des
contrats voit son gel refusé, avec le nom des témoins et le module fautif.
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "freeze_checks_script_7736", REPO / "scripts" / "freeze-checks.py")
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

#: nom du contrôle → identifiant `unittest` appelé au gel
WITNESSES = {
    "liste « toujours » #7630":
        "tests.test_l161f_plan.MapTest.test_always_list_sorted_and_known",
    "registre des contrats #7726":
        "tests.test_contract_registry_ameesh.AmeeshRegistryTests",
    "couverture de la garde #2449":
        "tests.test_2449_garde_mecanique.SchemaCoverageTest",
    "table L107 #7671":
        "tests.test_l107_executor_contract.TableTest",
}


def exists(ident: str) -> bool:
    """L'identifiant `unittest` désigne une classe, ou une méthode de classe,
    d'un module de test du dépôt. Un renommage ne disparaît pas en silence :
    `unittest` rendrait 0 sur zéro test sélectionné."""
    parts = ident.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        path = REPO.joinpath(*parts[:cut]).with_suffix(".py")
        if path.is_file():
            break
    else:
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return False
    classes = {node.name: node for node in tree.body
               if isinstance(node, ast.ClassDef)}
    rest = parts[cut:]
    if not rest or rest[0] not in classes:
        return False
    if len(rest) == 1:
        return True
    if len(rest) != 2:
        return False
    return any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == rest[1] for node in classes[rest[0]].body)


class ScriptTest(unittest.TestCase):
    """Le script de gel, sur l'arbre réel puis sur une copie fautive."""

    def main(self, root, checks) -> tuple[int, str]:
        environ = {key: value for key, value in os.environ.items()
                   if key not in (script.GATE_ENV, script.PATHS_ENV)}
        # le contrôle des contrats de la porte et l'avertissement d'herméticité
        # ne font pas partie de ce témoin : ni l'un ni l'autre ne doit peser ici
        environ[script.GATE_ENV] = "[]"
        out = io.StringIO()
        with mock.patch.object(script.os, "getcwd", return_value=str(root)), \
                mock.patch.dict(script.os.environ, environ, clear=True), \
                mock.patch.object(script, "CHECKS", tuple(checks)), \
                mock.patch.object(script, "WARN_CHECKS", ()), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = script.main([])
        return code, out.getvalue()

    def copied_tree(self) -> str:
        root = tempfile.mkdtemp(prefix="ameesh-7736-")
        self.addCleanup(shutil.rmtree, root, True)
        for rel in ("src", "tests", "scripts", "docs"):
            shutil.copytree(REPO / rel, pathlib.Path(root, rel),
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(REPO / "pyproject.toml", pathlib.Path(root, "pyproject.toml"))
        return root

    def test_les_quatre_temoins_sont_branches(self):
        self.assertEqual({name: args[-1] for name, args in script.COHERENCE_CHECKS},
                         WITNESSES)
        self.assertEqual(script.CHECKS[-len(WITNESSES):], script.COHERENCE_CHECKS)
        for name, args in script.COHERENCE_CHECKS:
            with self.subTest(name=name):
                self.assertEqual(args[:3], ["-m", "unittest", "-q"])
                # `-k` rendrait 0 sur zéro test sélectionné : pas de filtre
                self.assertNotIn("-k", args)
                self.assertTrue(exists(args[-1]), args[-1])

    def test_un_arbre_propre_passe(self):
        code, out = self.main(REPO, script.COHERENCE_CHECKS)
        self.assertEqual(code, 0, out)
        for name, _args in script.COHERENCE_CHECKS:
            self.assertIn("ok  " + name, out)

    def test_un_module_inscrit_sans_registre_refuse_le_gel(self):
        """Le scénario du paquet : un lot ajoute un module de contrat à la
        liste « toujours » sans le registre universel ni la liste attendue du
        plan. Le gel est refusé, et les deux témoins sont nommés."""
        root = self.copied_tree()
        pathlib.Path(root, "tests", "test_7736_ghost.py").write_text(
            "import unittest\n\n\nclass Ghost(unittest.TestCase):\n"
            "    def test_x(self):\n        pass\n", encoding="utf-8")
        with open(pathlib.Path(root, "tests", "parts", "toujours.txt"),
                  "a", encoding="utf-8") as fh:
            fh.write("test_7736_ghost  # module ajouté sans registre\n")
        code, out = self.main(root, script.COHERENCE_CHECKS)
        self.assertEqual(code, 1, out)
        self.assertIn("RED liste « toujours » #7630", out)
        self.assertIn("RED registre des contrats #7726", out)
        self.assertIn("test_7736_ghost", out)
        # les inventaires intacts restent verts : seule la faute est nommée
        self.assertIn("ok  couverture de la garde #2449", out)
        self.assertIn("ok  table L107 #7671", out)


if __name__ == "__main__":
    unittest.main()
