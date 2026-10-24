# SPDX-License-Identifier: AGPL-3.0-only
"""#3416 — avertir le relecteur avant un correctif de premier tour non recevable.

Cas réel #3186 : auteur claude, deux avis requis ; le relecteur codex corrige
au premier tour, sa famille devient auteur, seule deepseek reste éligible. Le
relecteur doit l'apprendre avant de pousser (« §2 : retour à l'auteur »).
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import review_designation as rd
from ameesh import review_designation_activation as activation

FAMILIES = ("claude", "codex", "deepseek")
AUTHORS = [{"name": "ameesh-impl", "family": "claude"}]


def cand(name, family, available=True):
    return rd.Candidate(name=name, family=family, available=available)


CANDIDATES = [cand("ameesh-cx2", "codex"), cand("ameesh-ds1", "deepseek"),
              cand("ameesh-cl3", "claude")]


class CorrectionRoomTest(unittest.TestCase):
    def room(self, **kwargs):
        args = {"required": 2, "corrector": {"name": "ameesh-cx2", "family": "codex"},
                "authors": AUTHORS, "candidates": CANDIDATES, "families": FAMILIES}
        args.update(kwargs)
        return rd.correction_room(**args)

    def test_3186_two_reviews_codex_corrector_is_short(self):
        room = self.room()
        self.assertEqual(room["state"], rd.ROOM_SHORT)
        self.assertEqual(room["missing"], 1)
        self.assertEqual(room["families"], ["deepseek"])
        self.assertTrue(room["why"].startswith(rd.RETURN_TO_AUTHOR))
        self.assertIn("ameesh-cx2", room["why"])

    def test_one_review_leaves_room(self):
        room = self.room(required=1)
        self.assertEqual(room["state"], rd.ROOM_OK)
        self.assertEqual(room["families"], ["deepseek"])

    def test_fourth_family_leaves_room_for_two(self):
        room = self.room(families=FAMILIES + ("gemini",),
                         candidates=CANDIDATES + [cand("ameesh-gm1", "gemini")])
        self.assertEqual(room["state"], rd.ROOM_OK)
        self.assertEqual(room["families"], ["deepseek", "gemini"])

    def test_previous_corrector_counts_too(self):
        # a deepseek reviewer already corrected: a codex correction leaves nothing
        room = self.room(required=1, fixers=[{"name": "ameesh-ds1", "family": "deepseek"}])
        self.assertEqual(room["state"], rd.ROOM_SHORT)
        self.assertEqual(room["families"], [])

    def test_unavailable_family_does_not_count(self):
        candidates = [cand("ameesh-cx2", "codex"), cand("ameesh-ds1", "deepseek",
                                                         available=False)]
        room = self.room(required=1, candidates=candidates)
        self.assertEqual(room["state"], rd.ROOM_SHORT)

    def test_unknown_corrector_family_is_unknown_not_ok(self):
        room = self.room(corrector={"name": "ameesh-x", "family": ""})
        self.assertEqual(room["state"], rd.ROOM_UNKNOWN)

    def test_unknown_author_family_is_unknown(self):
        room = self.room(authors=[{"name": "ameesh-impl", "family": ""}])
        self.assertEqual(room["state"], rd.ROOM_UNKNOWN)

    def test_no_review_required(self):
        self.assertEqual(self.room(required=0)["state"], rd.ROOM_OK)


class CorrectionWarningTest(unittest.TestCase):
    ITEM = {"id": 3186, "assignee": "ameesh-impl", "app": "ameesh"}
    FREEZE = {"id": 18170, "sha": "a" * 40, "at_ts": 0.0, "actor": "ameesh-impl"}

    def warn(self, actor, *, designated=("ameesh-cx2", "ameesh-ds1"), room=None):
        room = room or {"state": rd.ROOM_SHORT, "why": rd.RETURN_TO_AUTHOR + ": x"}
        with mock.patch.object(activation.work, "get", return_value=dict(self.ITEM)), \
                mock.patch.object(activation, "_latest_freeze", return_value=self.FREEZE), \
                mock.patch.object(activation, "_designated_reviewers",
                                  return_value=set(designated)), \
                mock.patch.object(activation, "correction_check",
                                  return_value=room) as check:
            return activation.correction_warning(None, None, 3186, actor), check

    def test_designated_reviewer_short_is_warned_locally(self):
        text, check = self.warn("ameesh-cx2")
        self.assertIn(rd.RETURN_TO_AUTHOR, text)
        self.assertIn("--if-corrector ameesh-cx2", text)
        self.assertEqual(check.call_args.kwargs.get("fetch"), False)

    def test_not_a_designated_reviewer_is_silent(self):
        text, check = self.warn("ameesh-impl")
        self.assertEqual(text, "")
        check.assert_not_called()

    def test_room_ok_is_silent(self):
        text, _ = self.warn("ameesh-cx2", room={"state": rd.ROOM_OK, "why": ""})
        self.assertEqual(text, "")

    def test_read_failure_never_raises(self):
        with mock.patch.object(activation.work, "get", side_effect=RuntimeError("db")):
            self.assertEqual(activation.correction_warning(None, None, 3186, "x"), "")


if __name__ == "__main__":
    unittest.main()
