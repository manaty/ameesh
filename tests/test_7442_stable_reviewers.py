# SPDX-License-Identifier: AGPL-3.0-only
"""#7442: re-freeze keeps eligible seats, independently from verdict carry."""
from __future__ import annotations

import unittest

from ameesh import review_designation as rd
from tests.test_1617_review_designation import FakePort, FAMILIES, SHA_A, SHA_B


class StableSeatsTest(unittest.TestCase):
    def select(self, candidates, **kw):
        return rd.select(required=2, authors=[{"name": "author", "family": "codex"}],
                         families=FAMILIES, candidates=candidates,
                         preferred=["old-c", "old-d"], **kw)

    def test_previous_seats_win_over_new_load_and_pace(self):
        candidates = [rd.Candidate("old-c", "claude", load=20),
                      rd.Candidate("new-c", "claude"),
                      rd.Candidate("old-d", "deepseek", load=15),
                      rd.Candidate("new-d", "deepseek")]
        out = self.select(candidates, advances={"claude": 50, "deepseek": -10})
        self.assertEqual({r["name"] for r in out["reviewers"]}, {"old-c", "old-d"})
        self.assertEqual(out["missing"], 0)

    def test_unavailable_previous_seat_is_replaced(self):
        out = self.select([rd.Candidate("old-c", "claude", available=False),
                           rd.Candidate("new-c", "claude"),
                           rd.Candidate("old-d", "deepseek")])
        self.assertEqual({r["name"] for r in out["reviewers"]}, {"new-c", "old-d"})

    def test_previous_reviewer_who_authored_a_fix_is_not_kept(self):
        out = self.select([rd.Candidate("old-c", "claude"),
                           rd.Candidate("new-c", "claude"),
                           rd.Candidate("old-d", "deepseek")],
                          fixers=[{"name": "old-c", "family": "claude"}])
        self.assertEqual(out["state"], rd.WAITING)
        self.assertEqual(out["reviewers"], [{"name": "old-d", "family": "deepseek"}])

    def test_changed_live_family_cannot_duplicate_another_seat(self):
        out = self.select([rd.Candidate("old-c", "deepseek"),
                           rd.Candidate("old-d", "deepseek"),
                           rd.Candidate("new-c", "claude")])
        self.assertEqual({r["family"] for r in out["reviewers"]}, {"claude", "deepseek"})

    def test_explicit_manual_seat_is_not_displaced(self):
        out = self.select([rd.Candidate("old-c", "claude"),
                           rd.Candidate("old-d", "deepseek")],
                          already=[{"name": "manual", "family": "claude"}])
        self.assertEqual(out["manual"], [{"name": "manual", "family": "claude"}])
        self.assertEqual(out["reviewers"], [{"name": "old-d", "family": "deepseek"}])


class RefreezeRequestTest(unittest.TestCase):
    def context(self):
        return {"freeze": {"id": 1, "sha": SHA_A},
                "reviewers": [{"name": "old-c", "family": "claude"}],
                "reviewed": ["old-c"],
                "blocked": [{"id": 7, "actor": "old-c", "note": "B: wrong boundary"}],
                "note": "Boundary fixed; targeted tests green."}

    def apply(self, port):
        return rd.apply({"id": 9, "title": "Fix", "reviewers": ["old-c"]},
                        policy=(), paths=["src/ameesh/thing.py"], required=1,
                        port=port, freeze_sha=SHA_B, base="c" * 40,
                        authors=[{"name": "author", "family": "codex"}],
                        candidates=[rd.Candidate("old-c", "claude", load=20),
                                    rd.Candidate("new-c", "claude")],
                        families=FAMILIES)

    def test_new_head_gets_new_request_with_delta_blocker_and_author_note(self):
        port = FakePort({9: [{"id": 2, "sha": SHA_B}]})
        port.previous_review = lambda *_: self.context()
        out = self.apply(port)
        self.assertEqual(out["reviewers"], [{"name": "old-c", "family": "claude"}])
        self.assertEqual(len(port.messages), 1)
        body = port.messages[0][1]
        self.assertIn("git diff %s..%s" % (SHA_A[:12], SHA_B[:12]), body)
        self.assertIn("Previous B #7 by old-c", body)
        self.assertIn("Boundary fixed; targeted tests green.", body)
        self.assertIn("Your seat is kept", body)
        self.assertIn("--sha " + SHA_B, body)
        self.apply(port)
        self.assertEqual(len(port.messages), 1)

    def test_replacement_gets_full_review_instructions(self):
        body = rd.reviewer_message({"id": 9}, freeze={"id": 2, "sha": SHA_B},
                                   required=1, rank=1, reviewer={"name": "new-c"},
                                   base="c" * 40, previous_review=self.context())
        self.assertIn("review the full diff above", body)
        self.assertNotIn("Your seat is kept", body)

    def test_a_pending_seat_still_owes_the_full_review(self):
        context = dict(self.context(), reviewed=[])
        body = rd.reviewer_message({"id": 9}, freeze={"id": 2, "sha": SHA_B},
                                   required=1, rank=1, reviewer={"name": "old-c"},
                                   base="c" * 40, previous_review=context)
        self.assertIn("no prior verdict of yours is recorded", body)
        self.assertIn("review the full diff above", body)

    def test_partial_waiting_keeps_only_current_requests_without_duplicate(self):
        port = FakePort({9: [{"id": 2, "sha": SHA_B}]})
        port.previous_review = lambda *_: self.context()
        port.records[(9, 2)] = {"state": rd.WAITING, "required": 2,
                                "reviewers": [{"name": "new-c", "family": "claude"}],
                                "missing": 1}
        self.apply(port)
        self.assertEqual(port.messages, [])


if __name__ == "__main__":
    unittest.main()
