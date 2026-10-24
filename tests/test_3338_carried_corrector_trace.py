# SPDX-License-Identifier: AGPL-3.0-only
"""#3338 — la trace « correcteur » d'un gel antérieur suit la correction qui survit.

Relecture de #3335 (gel 5e6966e1) : `_carried_from_previous` exigeait
`every=True` pour la trace « correcteur » d'un gel antérieur, alors que la
lecture de la plage courante (`_surviving_review_commits`) garde l'acteur dès
qu'un seul de ses correctifs survit. Une correction qui survit suffit donc à
garder la trace ; quand aucune ne survit, elle tombe ; une lecture impossible
la garde (le doute ne retire jamais une exclusion).

L'exclusion de famille est portée par les auteurs (#2888) : ces témoins
vérifient qu'elle ne dépend pas de cette trace.
"""
from __future__ import annotations

import os

from .test_2924_history_rewrite_provenance import _RewriteFixture


class _PartialPriorCorrectorFixture(_RewriteFixture):
    """Gel antérieur A avec deux correctifs de cx3 ; la tête B n'en garde qu'un."""

    def rewrite_keeping(self, keep: str) -> str:
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("src/ameesh/runner.py", "base\nlot\n" + keep)
        witness = os.path.join(self.repo.root, "tests", "test_runner.py")
        if os.path.exists(witness):
            os.remove(witness)
        head = self.repo.commit("impl: reprend la garde, retire le témoin",
                                agent="impl")
        self.receipt("impl", "deepseek", [head])
        freeze = self.freeze(head)
        self.milestone = {"id": int(freeze["id"]), "sha": head,
                          "actor": "impl", "at_ts": freeze.get("at_ts")}
        return head


class PartlySurvivingPriorCorrectorTest(_PartialPriorCorrectorFixture):
    """Un des deux correctifs antérieurs survit : la trace « correcteur » reste."""

    def setUp(self):
        super().setUp()
        self.head = self.rewrite_keeping(
            "if not admitted:\n    return deferred  # cx3 fix\n")

    def test_one_surviving_correction_keeps_the_corrector_trace(self):
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head], self.base, self.head)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertEqual([row["name"] for row in correctors], ["cx3"])


class NoSurvivingPriorCorrectorTest(_PartialPriorCorrectorFixture):
    """Aucun correctif antérieur ne survit : la trace tombe (témoin inverse)."""

    def setUp(self):
        super().setUp()
        self.head = self.rewrite_keeping("")

    def test_no_surviving_correction_drops_the_trace(self):
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head], self.base, self.head)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek"})
        self.assertEqual(correctors, [])
