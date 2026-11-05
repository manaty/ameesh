# SPDX-License-Identifier: AGPL-3.0-only
"""#5965 : un journal des tours illisible se distingue d'une famille absente."""
import unittest
from unittest import mock

from ameesh import review_authors, review_lot

FAMILIES = ("claude", "codex", "deepseek")


class ActorFamilyJournalTest(unittest.TestCase):
    def broken(self):
        return mock.patch.object(review_lot, "_turns_harness",
                                 side_effect=RuntimeError("disk I/O error"))

    def test_default_keeps_unknown_family(self):
        with self.broken():
            self.assertEqual(
                review_authors.actor_family(None, "a", 10.0, FAMILIES), "")

    def test_strict_names_the_unreadable_journal(self):
        with self.broken(), self.assertRaisesRegex(
                review_authors.JournalUnreadable, "a: disk I/O error"):
            review_authors.actor_family(None, "a", 10.0, FAMILIES, strict=True)

    def test_strict_absent_family_stays_empty(self):
        with mock.patch.object(review_lot, "_turns_harness", return_value=""):
            self.assertEqual(review_authors.actor_family(
                None, "a", 10.0, FAMILIES, strict=True), "")


if __name__ == "__main__":
    unittest.main()
