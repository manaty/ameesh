# SPDX-License-Identifier: AGPL-3.0-only
"""#3954 — les contrats transverses entrent dans la sélection d'un lot.

Le constat de h-37 : un lot qui ajoute des opérations de stockage change
`src/ameesh/storage/interface.py`, mais la carte mesurée ne relie pas
`test_l107_executor_contract` à ce fichier (l'import est déjà chargé par le
harnais). Le rouge déterministe n'apparaît qu'à la CI du paquet (#3885). Un
test changé du lot (P0) est dû par son propre diff et ne doit pas être sauté
par un repli « contrats seuls » (#3916).

Témoins : la déclaration (module → fichiers lus) est valide, `hits` rend les
contrats dus par un change set, et le plan d'un lot garde le contrat
transverse dans sa sélection impactée. Les têtes citées par le lot sont
#3885 `68c30d91` (avant `9173a40dc`) et #3916 `7ae1e095` (avant `4cee317`).
"""
from __future__ import annotations

import pathlib
import sys
import unittest

from ameesh import transverse_contracts

from tests.test_l161f_plan import NOW, _Repo, _load

REPO = pathlib.Path(__file__).resolve().parent.parent
plan = _load("test_plan_3954", "test-plan.py")
parts = _load("test_parts_3954", "test-parts.py")
sys.path.insert(0, str(REPO / "scripts"))
import map as map_mod  # noqa: E402


class DeclarationTest(unittest.TestCase):
    """La déclaration est valide, triée et couvre les deux contrats du constat."""

    def test_modules_connus_et_chemins_existants(self):
        known = set(parts.modules())
        self.assertEqual(sorted(transverse_contracts.CONTRACTS),
                         list(transverse_contracts.CONTRACTS))
        self.assertEqual(transverse_contracts.problem(known), "")
        for module, patterns in transverse_contracts.CONTRACTS.items():
            with self.subTest(module=module):
                self.assertIn(module, known)
                self.assertEqual(list(patterns), sorted(patterns))
                for pattern in patterns:
                    path = REPO / pattern.rstrip("/")
                    self.assertTrue(path.exists(), pattern)
                    if pattern.endswith("/"):
                        self.assertTrue(path.is_dir(), pattern)
                    else:
                        self.assertTrue(path.is_file(), pattern)

    def test_les_deux_contrats_du_constat_sont_declares(self):
        self.assertIn("test_l107_executor_contract", transverse_contracts.CONTRACTS)
        self.assertIn("test_cluster", transverse_contracts.CONTRACTS)
        self.assertIn("src/ameesh/mediated_executor/",
                      transverse_contracts.CONTRACTS["test_l107_executor_contract"])
        self.assertIn("src/ameesh/storage/interface.py",
                      transverse_contracts.CONTRACTS["test_l107_executor_contract"])
        self.assertIn("src/ameesh/migrations/",
                      transverse_contracts.CONTRACTS["test_cluster"])
        self.assertIn("deploy/sql/",
                      transverse_contracts.CONTRACTS["test_cluster"])


