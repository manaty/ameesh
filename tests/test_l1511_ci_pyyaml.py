# SPDX-License-Identifier: AGPL-3.0-only
"""#1511 : PyYAML est fourni partout où tourne ManifestesTest.

Sans base : lecture des workflows. `tests/test_cluster.py` porte
`ManifestesTest` (manifestes `deploy/k8s`) derrière `skipIf(yaml is None)` ;
`test_cluster` est dans la liste « toujours » (`tests/parts/toujours.txt`,
contrat L1211) : il tourne sous les deux pilotes, et la carte le mesure sous
les deux aussi. Sans PyYAML dans l'environnement, le module est sélectionné
puis sauté — la vérification des manifestes n'a jamais lieu en CI. Ce témoin
exige PyYAML dans les trois environnements qui exécutent la suite :

* `.venv-ci` (parts psycopg) : installé, et présent dans le contrôle d'import
  qui valide un venv restauré du cache (un venv sans PyYAML doit être
  reconstruit, pas servi) ;
* parts psql (setup-python nu) : installé avant la part ;
* `.venv-carte` (mesure de la carte), dans les deux branches de pilote.
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
TESTS = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
CARTE = (REPO / ".github" / "workflows" / "carte.yml").read_text(encoding="utf-8")
TOUJOURS = (REPO / "tests" / "parts" / "toujours.txt").read_text(encoding="utf-8")

PYYAML = "'pyyaml>=6'"


def _etape(workflow: str, marqueur: str) -> str:
    """Le bloc d'une étape, de son marqueur à l'étape suivante."""
    m = re.search(re.escape(marqueur) + r"[^\n]*\n(.*?)(?=\n      - |\n  [a-z-]+:|\Z)",
                  workflow, re.S)
    if m is None:
        raise AssertionError("étape introuvable : %s" % marqueur)
    return m.group(0)


class PyYamlCiTest(unittest.TestCase):

    def test_manifestes_dans_la_liste_contractuelle(self):
        # la raison d'être : ManifestesTest vit dans test_cluster, « toujours ».
        self.assertIn("test_cluster", TOUJOURS)
        cluster = (REPO / "tests" / "test_cluster.py").read_text(encoding="utf-8")
        self.assertIn("skipIf(yaml is None", cluster)

    def test_venv_psycopg_installe_et_controle_pyyaml(self):
        # installé (sinon sauté) ET contrôlé à la restauration du cache
        # (sinon un venv restauré sans PyYAML serait déclaré valide).
        deps = _etape(TESTS, "      - id: deps")
        self.assertIn(PYYAML, deps)
        self.assertIn("import psycopg, cryptography, psutil, coverage, yaml", deps)

    def test_part_psql_installe_pyyaml(self):
        # le job psql n'a pas de venv : il installe PyYAML avant la part.
        m = re.search(r"\n  psql:\n(.*?)\n  platform-macos:", TESTS, re.S)
        self.assertIsNotNone(m, "job psql introuvable")
        self.assertIn(PYYAML, m.group(1))

    def test_mesure_carte_installe_pyyaml_pour_les_deux_pilotes(self):
        # la carte mesure test_cluster sous psycopg et sous psql : les deux
        # branches de .venv-carte doivent fournir PyYAML.
        m = re.search(r"name: environnement de mesure\n(.*?)\n      - ", CARTE, re.S)
        self.assertIsNotNone(m, "étape « environnement de mesure » introuvable")
        self.assertEqual(2, m.group(1).count(PYYAML),
                         "les deux branches de pilote de .venv-carte doivent installer PyYAML")


if __name__ == "__main__":
    unittest.main()
