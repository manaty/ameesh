# SPDX-License-Identifier: AGPL-3.0-only
"""#1770 : bloc canon `prevalidation`, extension additive du lecteur (#1616).

Le document sans bloc compile **à l'identique** : schéma `ameesh-mechanics/1`,
clé `prevalidation` absente, dictionnaire inchangé. Le bloc n'existe que sous
le schéma `/2`, et chaque paramètre (classes, contrats, hôtes, délai) est relu,
jamais supposé. Le consommateur explicite refuse un bloc absent, cause nommée
`block-missing` : l'accroche automatique du gel reste inactive tant que la
règle n'est pas adoptée."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

#: le graphe garanti du plan L161f ne prouve un fichier que par `import
#: ameesh.x` / `from ameesh.x import y` : `from ameesh import x` ne charge le
#: sous-module que si le paquet ne lie pas déjà le nom (#1339). Ces imports
#: explicites certifient le chargement des deux fichiers au gel (#1770).
import ameesh.mechanics  # noqa: F401
import ameesh.prevalidation  # noqa: F401
from ameesh import mechanics, prevalidation

MECHANICS = """schema: ameesh-mechanics/1
timezone: Europe/Paris
cutoffs: ["11:00", "23:00"]
build:
  max_in_build: 2
ci:
  packet: full
  incident_label: ci-full
  environment_red: does_not_block
  faulty_lot: removed_without_new_review
reviews:
  families: [claude, codex, deepseek]
  exclude_author_family: true
  distinct_families: true
merge:
  named_fire: false
  repeat_freeze: false
  on_green_packet: true
  post_merge_red: forward_or_revert
projects: {}
"""

FEDERATION = """federation: "0.1"
id: manaty
review_policies:
  risk_classes:
    default: normal
    projects:
      ameesh:
        default: light
        rules: []
  train_reviews:
    schema: ameesh-train-reviews-policy/1
    projects:
      ameesh:
        rules: []
