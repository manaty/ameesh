# SPDX-License-Identifier: AGPL-3.0-only
"""#3562 — les motifs génériques sans `**` ne nomment aucun chemin.

Suite de #3542 : `is_catch_all` exigeait `**`, donc `*` et `*/*` restaient
nommants. Une règle `*/*` à 1 sur `deploy/x.sh` faisait alors tomber le
plancher critique de 2 à 1. Un motif non vide sans littéral (`*`, `*/*`, `?`,
`**`…) est une portée générique : il peut relever le compte s'il matche un
chemin du diff, jamais nommer ce chemin ni abaisser le plancher critique.
"""
from __future__ import annotations

import types
import unittest

from ameesh import lot_class, merge_queue, review


def canon(*, rules=()):
    """Un canon en mémoire pour `ameesh` : politique de classe et d'avis."""
    return types.SimpleNamespace(root="fixture", sources=[], federation={
        "review_policies": {
            "risk_classes": {"projects": {"ameesh": {
                "default": "normal", "rules": []}}},
            "train_reviews": {
                "schema": "ameesh-train-reviews-policy/1",
                "projects": {"ameesh": {"rules": list(rules)}}},
        }})


class GenericPatternTest(unittest.TestCase):
    def test_patterns_without_literal_are_generic(self):
        for pattern in ("*", "*/*", "?", "??", "*/?", "**", "**/*",
                        "***", "*/**", "*/*/**", "**?"):
            with self.subTest(pattern=pattern):
                self.assertTrue(review.is_catch_all(pattern))

    def test_patterns_with_a_literal_still_name(self):
        for pattern in ("deploy/**", "src/ameesh/**", "**/*.sql", "*.py",
                        "docs/*", "Dockerfile", "", "/"):
            with self.subTest(pattern=pattern):
                self.assertFalse(review.is_catch_all(pattern))

    def test_declared_reviews_moves_star_patterns_to_wide(self):
        # Témoin exact dev2 : `*/*` matche `deploy/x.sh` sans le nommer.
        self.assertEqual(
            review.declared_reviews([(["*/*"], 1)], ["deploy/x.sh"]), ({}, 1))
        # `*` matche un fichier à la racine de la même façon.
        self.assertEqual(
            review.declared_reviews([(["*"], 1)], ["pyproject.toml"]), ({}, 1))

    def test_a_star_rule_cannot_lower_a_root_critical_path(self):
        source = canon(rules=[{"paths": ["*"], "reviews": 1}])
        klass = lot_class.class_of_paths([source], "ameesh", ["pyproject.toml"])
        self.assertTrue(merge_queue.critical_scope(klass))
        self.assertEqual(klass["train_reviews_paths"], {})
        self.assertEqual(klass["train_reviews_wide"], 1)
        self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_a_star_slash_star_rule_cannot_lower_a_nested_critical_path(self):
        source = canon(rules=[{"paths": ["*/*"], "reviews": 1}])
        klass = lot_class.class_of_paths([source], "ameesh", ["deploy/x.sh"])
        self.assertTrue(merge_queue.critical_scope(klass))
        self.assertEqual(klass["train_reviews_paths"], {})
        self.assertEqual(klass["train_reviews_wide"], 1)
        self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_an_applicable_star_rule_still_raises_the_count(self):
        source = canon(rules=[{"paths": ["*"], "reviews": 2}])
        klass = lot_class.class_of_paths([source], "ameesh", ["notes.txt"])
        self.assertFalse(merge_queue.critical_scope(klass))
        self.assertEqual(klass["train_reviews_paths"], {})
        self.assertEqual(klass["train_reviews_wide"], 2)
        self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_a_literal_rule_still_names_the_path(self):
        source = canon(rules=[{"paths": ["deploy/**"], "reviews": 1}])
        klass = lot_class.class_of_paths([source], "ameesh", ["deploy/x.sh"])
        self.assertTrue(merge_queue.critical_scope(klass))
        self.assertEqual(klass["train_reviews_paths"], {"deploy/x.sh": 1})
        self.assertEqual(klass["train_reviews_wide"], 0)
        self.assertEqual(merge_queue.required_verdicts(klass), 1)


if __name__ == "__main__":
    unittest.main()
