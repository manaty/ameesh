# SPDX-License-Identifier: AGPL-3.0-only
"""#2726 : la porte lit les auteurs de la plage et les drapeaux compilés.

Témoins de la ligne 2 du panier #2681 : `reviews.exclude_author_family` rend
la règle stricte même à un avis, `reviews.distinct_families` décide de la
double famille au compte 2 ; le lecteur de classe porte les deux drapeaux.
"""

from __future__ import annotations

import types
from unittest import TestCase, mock

from ameesh import lot_class, mechanics, merge_queue

SHA = "a" * 40
BASE = "b" * 40


def mark(number: int, actor: str) -> dict:
    return {"kind": "verdict", "id": number, "actor": actor, "verdict": "ok",
            "sha": SHA, "at_ts": 100.0 + number}


def canon_fixture() -> types.SimpleNamespace:
    source = types.SimpleNamespace(mode="git", repo="fixture", commit="c" * 40,
                                   prefix="")
    return types.SimpleNamespace(root="fixture", sources=[source], federation={
        "review_policies": {"risk_classes": {"projects": {"ameesh": {
            "default": "light", "rules": []}}}}})


class GateFlagsTest(TestCase):
    FAMILIES = {"author": "deepseek", "r1": "deepseek", "r2": "codex",
                "r3": "codex"}

    def gate(self, marks, required, klass):
        # `db=None` : la plage n'est pas lue, seuls les drapeaux et les avis
        # comptent (le lecteur de plage a ses témoins #2739/#2681).
        return merge_queue.review_gate(
            None, marks, SHA, klass=klass, item={"assignee": "author"},
            required=required,
            family_of=lambda name: self.FAMILIES.get(str(name or ""), ""))

    def test_exclude_author_family_applies_even_at_one_review(self):
        klass = {"class": "normal", "base_sha": BASE}
        loose = self.gate([mark(1, "r1")], 1,
                          dict(klass, exclude_author_family=False))
        self.assertTrue(loose["ok"], loose["why"])
        strict = self.gate([mark(1, "r1")], 1,
                           dict(klass, exclude_author_family=True))
        self.assertFalse(strict["ok"], strict)
        self.assertEqual(strict["accepted"], [])
        self.assertTrue(strict["exclude_author_family"])

    def test_distinct_families_drives_the_required_two(self):
        klass = {"class": "normal", "base_sha": BASE}
        marks = [mark(1, "r2"), mark(2, "r3")]
        loose = self.gate(marks, 2, dict(klass, distinct_families=False))
        self.assertTrue(loose["ok"], loose["why"])
        self.assertEqual([entry["actor"] for entry in loose["accepted"]],
                         ["r2", "r3"])
        strict = self.gate(marks, 2, dict(klass, distinct_families=True))
        self.assertFalse(strict["ok"], strict)
        self.assertEqual([entry["actor"] for entry in strict["accepted"]],
                         ["r2"])


class ClassFlagsTest(TestCase):
    def test_distinct_families_keeps_the_strictest_canon_in_either_order(self):
        for flags in ((True, False), (False, True), (False, False)):
            with self.subTest(flags=flags), \
                    mock.patch.object(mechanics, "light_reviews_at", return_value=0), \
                    mock.patch.object(mechanics, "review_flags_at",
                                      side_effect=[(False, flag) for flag in flags]):
                klass = lot_class.class_of_paths(
                    [canon_fixture(), canon_fixture()], "ameesh", ["docs/x.md"])
            self.assertFalse(klass["doubt"], klass)
            self.assertEqual(klass["distinct_families"], any(flags))

    def test_the_class_reader_carries_the_compiled_flags(self):
        with mock.patch.object(mechanics, "light_reviews_at", return_value=0), \
                mock.patch.object(mechanics, "review_flags_at",
                                  return_value=(True, False)):
            klass = lot_class.class_of_paths([canon_fixture()], "ameesh",
                                             ["docs/x.md"])
        self.assertFalse(klass["doubt"], klass)
        self.assertTrue(klass["exclude_author_family"])
        self.assertFalse(klass["distinct_families"])

    def test_an_unreadable_flag_reader_is_a_doubt(self):
        with mock.patch.object(mechanics, "light_reviews_at", return_value=0), \
                mock.patch.object(mechanics, "review_flags_at",
                                  side_effect=mechanics.MechanicsError("bad doc")):
            klass = lot_class.class_of_paths([canon_fixture()], "ameesh",
                                             ["docs/x.md"])
        self.assertTrue(klass["doubt"])
        self.assertTrue(klass["exclude_author_family"])
        self.assertTrue(klass["distinct_families"])
