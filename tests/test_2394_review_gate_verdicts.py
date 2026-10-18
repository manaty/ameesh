# SPDX-License-Identifier: AGPL-3.0-only
"""#2394 — le barème des avis de la porte suit le canon, sans surcharge.

Canon `processus/lots.md` §2 (allègement owner du 15/10/2026) : un avis d'une
autre famille suffit ; deux familles seulement pour droits SQL, secrets,
paiement et prod réelle. L'application auditeur du 13/10/2026 y ajoute la
**porte de fusion** (checks requis, leurs conditions `if:`, et tout ce qui peut
faire passer un check requis à `skipped` ou `success` sans exécuter les tests)
comme domaine de sécurité, à deux avis. Un doute de classe reste sensible
(§8 : « en cas de doute, le lot est sensible ») : un avis, jamais deux par le
seul doute ni par une liste de chemins vide.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import lot_class, merge_queue

HEAD = "a" * 40
FAMILIES = {"author": "deepseek", "codex": "codex", "claude": "claude",
            "same": "deepseek", "unknown": ""}


def entry(path, cls="sensitive", **extra):
    return dict({"path": path, "class": cls, "rule": [path]}, **extra)


class CanonScaleTest(unittest.TestCase):
    """Le nombre d'avis de chaque classe, sur le barème du canon."""

    def test_light_and_normal(self):
        self.assertEqual(merge_queue.required_verdicts({"class": "light"}), 0)
        self.assertEqual(merge_queue.required_verdicts({"class": "normal"}), 1)

    def test_sensitive_needs_one_review(self):
        for files in ([], [entry("src/ameesh/work.py")],
                      [entry("docs/design/notes.md")]):
            with self.subTest(files=files):
                klass = {"class": "sensitive", "doubt": False, "files": files}
                self.assertEqual(merge_queue.required_verdicts(klass), 1)

    def test_a_doubt_is_sensitive_not_critical(self):
        # §8 : le doute rend la classe sensible ; il ne prouve aucun domaine
        # critique, et un appelant qui laisserait la classe plus basse ne doit
        # pas obtenir zéro avis.
        for cls in ("sensitive", "light", "normal", "release"):
            with self.subTest(cls=cls):
                klass = {"class": cls, "doubt": True, "files": []}
                self.assertEqual(merge_queue.required_verdicts(klass), 1)

    def test_two_reviews_are_reserved_for_critical_paths(self):
        for path in ("src/ameesh/migrations/0460_x.sql", "src/ameesh/db.py",
                     "src/ameesh/authority.py", "src/ameesh/approve_check.py",
                     "src/ameesh/config.py", "deploy/mise-a-jour/poste.sh",
                     "Dockerfile", ".github/workflows/tests.yml",
                     "scripts/test-plan.py", "src/ameesh/merge_queue.py",
                     "src/ameesh/merge_pr.py", "src/ameesh/review.py"):
            with self.subTest(path=path):
                klass = {"class": "sensitive", "files": [entry(path)]}
                self.assertEqual(merge_queue.required_verdicts(klass), 2, path)

    def test_a_critical_path_keeps_two_reviews_whatever_the_class(self):
        # #2265 : le canon peut classer léger un chemin de la porte de fusion ;
        # la porte le voit quand même (deux avis).
        for cls in ("light", "normal", "sensitive"):
            with self.subTest(cls=cls):
                klass = {"class": cls,
                         "files": [entry("src/ameesh/merge_queue.py")]}
                self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_a_declared_critical_path_needs_two_without_a_regex(self):
        klass = {"class": "sensitive",
                 "files": [entry("src/ameesh/new_verifier.py", critical=True)]}
        with mock.patch.object(merge_queue, "CRITICAL_SCOPE", ()):
            self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_empty_or_pathless_files_never_claim_a_critical_domain(self):
        for files in ([], [{"class": "sensitive", "rule": ["src/ameesh/db.py"]}]):
            with self.subTest(files=files):
                klass = {"class": "sensitive", "doubt": True, "files": files}
                self.assertFalse(merge_queue.critical_scope(klass))
                self.assertEqual(merge_queue.required_verdicts(klass), 1)