"""

#: le dictionnaire rendu par le lecteur /1 avant ce delta : témoin d'identité
IDENTICAL = {
    "schema": "ameesh-mechanics/1",
    "timezone": "Europe/Paris",
    "cutoffs": ["11:00", "23:00"],
    "packets": {"cutoffs": []},
    "build": {"max_in_build": 2},
    "ci": {"packet": "full", "incident_label": "ci-full",
           "environment_red": "does_not_block",
           "faulty_lot": "removed_without_new_review"},
    "reviews": {"families": ["claude", "codex", "deepseek"],
                "exclude_author_family": True,
                "distinct_families": True},
    "merge": {"named_fire": False, "repeat_freeze": False,
              "on_green_packet": True,
              "post_merge_red": "forward_or_revert"},
    "projects": [],
}


def _block(**over) -> str:
    """Un bloc `prevalidation` bien formé, surchargé clé par clé."""
    values = {"classes": "[P0]", "contracts": "[]", "hosts": "[h1]",
              "timeout_seconds": "60"}
    values.update(over)
    body = "".join("  %s: %s\n" % (key, value)
                   for key, value in values.items() if value is not None)
    return "prevalidation:\n" + body


def _document(block: str = "", schema: str = "ameesh-mechanics/2") -> str:
    text = MECHANICS.replace("ameesh-mechanics/1", schema)
    return text + block


class _Repo:
    """Un dépôt canon jetable (mécanique + fédération)."""

    def __init__(self, root: str, mechanics_text: str, federation_text: str = FEDERATION):
        self.root = root
        self.dry = dict(os.environ,
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.git("init", "-q")
        self.write("processus/mecanique.yaml", mechanics_text)
        self.write("federation.yaml", federation_text)
        self.git("add", "-A")
        self.git("commit", "-qm", "initial")

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", "-C", self.root, *args],
                              capture_output=True, text=True, env=self.dry)
        if proc.returncode:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def write(self, path: str, text: str) -> None:
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)


class ReaderTest(unittest.TestCase):
    def test_document_without_block_compiles_identically(self):
        document = mechanics.compile_document(MECHANICS)
        self.assertEqual(document.schema, mechanics.SCHEMA)
        self.assertIsNone(document.prevalidation)
        self.assertNotIn("prevalidation", document.to_dict())
        # le dictionnaire complet est celui d'avant le delta, clé pour clé
        self.assertEqual(document.to_dict(), IDENTICAL)

    def test_unknown_key_still_refuses(self):
        with self.assertRaises(mechanics.MechanicsError) as caught:
            mechanics.compile_document(MECHANICS + "inconnue: 1\n")
        self.assertIn("unknown key", str(caught.exception))

    def test_block_parses_and_classes_follow_canonical_order(self):
        document = mechanics.compile_document(_document(_block(
            classes="[P1, P0]", contracts="[tests/parts/toujours.txt]",
            hosts="[ameesh-agents-1, ameesh-agents-2]", timeout_seconds="3600")))
        self.assertEqual(document.schema, mechanics.SCHEMA_PREVALIDATION)
        block = document.prevalidation
        self.assertEqual(block.classes, ("P0", "P1"))
        self.assertEqual(block.contracts, ("tests/parts/toujours.txt",))
        self.assertEqual(block.hosts, ("ameesh-agents-1", "ameesh-agents-2"))
        self.assertEqual(block.timeout_seconds, 3600)
        self.assertEqual(document.to_dict()["prevalidation"],
                         {"classes": ["P0", "P1"],
                          "contracts": ["tests/parts/toujours.txt"],
                          "hosts": ["ameesh-agents-1", "ameesh-agents-2"],
                          "timeout_seconds": 3600})

    def test_parts_script_is_declared_by_the_block_only(self):
        """Le lanceur de part : optionnel au bloc, aucune valeur au lecteur."""
        declared = mechanics.compile_document(_document(_block(
            parts_script="tools/parts.py")))
        self.assertEqual(declared.prevalidation.parts_script, "tools/parts.py")
        self.assertEqual(declared.to_dict()["prevalidation"]["parts_script"],
                         "tools/parts.py")
        # absent, le dictionnaire compilé reste celui du bloc déclaré : la
        # convention (`scripts/test-parts.py`) appartient au consommateur
        default = mechanics.compile_document(_document(_block()))
        self.assertIsNone(default.prevalidation.parts_script)
        self.assertNotIn("parts_script", default.to_dict()["prevalidation"])

    def test_block_and_schema_are_consistent(self):
        with self.assertRaises(mechanics.MechanicsError) as caught:
            mechanics.compile_document(_document(_block(), schema="ameesh-mechanics/1"))
        self.assertIn("requires schema ameesh-mechanics/2", str(caught.exception))
        with self.assertRaises(mechanics.MechanicsError) as caught:
            mechanics.compile_document(_document())
        self.assertIn("requires the prevalidation block", str(caught.exception))
        with self.assertRaises(mechanics.MechanicsError) as caught:
            mechanics.compile_document(_document(_block(), schema="ameesh-mechanics/3"))
        self.assertIn("schema ameesh-mechanics/1 is required", str(caught.exception))

    def test_malformed_block_refuses(self):
        cases = {
            "unknown key": _block(extra="1"),
            "key missing": "prevalidation:\n  classes: [P0]\n",
            "empty classes": _block(classes="[]"),
            "unknown class": _block(classes="[P9]"),
            "duplicate class": _block(classes="[P0, P0]"),
            "class not a label": _block(classes="[p0]"),
            "contracts not a list": _block(contracts="tests/parts/toujours.txt"),
            "empty contract": _block(contracts="[\"\"]"),
            "absolute contract": _block(contracts="[/etc/passwd]"),
            "escaping contract": _block(contracts="[../secret]"),
            "backslash contract": _block(contracts="['a\\\\b']"),
            "duplicate contract": _block(contracts="[a.txt, a.txt]"),
            "empty hosts": _block(hosts="[]"),
            "host not a label": _block(hosts="['a b']"),
            "duplicate host": _block(hosts="[h1, h1]"),
            "host not a string": _block(hosts="[1]"),
            "absolute parts_script": _block(parts_script="/usr/bin/python3"),
            "escaping parts_script": _block(parts_script="../runner.py"),
            "empty parts_script": _block(parts_script='""'),
            "null parts_script": _block(parts_script="null"),
            "timeout zero": _block(timeout_seconds="0"),
            "timeout not an int": _block(timeout_seconds="1h"),
        }
        for name, text in cases.items():
            with self.subTest(name):
                with self.assertRaises(mechanics.MechanicsError):
                    mechanics.compile_document(_document(text))


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_load_exposes_the_block_only_when_declared(self):
        repo = _Repo(self.tmp.name, MECHANICS)
        head = repo.git("rev-parse", "HEAD")
        self.assertIsNone(mechanics.load(repo.root, head, "ameesh").prevalidation)
        with open(os.path.join(repo.root, "processus/mecanique.yaml"), "w",
                  encoding="utf-8") as handle:
            handle.write(_document(_block(classes="[P0, P1]")))
        head = repo.git("add", "-A") or repo.git("commit", "-qm", "bloc")
        head = repo.git("rev-parse", "HEAD")
        compiled = mechanics.load(repo.root, head, "ameesh")
        self.assertEqual(compiled.prevalidation.classes, ("P0", "P1"))
        self.assertEqual(compiled.to_dict()["prevalidation"]["hosts"], ["h1"])


class ConsumerTest(unittest.TestCase):
    def _parameters(self, document):
        with mock.patch.object(mechanics, "load", return_value=document):
            return prevalidation.load_parameters("/repo", "a" * 40, "ameesh")

    def test_absent_block_is_a_named_refusal(self):
        document = SimpleNamespace(prevalidation=None, source="canon@abc:mecanique.yaml")
        with self.assertRaises(prevalidation.PrevalidationError) as caught:
            self._parameters(document)
        self.assertEqual(caught.exception.cause, "block-missing")
        self.assertIn("block-missing", prevalidation.CAUSES)

    def test_present_block_is_returned(self):
        block = mechanics.Prevalidation(classes=("P0", "P1"),
                                        contracts=("tests/parts/toujours.txt",),
                                        hosts=("h1",), timeout_seconds=60)
        document = SimpleNamespace(prevalidation=block, source="canon@abc:mecanique.yaml")
        got_document, got = self._parameters(document)
        self.assertIs(got_document, document)
        self.assertIs(got, block)


if __name__ == "__main__":
    unittest.main()
