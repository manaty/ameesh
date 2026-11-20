# SPDX-License-Identifier: AGPL-3.0-only
"""#2696 — la porte lit le canon compilé, y compris pour la priorité.

Le pilote a tranché la forme (b) : le classement lit `train_reviews` du canon
(`federation.yaml`, `review_policies.train_reviews`) et la porte en dérive le
même nombre ; les motifs de secours de `CRITICAL_SCOPE` ne sont pas élargis.
Le témoin porte sur les chemins réels du canon (`src/ameesh/train*.py`,
`radar.py`, `plan_git.py`, `resolutions.py`) et sur un chemin déclaré au canon
qui n'est dans aucune liste de secours, pour prouver que la source est bien le
canon. Un seul avis ne doit ni ouvrir la porte ni rendre le lot prêt.

Témoin identité (auditeur, 19/10) : `src/ameesh/{oidc,login,login_cli}.py` sont
déclarés à deux avis par la règle d'identité (`#2629`, `#2631`) et aucun n'est
dans `CRITICAL_SCOPE` ; la porte ne peut donc les compter que par le canon.

Second cas (mesh-design, 19/10) : le repli ne doit pas surclasser le canon d'un
autre projet. Le crate Nexlink `desktop/host/host-linux` est déclaré à un avis
par `desktop/**` ; `host[-_]` ne doit pas en faire un chemin de prod. Les motifs
ameesh sont ancrés sous `src/ameesh/` et le repli ne s'applique qu'à ameesh ;
les témoins couvrent Nexlink=1, `resolutions.py`=2 et le repli ameesh conservé.
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
IDENTITY_PATHS = (
    "src/ameesh/oidc.py",
    "src/ameesh/login.py",
    "src/ameesh/login_cli.py",
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
        # `mail.py` est hors du périmètre critique ; `work.py` y est entré
        # avec #1515 (il participe à la porte des avis).
        source = canon(reviews=[{"paths": list(TRAIN_PATHS), "reviews": 2}])
        klass = lot_class.class_of_paths([source], "ameesh",
                                         ["src/ameesh/mail.py"])
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

    def test_identity_declared_by_the_canon_needs_two_reviews(self):
        # Auditeur 19/10 : l'identité est déclarée dans la même politique ;
        # `oidc.py` n'est dans aucune liste de secours, donc seul le canon
        # peut exiger les deux avis.
        source = canon(reviews=[{"paths": list(IDENTITY_PATHS), "reviews": 2}])
        for path in IDENTITY_PATHS:
            with self.subTest(path=path):
                klass = lot_class.class_of_paths([source], "ameesh", [path])
                self.assertFalse(klass["doubt"])
                self.assertEqual(klass["class"], "sensitive")
                self.assertEqual(klass["train_reviews"], 2)
                self.assertFalse(merge_queue.critical_scope(klass), path)
                self.assertEqual(merge_queue.required_verdicts(klass), 2, path)

    def test_an_unreadable_train_declaration_is_a_doubt(self):
        # Une règle illisible ne se lit pas comme un zéro rassurant.
        source = canon(reviews=[{"paths": list(TRAIN_PATHS), "reviews": 3}])
        klass = lot_class.class_of_paths([source], "ameesh",
                                         ["src/ameesh/resolutions.py"])
        self.assertTrue(klass["doubt"])
        self.assertEqual(klass["class"], "sensitive")


class CanonPriorityTest(unittest.TestCase):
    """#2696, second cas : le repli ne surclasse pas le canon d'un autre projet
    et reste borné aux chemins d'ameesh."""

    NX_HOST = "desktop/host/crates/host-linux/src/lib.rs"
    NX_DEPLOY = "deploy/foo.sh"

    def nx_canon(self):
        return canon(
            rules=[{"paths": ["desktop/**", "deploy/**"], "class": "sensitive"}],
            reviews=[{"paths": ["desktop/**", "deploy/**"], "reviews": 1}],
            risk_project="nexlink", train_project="nexlink")

    def test_the_nxlink_host_crate_keeps_the_canon_one(self):
        klass = lot_class.class_of_paths([self.nx_canon()], "nexlink", [self.NX_HOST])
        self.assertFalse(klass["doubt"])
        self.assertEqual(klass["class"], "sensitive")
        self.assertEqual(klass["project"], "nexlink")
        self.assertEqual(klass["train_reviews"], 1)
        self.assertFalse(merge_queue.critical_scope(klass))
        self.assertEqual(merge_queue.required_verdicts(klass), 1)

    def test_the_generic_fallback_is_skipped_for_another_project(self):
        # Le motif `deploy` reste global, mais le repli ne s'applique qu'à
        # ameesh : la politique Nexlink (un avis) fait foi.
        klass = lot_class.class_of_paths([self.nx_canon()], "nexlink", [self.NX_DEPLOY])
        self.assertEqual(klass["class"], "sensitive")
        self.assertTrue(merge_queue.critical_scope(klass))
        self.assertEqual(merge_queue.required_verdicts(klass), 1)

    def test_the_ameesh_fallback_still_holds(self):
        # Pour ameesh, un chemin critique que le canon ne déclare pas garde le
        # minimum de deux avis.
        path = "src/ameesh/merge_queue.py"
        source = canon(rules=[{"paths": [path], "class": "sensitive"}])
        klass = lot_class.class_of_paths([source], "ameesh", [path])
        self.assertEqual(klass["train_reviews"], 0)
        self.assertTrue(merge_queue.critical_scope(klass))
        self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_the_ameesh_fallback_holds_in_subpackages(self):
        # Revue dev2 : l'ancrage sous src/ameesh/ vaut à toute profondeur,
        # comme `(^|/)` avant #2696 — l'identité et la garde d'hôte de
        # l'exécuteur médié gardent deux avis.
        for path in ("src/ameesh/mediated_executor/identity.py",
                     "src/ameesh/mediated_executor/host_gate.py"):
            with self.subTest(path=path):
                source = canon(rules=[{"paths": [path], "class": "sensitive"}])
                klass = lot_class.class_of_paths([source], "ameesh", [path])
                self.assertTrue(merge_queue.critical_scope(klass))
                self.assertEqual(merge_queue.required_verdicts(klass), 2)

    def test_an_ameesh_test_name_is_not_a_host_path(self):
        # Le motif `host[-_]` est ancré sous src/ameesh/ : un fichier de test
        # `test_host_gate.py` n'est pas un chemin de prod.
        path = "tests/test_host_gate.py"
        source = canon(rules=[{"paths": ["tests/**"], "class": "light"}])
        klass = lot_class.class_of_paths([source], "ameesh", [path])
        self.assertEqual(klass["class"], "light")
        self.assertFalse(merge_queue.critical_scope(klass))
        self.assertEqual(merge_queue.required_verdicts(klass), 0)


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

    def test_one_identity_verdict_does_not_open_the_gate(self):
        # Le témoin identité va jusqu'à la porte : un avis ne rend pas prêt.
        source = canon(reviews=[{"paths": list(IDENTITY_PATHS), "reviews": 2}])
        klass = lot_class.class_of_paths([source], "ameesh",
                                         ["src/ameesh/oidc.py"])
        gate = self.gate(klass, "reviewer")
        self.assertEqual(gate["required"], 2)
        self.assertFalse(gate["ok"], gate["why"])
        self.assertEqual(len(gate["accepted"]), 1)

    def test_two_distinct_families_open_the_gate(self):
        gate = self.gate(self.klass(), "reviewer", "second")
        self.assertEqual(gate["required"], 2)
        self.assertTrue(gate["ok"], gate["why"])
        self.assertEqual(len(gate["accepted"]), 2)
