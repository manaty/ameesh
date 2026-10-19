# SPDX-License-Identifier: AGPL-3.0-only
"""#2664 : les familles de modèle se lisent au canon compilé, jamais en local.

`review_lot.compiled_families` lit `processus/mecanique.yaml` au **dernier
commit audité de l'hôte** (`canon_state.last_good_commit`, #2447 B2), même si
le passage courant est invalide ; sans état, le commit exact de la source est
le seul connu. Sans aucun commit audité lisible, la porte échoue **fermée**
avec le canon nommé, et `review_provenance.capture` refuse la preuve. Aucune
liste locale `FAMILIES` ne subsiste dans les deux modules (la garde #2449 le
vérifie aussi).
"""
from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import canon, merge_queue, review_lot, review_provenance, storage

from .test_l1616_mechanics import MECHANICS, _Repo

DEFAULT_FAMILIES = ("claude", "codex", "deepseek")


class _Store:
    """La lecture `storage.of(db).canon.state(host, key)` d'un bac sans base."""

    def __init__(self, state):
        self.canon = SimpleNamespace(state=lambda host, key: state)


class FamiliesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = _Repo(self.tmp.name)
        self.first = self.repo.git("rev-parse", "HEAD")
        # un second commit NON audité, avec une politique différente : il ne
        # doit pas fournir les familles tant que `last_good_commit` est le
        # premier.
        self.repo.write("processus/mecanique.yaml",
                        MECHANICS.replace("families: [claude, codex, deepseek]",
                                          "families: [claude, codex]"))
        self.second = self.repo.commit("familles réduites")
        self.cfg = SimpleNamespace(canon=self.repo.root, canon_ref=self.second,
                                   canon_untrusted=False, host="h",
                                   canon_entries=None)
        self.source = SimpleNamespace(mode="git", repo=self.repo.root,
                                      prefix="", commit=self.second)
        self.canon = SimpleNamespace(root=self.repo.root, is_default=True,
                                     id_source="federation", id="manaty",
                                     sources=[self.source])
        self.load = mock.patch.object(canon, "load_configured",
                                      return_value=[self.canon])
        self.load.start()
        self.addCleanup(self.load.stop)
        review_lot._FAMILIES_CACHE.clear()
        self.addCleanup(review_lot._FAMILIES_CACHE.clear)

    def families(self, state):
        with mock.patch.object(storage, "of", return_value=_Store(state)):
            return review_lot.compiled_families(None, self.cfg)

    def test_reads_the_last_audited_commit_not_the_current_ref(self):
        state = {"root": self.repo.root, "last_good_commit": self.first}
        self.assertEqual(self.families(state), DEFAULT_FAMILIES)

    def test_without_a_recorded_state_the_exact_source_commit_is_read(self):
        self.assertEqual(self.families(None), ("claude", "codex"))

    def test_an_audited_state_without_commit_fails_closed_with_the_canon(self):
        state = {"root": self.repo.root, "last_good_commit": ""}
        with self.assertRaises(review_lot.FamiliesError) as caught:
            self.families(state)
        self.assertIn(self.repo.root, str(caught.exception))

    def test_a_state_for_another_root_does_not_fall_back_to_the_ref(self):
        state = {"root": "/un/autre/canon", "last_good_commit": self.first}
        with self.assertRaises(review_lot.FamiliesError) as caught:
            self.families(state)
        self.assertIn(self.repo.root, str(caught.exception))

    def test_a_family_outside_the_canon_is_unknown(self):
        self.assertEqual(review_lot.family_of("gemini-pro", DEFAULT_FAMILIES), "")
        self.assertEqual(review_lot.family_of("claude-opus", DEFAULT_FAMILIES), "claude")
        self.assertEqual(review_lot.reviewer_family("deepseek", DEFAULT_FAMILIES), "codex")
        # une famille opposée non déclarée n'est pas proposée
        self.assertEqual(review_lot.reviewer_family("claude", ("claude", "deepseek")),
                         review_lot.UNKNOWN_FAMILY)
        self.assertEqual(review_lot.review_families("", DEFAULT_FAMILIES),
                         ("codex", "claude"))

    def test_the_gate_fails_closed_with_the_named_canon(self):
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/merge_queue.py",
                            "class": "sensitive", "rule": None}]}
        error = review_lot.FamiliesError(
            "%s: no audited commit readable" % self.repo.root)
        with mock.patch.object(review_lot, "compiled_families", side_effect=error):
            gate = merge_queue.review_gate(
                object(), [], "a" * 40, klass=klass, item={"assignee": "author"})
        self.assertFalse(gate["ok"])
        self.assertIn(self.repo.root, gate["error"])
        self.assertIn("model families cannot be read", gate["why"])

    def test_provenance_accepts_only_the_compiled_families(self):
        env = {"AGENT_MAIL_NAME": "a", "AMEESH_TURN_ID": "t" * 32,
               "AMEESH_HARNESS": "deepseek"}
        self.assertEqual(review_provenance.capture(env, families=DEFAULT_FAMILIES),
                         {"actor": "a", "turn": "t" * 32, "harness": "deepseek"})
        with self.assertRaises(review_provenance.ProvenanceError):
            review_provenance.capture(env, families=("claude", "codex"))
        with self.assertRaises(review_provenance.ProvenanceError):
            review_provenance.capture(env, families=())

    def test_the_two_historical_exemptions_are_gone(self):
        import importlib.util
        import pathlib
        import sys

        root = pathlib.Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "garde_2664", root / "scripts" / "garde-mecanique.py")
        garde = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = garde
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(garde)
        converted = {("src/ameesh/review_lot.py", "reviews.families"),
                     ("src/ameesh/review_provenance.py", "reviews.families")}
        self.assertFalse({(item.path, item.parameter) for item in garde.EXEMPTIONS}
                         & converted)
        scan = garde.scan_repository(root)
        self.assertEqual(list(scan.findings), [])
        self.assertFalse(scan.unused)


if __name__ == "__main__":
    unittest.main()
