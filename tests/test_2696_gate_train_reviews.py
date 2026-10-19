# SPDX-License-Identifier: AGPL-3.0-only
"""#2696 — la porte exige deux avis pour la machinerie des paquets.

Le pilote a tranché la forme (b) : le classement lit `train_reviews` du canon
(`federation.yaml`, `review_policies.train_reviews`) et la porte en dérive le
même nombre ; les motifs de secours de `CRITICAL_SCOPE` ne sont pas élargis.
Le témoin porte sur les chemins réels du canon (`src/ameesh/train*.py`,
`radar.py`, `plan_git.py`, `resolutions.py`) et sur un chemin déclaré au canon
qui n'est dans aucune liste de secours, pour prouver que la source est bien le
canon. Un seul avis ne doit ni ouvrir la porte ni rendre le lot prêt.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import lot_class, merge_queue

HEAD = "a" * 40
TRAIN_PATHS = (
    "src/ameesh/train_policy.py",
    "src/ameesh/radar.py",
    "src/ameesh/plan_git.py",
    "src/ameesh/resolutions.py",
)
FAMILIES = {"author": "deepseek", "reviewer": "claude", "second": "codex"}


def canon(rules=(), reviews=(), risk_project="ameesh", train_project="ameesh"):
    """Un canon en mémoire : risque par défaut sensible, règles train_reviews."""
    return types.SimpleNamespace(root="fixture", sources=[], federation={
        "review_policies": {
            "risk_classes": {"projects": {risk_project: {
                "default": "sensitive", "rules": list(rules)}}},
            "train_reviews": {
                "schema": "ameesh-train-reviews-policy/1",
                "projects": {train_project: {"rules": list(reviews)}},
            },
        }})


class TrainMachineryScaleTest(unittest.TestCase):
    def test_train_machinery_needs_two_reviews(self):
        source = canon(reviews=[{"paths": list(TRAIN_PATHS), "reviews": 2}])
        for path in TRAIN_PATHS:
            with self.subTest(path=path):
                klass = lot_class.class_of_paths([source], "ameesh", [path])
                self.assertFalse(klass["doubt"])
                self.assertEqual(klass["class"], "sensitive")
                self.assertEqual(merge_queue.required_verdicts(klass), 2, path)

    def test_a_control_path_keeps_one_review(self):
        # Le voisin direct de train*.py n'est pas dans la règle : un avis.
        source = canon(reviews=[{"paths": list(TRAIN_PATHS), "reviews": 2}])
        klass = lot_class.class_of_paths([source], "ameesh",
                                         ["src/ameesh/work.py"])
        self.assertFalse(klass["doubt"])
        self.assertEqual(merge_queue.required_verdicts(klass), 1)

    def test_the_declaration_is_read_for_the_lot_project_only(self):
        # Un train_reviews d'un autre projet ne change rien pour ameesh.
        source = canon(reviews=[{"paths": list(TRAIN_PATHS), "reviews": 2}],
                       risk_project="ameesh", train_project="nexlink")
        klass = lot_class.class_of_paths([source], "ameesh",
                                         ["src/ameesh/resolutions.py"])
        self.assertFalse(klass["doubt"])
        self.assertEqual(merge_queue.required_verdicts(klass), 1)

    def test_a_path_declared_only_by_the_canon_needs_two_reviews(self):
        # Forme (b) : la source est le canon, pas une liste de secours
        # (`CRITICAL_SCOPE` ne connaît pas ce chemin).
        path = "src/ameesh/packet_extra.py"
        source = canon(reviews=[{"paths": [path], "reviews": 2}])
        klass = lot_class.class_of_paths([source], "ameesh", [path])
        self.assertFalse(klass["doubt"])
        self.assertFalse(merge_queue.critical_scope(klass))
        self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_an_unreadable_train_declaration_is_a_doubt(self):
        # Une règle illisible ne se lit pas comme un zéro rassurant.
        source = canon(reviews=[{"paths": list(TRAIN_PATHS), "reviews": 3}])
        klass = lot_class.class_of_paths([source], "ameesh",
                                         ["src/ameesh/resolutions.py"])
        self.assertTrue(klass["doubt"])
        self.assertEqual(klass["class"], "sensitive")


class TrainMachineryGateTest(unittest.TestCase):
    def gate(self, klass, *actors):
        marks = [{"kind": "verdict", "verdict": "ok", "sha": HEAD,
                  "actor": actor, "at_ts": index, "id": index}
                 for index, actor in enumerate(actors, 1)]
        with mock.patch.object(merge_queue, "actor_family",
                               side_effect=lambda _db, name, **_: FAMILIES.get(name, "")):
            return merge_queue.review_gate(None, marks, HEAD, klass=klass,
                                           item={"assignee": "author"})

    def klass(self):
        source = canon(reviews=[{"paths": list(TRAIN_PATHS), "reviews": 2}])
        return lot_class.class_of_paths([source], "ameesh",
                                        ["src/ameesh/resolutions.py"])

    def test_one_verdict_does_not_open_the_gate(self):
        gate = self.gate(self.klass(), "reviewer")
        self.assertEqual(gate["required"], 2)
        self.assertFalse(gate["ok"], gate["why"])
        self.assertEqual(len(gate["accepted"]), 1)

    def test_two_distinct_families_open_the_gate(self):
        gate = self.gate(self.klass(), "reviewer", "second")
        self.assertEqual(gate["required"], 2)
        self.assertTrue(gate["ok"], gate["why"])
        self.assertEqual(len(gate["accepted"]), 2)
