# SPDX-License-Identifier: AGPL-3.0-only
"""#6915 : bloc `projects.<p>.prevalidation`, surcharge par projet.

Le canon annonce qu'un bloc de projet remplace le bloc global pour ce projet.
Le lecteur accepte donc `prevalidation` sous `projects.<p>`, le compile comme
le bloc global (clé inconnue refusée), et `mechanics.load` rend le bloc du
projet en entier — jamais une fusion clé par clé ; les autres projets gardent
le bloc global, et un canon sans bloc de projet compile à l'identique."""
from __future__ import annotations

import tempfile
import unittest

import ameesh.mechanics  # noqa: F401
import ameesh.prevalidation  # noqa: F401
from ameesh import mechanics, prevalidation

from .test_1770_mechanics_prevalidation import MECHANICS, _Repo

FEDERATION = """federation: "0.1"
id: manaty
review_policies:
  risk_classes:
    default: normal
    projects:
      ameesh:
        default: light
        rules: []
      nexlink:
        default: light
        rules: []
  train_reviews:
    schema: ameesh-train-reviews-policy/1
    projects:
      ameesh:
        rules: []
      nexlink:
        rules: []
"""

GLOBAL = """prevalidation:
  classes: [P0, P1]
  contracts: [tests/parts/toujours.txt]
  hosts: [ameesh-agents-1]
  timeout_seconds: 3600
"""

#: le bloc nexlink ne déclare pas `parts_script` : il ne l'hérite pas du global
NEXLINK = """projects:
  nexlink:
    prevalidation:
      classes: [P0]
      contracts: [scripts/test-contracts.txt]
      hosts: [ameesh-agents-2, ameesh-agents-3]
      timeout_seconds: 1800
      plan_script: scripts/ameesh-test-plan.py
"""


def _document(projects: str = NEXLINK, schema: str = "ameesh-mechanics/2",
              global_block: str = GLOBAL) -> str:
    text = MECHANICS.replace("ameesh-mechanics/1", schema)
    text = text.replace("projects: {}\n", projects)
    return text + global_block.replace("timeout_seconds: 3600",
                                       "timeout_seconds: 3600\n  parts_script: tools/parts.py")


class ReaderTest(unittest.TestCase):
    def test_project_block_compiles_into_its_override(self):
        document = mechanics.compile_document(_document())
        block = document.override("nexlink").prevalidation
        self.assertEqual(block.classes, ("P0",))
        self.assertEqual(block.contracts, ("scripts/test-contracts.txt",))
        self.assertEqual(block.hosts, ("ameesh-agents-2", "ameesh-agents-3"))
        self.assertEqual(block.timeout_seconds, 1800)
        self.assertEqual(block.plan_script, "scripts/ameesh-test-plan.py")
        self.assertIsNone(block.parts_script)
        self.assertEqual(document.prevalidation.contracts, ("tests/parts/toujours.txt",))

    def test_unknown_key_in_project_block_refuses(self):
        text = _document(NEXLINK + "      extra: 1\n")
        with self.assertRaises(mechanics.MechanicsError) as caught:
            mechanics.compile_document(text)
        self.assertIn("projects.nexlink.prevalidation", str(caught.exception))
        self.assertIn("unknown key", str(caught.exception))

    def test_malformed_project_block_refuses(self):
        text = _document(NEXLINK.replace("hosts: [ameesh-agents-2, ameesh-agents-3]",
                                         "hosts: []"))
        with self.assertRaises(mechanics.MechanicsError) as caught:
            mechanics.compile_document(text)
        self.assertIn("projects.nexlink.prevalidation.hosts", str(caught.exception))

    def test_project_block_requires_schema_2(self):
        text = MECHANICS.replace("projects: {}\n", NEXLINK)
        with self.assertRaises(mechanics.MechanicsError) as caught:
            mechanics.compile_document(text)
        self.assertIn("requires schema ameesh-mechanics/2", str(caught.exception))

    def test_canon_without_project_block_is_unchanged(self):
        document = mechanics.compile_document(_document(projects="projects: {}\n"))
        self.assertEqual(document.overrides, ())
        plain = mechanics.compile_document(MECHANICS)
        self.assertNotIn("prevalidation", plain.to_dict())


class LoadTest(unittest.TestCase):
    def _load(self, text: str, project: str):
        with tempfile.TemporaryDirectory() as root:
            repo = _Repo(root, text, FEDERATION)
            commit = repo.git("rev-parse", "HEAD")
            return prevalidation.load_parameters(root, commit, project)[1]

    def test_nexlink_reads_its_block_and_ameesh_the_global_one(self):
        nexlink = self._load(_document(), "nexlink")
        self.assertEqual(nexlink.contracts, ("scripts/test-contracts.txt",))
        self.assertEqual(nexlink.timeout_seconds, 1800)
        # remplacement en bloc : rien n'est hérité du global
        self.assertIsNone(nexlink.parts_script)
        ameesh_block = self._load(_document(), "ameesh")
        self.assertEqual(ameesh_block.contracts, ("tests/parts/toujours.txt",))
        self.assertEqual(ameesh_block.parts_script, "tools/parts.py")

    def test_project_without_its_block_keeps_the_global_one(self):
        text = _document(projects="projects:\n  nexlink:\n    timezone: Asia/Manila\n")
        block = self._load(text, "nexlink")
        self.assertEqual(block.contracts, ("tests/parts/toujours.txt",))
        self.assertEqual(block.timeout_seconds, 3600)


if __name__ == "__main__":
    unittest.main()