class ClassReaderTest(unittest.TestCase):
    """Le lecteur de classe et la porte s'accordent sur le canon."""

    def classify(self, paths, rules):
        canon = types.SimpleNamespace(root="fixture", sources=[], federation={
            "review_policies": {"risk_classes": {"projects": {"ameesh": {
                "default": "light", "rules": list(rules)}}}}})
        return lot_class.class_of_paths([canon], "ameesh", paths)

    def test_the_class_reader_and_the_gate_agree(self):
        path = "src/ameesh/new_verifier.py"
        plain = self.classify([path], [{"paths": [path], "class": "sensitive"}])
        self.assertFalse(plain["doubt"])
        self.assertEqual(plain["class"], "sensitive")
        self.assertEqual(merge_queue.required_verdicts(plain), 1)
        critical = self.classify([path], [{"paths": [path], "class": "sensitive",
                                          "critical": True}])
        self.assertFalse(critical["doubt"])
        self.assertEqual(merge_queue.required_verdicts(critical), 2)
        # Une déclaration invalide fait douter de la politique : sensible, un
        # avis, jamais light.
        invalid = self.classify([path], [{"paths": [path], "class": "sensitive",
                                         "critical": "true"}])
        self.assertTrue(invalid["doubt"])
        self.assertEqual(invalid["class"], "sensitive")
        self.assertEqual(merge_queue.required_verdicts(invalid), 1)

    def test_an_unreadable_policy_keeps_the_critical_paths_it_was_given(self):
        # Revue #2394 (dev2) : le doute vient de la politique, pas du diff ; les
        # motifs de secours voient encore SQL et la porte de fusion (2 avis).
        bad = [{"paths": ["x"], "class": "bogus"}]
        for paths, required in ((["src/ameesh/migrations/0460_x.sql"], 2),
                                ([".github/workflows/tests.yml"], 2),
                                (["src/ameesh/work.py"], 1)):
            with self.subTest(paths=paths):
                klass = self.classify(paths, bad)
                self.assertTrue(klass["doubt"])
                self.assertEqual(klass["class"], "sensitive")
                self.assertEqual([f["path"] for f in klass["files"]], paths)
                self.assertEqual(merge_queue.required_verdicts(klass), required)
        undeclared = lot_class.class_of_paths([], "ameesh", ["src/ameesh/db.py"])
        self.assertTrue(undeclared["doubt"])
        self.assertEqual(merge_queue.required_verdicts(undeclared), 2)


class GateTest(unittest.TestCase):
    """La porte applique ce barème : un avis hors doute, deux hors critique."""

    def gate(self, klass, *actors):
        marks = [{"kind": "verdict", "verdict": "ok", "sha": HEAD,
                  "actor": actor, "at_ts": index, "id": index}
                 for index, actor in enumerate(actors, 1)]
        with mock.patch.object(merge_queue, "actor_family",
                               side_effect=lambda _db, name: FAMILIES.get(name, "")):
            return merge_queue.review_gate(None, marks, HEAD, klass=klass,
                                           item={"assignee": "author"})

    def test_a_doubt_accepts_one_independent_family(self):
        for cls in ("sensitive", "light", "normal", "release"):
            with self.subTest(cls=cls):
                klass = {"class": cls, "doubt": True, "files": []}
                self.assertEqual(self.gate(klass)["required"], 1)
                one = self.gate(klass, "codex")
                self.assertEqual(one["class"], "sensitive")
                self.assertTrue(one["ok"], one["why"])
                self.assertFalse(self.gate(klass, "same")["ok"])
                self.assertFalse(self.gate(klass, "unknown")["ok"])
                self.assertFalse(self.gate(klass, "author")["ok"])

    def test_the_merge_gate_keeps_two_distinct_families(self):
        klass = {"class": "sensitive",
                 "files": [entry(".github/workflows/tests.yml")]}
        self.assertEqual(self.gate(klass, "codex")["required"], 2)
        self.assertFalse(self.gate(klass, "codex")["ok"])
        self.assertTrue(self.gate(klass, "codex", "claude")["ok"])


if __name__ == "__main__":
    unittest.main()
