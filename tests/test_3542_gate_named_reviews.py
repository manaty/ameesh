# SPDX-License-Identifier: AGPL-3.0-only
"""#3542 — une règle `train_reviews` qui nomme le chemin fait foi.

Décision de l'auditeur (#138293, 25/10) : quand une règle `train_reviews` du
projet nomme un chemin, son nombre d'avis fait foi ; `CRITICAL_SCOPE` ne reste
qu'un plancher pour les chemins qu'aucune règle ne nomme, et un motif
attrape-tout (`**`) ne l'abaisse jamais.

Témoins : les 9 chemins de #3529 (4 `deploy/**` que le canon met à 1, les 5
autres hors périmètre critique) demandent un avis ; un chemin hors canon qui
correspond à `CRITICAL_SCOPE` en demande deux ; une règle à 2 en demande deux ;
`**` ne baisse pas un chemin critique non nommé.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import lot_class, merge_queue

#: les 9 chemins du gel 428dd05a5887 de #3529, celui que la porte mettait à 2
PATHS_3529 = (
    "deploy/nexlink-release-config.example.json",
    "deploy/systemd/ameesh-release-nexlink.service",
    "deploy/systemd/ameesh-release-nexlink.timer",
    "deploy/systemd/ameesh-release.service",
    "docs/EXPLOITATION.md",
    "src/ameesh/nexlink_release.py",
    "src/ameesh/release.py",
    "src/ameesh/release_cutoff.py",
    "tests/test_3529_nexlink_release.py",
)


def canon(*, rules=(), risk_rules=(), default="light"):
    """Un canon en mémoire pour `ameesh` : règles de classe et d'avis."""
    return types.SimpleNamespace(root="fixture", sources=[], federation={
        "review_policies": {
            "risk_classes": {"projects": {"ameesh": {
                "default": default, "rules": list(risk_rules)}}},
            "train_reviews": {
                "schema": "ameesh-train-reviews-policy/1",
                "projects": {"ameesh": {"rules": list(rules)}},
            },
        }})


class NamedReviewGateTest(unittest.TestCase):
    def test_the_nine_paths_of_3529_need_one_review(self):
        source = canon(
            risk_rules=[{"paths": ["deploy/**"], "class": "sensitive"}],
            rules=[{"paths": ["deploy/**"], "reviews": 1}])
        # le canon réel déclare `reviews.light_reviews: 1` (mécanique) pour
        # les 5 chemins hors périmètre critique ; la fixture n'a pas de source
        # Git, on lit le même plancher.
        with mock.patch.object(lot_class, "_light_reviews", return_value=(1, "")):
            klass = lot_class.class_of_paths([source], "ameesh", PATHS_3529)
        self.assertFalse(klass["doubt"])
        self.assertTrue(merge_queue.critical_scope(klass))
        self.assertEqual(klass["train_reviews"], 1)
        self.assertEqual(klass["train_reviews_paths"],
                         {path: 1 for path in PATHS_3529[:4]})
        self.assertEqual(klass["train_reviews_wide"], 0)
        self.assertEqual(merge_queue.required_verdicts(klass), 1)

    def test_a_named_critical_path_at_one_needs_one_review(self):
        for path in ("pyproject.toml", "deploy/bascule.py",
                     "src/ameesh/host_gate.py"):
            with self.subTest(path=path):
                source = canon(default="normal",
                               rules=[{"paths": [path], "reviews": 1}])
                klass = lot_class.class_of_paths([source], "ameesh", [path])
                self.assertTrue(merge_queue.critical_scope(klass), path)
                self.assertEqual(klass["train_reviews_paths"], {path: 1})
                self.assertEqual(merge_queue.required_verdicts(klass), 1)

    def test_an_unnamed_critical_path_keeps_two_reviews(self):
        # `scripts/deploy*` n'est nommé par aucune règle : le plancher reste.
        path = "scripts/deploy-cutoff.sh"
        source = canon(default="normal",
                       rules=[{"paths": ["src/ameesh/**"], "reviews": 1}])
        klass = lot_class.class_of_paths([source], "ameesh", [path])
        self.assertTrue(merge_queue.critical_scope(klass))
        self.assertNotIn(path, klass["train_reviews_paths"])
        self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_a_named_path_with_two_reviews_keeps_two(self):
        path = "src/ameesh/db.py"
        source = canon(default="normal",
                       rules=[{"paths": [path], "reviews": 2}])
        klass = lot_class.class_of_paths([source], "ameesh", [path])
        self.assertEqual(klass["train_reviews_paths"], {path: 2})
        self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_a_catch_all_does_not_lower_a_critical_path(self):
        # `**` couvre tout sans nommer : il relève le compte, jamais il
        # n'abaisse le plancher critique d'un chemin qu'aucune règle ne nomme.
        path = "scripts/deploy-cutoff.sh"
        for pattern in ("**", "**/**", "/**"):
            with self.subTest(pattern=pattern):
                source = canon(default="normal",
                               rules=[{"paths": [pattern], "reviews": 1}])
                klass = lot_class.class_of_paths([source], "ameesh", [path])
                self.assertTrue(merge_queue.critical_scope(klass))
                self.assertEqual(klass["train_reviews_paths"], {})
                self.assertEqual(klass["train_reviews_wide"], 1)
                self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_a_catch_all_still_raises_a_named_path(self):
        # Le maximum des règles applicables reste le nombre d'avis ; seule
        # l'abaissement du plancher critique par `**` est interdit.
        path = "deploy/bascule.py"
        source = canon(rules=[{"paths": ["deploy/**"], "reviews": 1},
                              {"paths": ["**"], "reviews": 2}])
        klass = lot_class.class_of_paths([source], "ameesh", [path])
        self.assertEqual(klass["train_reviews_paths"], {path: 1})
        self.assertEqual(klass["train_reviews_wide"], 2)
        self.assertEqual(merge_queue.required_verdicts(klass), 2)


if __name__ == "__main__":
    unittest.main()