class HitsTest(unittest.TestCase):
    def hits(self, *paths):
        return transverse_contracts.hits([("M", path) for path in paths])

    def test_interface_de_stockage_rend_le_contrat_executeur(self):
        # Témoin #3885 : le lot change la liste des opérations, pas le test.
        self.assertEqual(self.hits("src/ameesh/storage/interface.py"),
                         {"test_l107_executor_contract":
                          "src/ameesh/storage/interface.py"})

    def test_contract_json_et_operations_de_stockage(self):
        self.assertEqual(self.hits("src/ameesh/mediated_executor/contract.json"),
                         {"test_l107_executor_contract":
                          "src/ameesh/mediated_executor/contract.json"})
        self.assertEqual(self.hits("src/ameesh/storage/postgres/work.py"),
                         {"test_l107_executor_contract":
                          "src/ameesh/storage/postgres/work.py"})

    def test_migration_et_droits_rendent_le_contrat_cluster(self):
        self.assertEqual(self.hits("src/ameesh/migrations/0492_x.sql"),
                         {"test_cluster": "src/ameesh/migrations/0492_x.sql"})
        self.assertEqual(self.hits("deploy/sql/role-superviseur.sql"),
                         {"test_cluster": "deploy/sql/role-superviseur.sql"})
        self.assertEqual(self.hits("deploy/k8s/statefulset.yaml"),
                         {"test_cluster": "deploy/k8s/statefulset.yaml"})

    def test_suppression_et_fichier_hors_contrat(self):
        self.assertEqual(transverse_contracts.hits(
            [("D", "src/ameesh/db.py")]),
            {"test_cluster": "src/ameesh/db.py",
             "test_l107_executor_contract": "src/ameesh/db.py"})
        self.assertEqual(self.hits("src/ameesh/config.py"),
                         {"test_l107_executor_contract": "src/ameesh/config.py"})
        self.assertEqual(self.hits("src/ameesh/mesh_cli.py"), {})
        # un préfixe sans « / » est un fichier exact, pas un dossier
        self.assertEqual(self.hits("Dockerfile.bak"), {})
        self.assertEqual(self.hits("Dockerfile"), {"test_cluster": "Dockerfile"})

    def test_module_disparu_rend_la_declaration_inutilisable(self):
        problem = transverse_contracts.problem(("test_cluster",))
        self.assertIn("test_l107_executor_contract", problem)


class PlanTest(unittest.TestCase):
    """Le plan d'un lot ajoute le contrat transverse aux impactés P1."""

    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.repo.path,
                                                            ignore_errors=True))
        self.repo.write("src/ameesh/storage/interface.py",
                        "class Storage:\n    def ops(self):\n        return 1\n")
        self.base = self.repo.commit()
        self.card = self._card("class Storage:\n    def ops(self):\n        return 1\n")

    def _card(self, code):
        infos, _ = map_mod.blocks(code)
        modules = {name: {"durees": {"psycopg": 0.2}, "echecs": []}
                   for name in ("test_a", "test_l107_executor_contract",
                                "test_cluster")}
        return {
            "schema": map_mod.SCHEMA, "commit": self.base,
            "genere": NOW.isoformat(), "modules": modules,
            "blocs": {"src/ameesh/storage/interface.py": {
                name: {**info, "modules": ["test_a"]} for name, info in infos.items()}},
            "donnees": {}, "dossiers": {}, "tables": {},
        }

    def test_interface_changee_rend_le_contrat_du(self):
        # Témoin #3885 (68c30d91) : l'interface gagne des opérations ; la
        # carte ne relie le bloc qu'à test_a, le contrat reste dû.
        self.repo.write("src/ameesh/storage/interface.py",
                        "class Storage:\n    def ops(self):\n        return 2\n")
        head = self.repo.commit()
        state = plan.classify(parts.modules(), self.card, NOW,
                              base_fallback=self.base, head=head,
                              repo=self.repo.path)
        self.assertTrue(state["sure"], state["unsure"])
        self.assertEqual(state["classes"]["test_l107_executor_contract"][0], "P1")
        self.assertIn("contrat transverse",
                      state["classes"]["test_l107_executor_contract"][1])
        self.assertEqual(state["transverse"],
                         {"test_l107_executor_contract":
                          "src/ameesh/storage/interface.py"})
        classes = {name: tuple(value) for name, value in state["classes"].items()}
        selection, why = plan.lot_selection(True, state, parts.modules(), classes)
        self.assertTrue(selection, why)
        self.assertIn("test_l107_executor_contract", selection)

    def test_fichier_hors_contrat_ne_l_ajoute_pas(self):
        self.repo.write("src/ameesh/code.py", CODE_V1_CHANGED)
        head = self.repo.commit()
        state = plan.classify(parts.modules(), self.card, NOW,
                              base_fallback=self.base, head=head,
                              repo=self.repo.path)
        self.assertEqual(state["transverse"], {})
        self.assertEqual(state["classes"]["test_l107_executor_contract"][0], "P3")


CODE_V1_CHANGED = ('"""Module."""\nLIMIT = 4\n\n\ndef used(x):\n    return x + 1\n')


if __name__ == "__main__":
    unittest.main()
