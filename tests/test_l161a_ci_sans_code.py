# SPDX-License-Identifier: AGPL-3.0-only
"""L161a : une PR de documentation seule ne passe que les tests qui lisent la
documentation, et une exécution de main n'est jamais annulée par la suivante.

Tests sans base : détection d'une PR de documentation seule
(scripts/version-only.py), liste des lecteurs déclarés, workflow.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("version_only",
                                               REPO / "scripts" / "version-only.py")
version_only = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(version_only)

WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"
# un test qui ouvre un fichier du dépôt sous docs/ ou site/
_READS_DOCS = re.compile(r"""(ROOT|REPO|racine)\s*[,/]\s*["'](docs|site)["']""")


class DocsOnlyTest(unittest.TestCase):

    def test_documentation_seule(self):
        cas = [
            ["docs/design/etudes/x.md"],
            ["site/docs/index.md", "site/landing/index.html"],
            ["README.md", "CONTRIBUTING.md", "docs/ORCHESTRATEUR.md"],
            ["deploy/README.md"],
        ]
        for names in cas:
            ok, why = version_only.docs_only(names)
            self.assertTrue(ok, (names, why))

    def test_autre_chose(self):
        cas = [
            [],
            ["docs/x.md", "src/ameesh/work.py"],
            ["src/ameesh/consignes/orchestrateur.md"],   # lu par le paquet
            ["tests/parts/notes.md"],
            [".github/pull_request_template.md"],
            ["mkdocs.yml"],
            ["pyproject.toml"],
            ["examples/canon/agents/ouvrier.md"],          # lu par test_canon, sur base
        ]
        for names in cas:
            ok, why = version_only.docs_only(names)
            self.assertFalse(ok, (names, why))


class ReadersTest(unittest.TestCase):

    def test_liste_rangee_et_connue(self):
        readers = version_only.doc_readers()
        self.assertTrue(readers)
        self.assertEqual(readers, sorted(set(readers)))
        for name in readers:
            self.assertTrue((REPO / (name.replace(".", "/") + ".py")).exists(), name)

    def test_tout_lecteur_est_declare(self):
        declared = {n.split(".", 1)[1] for n in version_only.doc_readers()}
        found = {p.stem for p in (REPO / "tests").glob("test_*.py")
                 if _READS_DOCS.search(p.read_text(encoding="utf-8"))}
        self.assertTrue(found)
        self.assertEqual(found - declared, set(),
                         "à déclarer dans tests/parts/lecteurs-doc.txt")


class WorkflowTest(unittest.TestCase):

    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_parts_sautees_pour_la_doc(self):
        # L161f : les matrices du plan sont vides pour une PR de documentation
        self.assertIn("if: needs.plan.outputs.psycopg != '[]'", self.text)
        self.assertIn("if: needs.plan.outputs.psql != '[]'", self.text)
        self.assertIn("if: needs.plan.outputs.code == 'true'", self.text)
        self.assertIn("if: needs.plan.outputs.docs_only == 'true'", self.text)
        self.assertIn("python -m unittest -v ${{ needs.plan.outputs.doc_readers }}", self.text)
        self.assertIn("needs: [plan, version, psycopg, psql, platform-macos, doc-readers]", self.text)
        self.assertIn('test "${{ needs.doc-readers.result }}" = success', self.text)

    def test_main_jamais_annulee(self):
        # hors PR, un groupe par exécution : ni annulée, ni écartée en attente
        self.assertIn("|| github.run_id }}", self.text)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", self.text)
        self.assertNotIn("cancel-in-progress: true", self.text)


if __name__ == "__main__":
    unittest.main()
