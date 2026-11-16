# SPDX-License-Identifier: AGPL-3.0-only
"""#7555 — le fetch de la pointe review/* suit le budget de la lecture.

Constat de la revue de #7539 (7f647c5e) : `_on_own_review_branch` appelait
`_forge_fetch(repo, tip)` sans échéance. Le fetch retombait donc sur
`FETCH_TIMEOUT` (180 s) alors que `_rewritten_after_freeze` se donne 60 s hors
balayage, comme les deux autres fetch de la même lecture (budget `remaining`).
Le budget doit traverser `_on_own_review_branch` jusqu'à `_forge_fetch` ; un
budget déjà épuisé ne lance rien.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import review_authors


class ReviewBranchFetchBudgetTest(unittest.TestCase):
    REPO = "/repo"
    SHA = "b" * 40
    TIP = "a" * 40

    def call(self, *, remaining=None):
        """Une lecture de branche review/* qui ne trouve que la pointe distante."""
        def read_out(repo, *args):
            if args[0] == "for-each-ref":
                return ""                   # aucune ref locale
            if args[0] == "ls-remote":
                return "%s\trefs/heads/review/cx3-42\n" % self.TIP
            raise AssertionError(args)

        with mock.patch.object(review_authors, "_read_out", side_effect=read_out), \
                mock.patch.object(review_authors.plan_git, "forge_remote",
                                  return_value="https://forge.example/repo.git"), \
                mock.patch.object(review_authors, "_forge_fetch",
                                  return_value=False) as fetch:
            found = review_authors._on_own_review_branch(
                self.REPO, "cx3", 42, self.SHA, fetch=True, remaining=remaining)
        return found, fetch

    def test_fetch_is_bounded_by_the_caller_budget(self):
        found, fetch = self.call(remaining=lambda: 7.0)
        self.assertFalse(found)
        fetch.assert_called_once_with(self.REPO, self.TIP, timeout=7.0)

    def test_an_expired_budget_is_passed_as_zero(self):
        found, fetch = self.call(remaining=lambda: 0.0)
        self.assertFalse(found)
        fetch.assert_called_once_with(self.REPO, self.TIP, timeout=0.0)

    def test_without_a_budget_the_default_is_kept(self):
        found, fetch = self.call()
        self.assertFalse(found)
        fetch.assert_called_once_with(self.REPO, self.TIP, timeout=None)


if __name__ == "__main__":
    unittest.main()
