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

Le pilote a étendu le périmètre à l'inventaire des types clients (#4887) :
`scripts/garde-types-clients.py` refuse une spec OpenAPI changée sans le
schéma généré, avec `gen-api --check` quand `node` et les modules du cache
sont là, et la garde statique sinon.
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
_spec_types = importlib.util.spec_from_file_location(
    "garde_types_clients_7736", REPO / "scripts" / "garde-types-clients.py")
types_guard = importlib.util.module_from_spec(_spec_types)
_spec_types.loader.exec_module(types_guard)

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

    def test_la_garde_types_clients_est_branchee(self):
        commands = [args for _name, args in script.CHECKS]
        self.assertIn(["scripts/garde-types-clients.py"], commands)


class Proc:
    def __init__(self, code=0, out="", err=""):
        self.returncode = code
        self.stdout = out
        self.stderr = err


class ClientTypesTest(unittest.TestCase):
    """#4887 : le contrôle réel quand node est là, la garde statique sinon."""

    SPEC = "docs/api/openapi-v1.json"
    SCHEMA = "ui/src/api/schema.d.ts"
    GENERATOR = "ui/scripts/gen-api.mjs"

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ameesh-7736-types-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.ui = pathlib.Path(self.root, "ui")
        (self.ui / "scripts").mkdir(parents=True)
        pathlib.Path(self.root, self.GENERATOR).write_text("", encoding="utf-8")
        self.calls = []

    def fake_run(self, replies=(), on_link=None):
        replies = list(replies)

        def fake(argv, **_kwargs):
            self.calls.append(list(argv))
            if on_link and argv[1:3] == ["deps", "link"]:
                on_link()
            return replies.pop(0) if replies else Proc()

        return fake

    def fake_which(self, node="/opt/node", ameesh="/opt/ameesh"):
        return lambda name: {"node": node, "ameesh": ameesh}.get(name)

    def check(self, paths, *, node="/opt/node", ameesh="/opt/ameesh",
              replies=(), on_link=None):
        return types_guard.check(self.root, paths, which=self.fake_which(node, ameesh),
                                 run=self.fake_run(replies, on_link))

    def test_inchanges_sans_sous_processus(self):
        self.assertEqual(self.check(["src/ameesh/chain.py"]),
                         (0, "types clients : inchangés"))
        self.assertEqual(self.calls, [])

    def test_spec_sans_schema_sans_node_refuse(self):
        code, message = self.check([self.SPEC], node=None)
        self.assertEqual(code, 1)
        self.assertIn(self.SPEC, message)
        self.assertIn(self.SCHEMA, message)
        self.assertIn("npm run gen", message)

    def test_spec_avec_schema_sans_node_reste_non_concluant(self):
        code, message = self.check([self.SPEC, self.SCHEMA], node=None)
        self.assertEqual(code, 0)
        self.assertIn("non concluant", message)

    def test_chemins_illisibles_sont_un_doute(self):
        self.assertIsNone(types_guard.changed_paths({}))
        self.assertIsNone(types_guard.changed_paths(
            {types_guard.PATHS_ENV: "pas du json"}))
        self.assertIsNone(types_guard.changed_paths(
            {types_guard.PATHS_ENV: '{"docs/api/openapi-v1.json": 1}'}))
        self.assertEqual(types_guard.changed_paths(
            {types_guard.PATHS_ENV: '["docs/api/openapi-v1.json"]'}), [self.SPEC])

    def test_gen_api_vert_quand_les_modules_sont_la(self):
        (self.ui / "node_modules").mkdir()
        code, message = self.check([self.SPEC], replies=[Proc(0, "ok")])
        self.assertEqual(code, 0)
        self.assertIn("à jour", message)
        self.assertEqual(self.calls, [["/opt/node", self.GENERATOR, "--check"]])

    def test_gen_api_rouge_prouve_le_schema_perime(self):
        (self.ui / "node_modules").mkdir()
        code, message = self.check(
            [self.SPEC, self.SCHEMA],
            replies=[Proc(1, "", "schema.d.ts is stale: run `npm run gen`")])
        self.assertEqual(code, 1)
        self.assertIn("périmés", message)
        self.assertIn("stale", message)

    def test_module_manquant_replie_sur_la_garde_statique(self):
        (self.ui / "node_modules").mkdir()
        missing = Proc(1, "", "Error [ERR_MODULE_NOT_FOUND]: openapi-typescript")
        self.assertEqual(self.check([self.SPEC], replies=[missing])[0], 1)
        code, message = self.check([self.SPEC, self.SCHEMA], replies=[missing])
        self.assertEqual(code, 0)
        self.assertIn("modules incomplets", message)

    def test_generation_tuee_ne_prouve_pas_la_peremption(self):
        """#7736 B : SIGKILL n'est pas un diagnostic de schéma périmé."""
        (self.ui / "node_modules").mkdir()
        code, message = self.check([self.SPEC, self.SCHEMA],
                                   replies=[Proc(-9, "", "")])
        self.assertEqual(code, 0)
        self.assertIn("non concluant", message)
        self.assertIn("signal 9", message)

    def test_refus_d_acces_ne_prouve_pas_la_peremption(self):
        (self.ui / "node_modules").mkdir()
        code, message = self.check(
            [self.SPEC, self.SCHEMA],
            replies=[Proc(1, "", "EACCES: permission denied, open 'spec'")])
        self.assertEqual(code, 0)
        self.assertIn("non concluant", message)
        self.assertIn("EACCES", message)

    def test_refus_d_acces_avec_spec_sans_schema_refuse_par_la_garde(self):
        (self.ui / "node_modules").mkdir()
        code, message = self.check(
            [self.SPEC], replies=[Proc(1, "", "EACCES: permission denied")])
        self.assertEqual(code, 1)
        self.assertIn(self.SCHEMA, message)

    def test_le_cache_est_lie_avant_le_controle(self):
        code, message = self.check(
            [self.SPEC], replies=[Proc(0, "linked"), Proc(0, "ok")],
            on_link=lambda: (self.ui / "node_modules").mkdir())
        self.assertEqual(code, 0, message)
        self.assertEqual(self.calls[0],
                         ["/opt/ameesh", "deps", "link", "--no-fill", "ui"])
        self.assertEqual(self.calls[1], ["/opt/node", self.GENERATOR, "--check"])

    def test_cache_impossible_replie_sur_la_garde(self):
        code, message = self.check([self.SPEC], ameesh=None)
        self.assertEqual(code, 1)
        self.assertIn("ameesh absent", message)
        self.assertEqual(self.check([self.SPEC, self.SCHEMA], ameesh=None)[0], 0)


if __name__ == "__main__":
    unittest.main()
