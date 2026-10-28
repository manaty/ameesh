# SPDX-License-Identifier: AGPL-3.0-only
"""#4325 — le courrier de relecture dit « ne poussez pas » d'emblée.

Signalement de l'orchestrateur Nexlink (28/10) : des relecteurs Claude ont
poussé des correctifs sur des lots sensibles à deux avis, car le courrier
commençait par « fix it yourself and push » et l'exception des familles ne
venait qu'en fin de phrase. Le courrier calcule désormais, pour son
destinataire, la place qu'un correctif laisserait (`review_correction`, le même
calcul que `--if-corrector`) : trop serrée, la consigne s'ouvre sur « ne
poussez pas : rendez le correctif à l'auteur, verdict blocked » ; sinon le
texte est inchangé.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import mail, plan_git, review_correction, work
from ameesh import review_designation as rd

from .test_1617_review_designation import FakePort, LOT, RULES, SHA_A, SHA_C, row

FAMILIES = ("claude", "codex", "deepseek", "gemini")
AUTHORS = [{"name": "mty-amh-impl-02", "family": "deepseek"}]
DO_NOT_PUSH = "do not push this correction: return the fix to the author, verdict blocked"
FIX_YOURSELF = "fix a blocking defect yourself and push"


def first_instruction(text: str) -> str:
    lines = text.splitlines()
    return lines[lines.index("To do:") + 1]


class AutomaticMailTest(unittest.TestCase):

    def apply(self, *pool, paths=("src/ameesh/db.py",)):
        port = FakePort({LOT["id"]: [{"id": 1, "sha": SHA_A}]})
        rd.apply(LOT, policy=RULES, paths=list(paths), port=port, freeze_sha=SHA_A,
                 authors=AUTHORS, risk="sensitive",
                 candidates=rd.candidates_from_registry(pool, families=FAMILIES,
                                                        project="ameesh"),
                 base=SHA_C, policy_ref="canon@deadbeef", notify="mesh-design",
                 families=FAMILIES)
        return dict(port.messages)

    def test_two_opinions_with_a_deepseek_author_say_do_not_push_first(self):
        mails = self.apply(row("claude-one", harness="claude", session="claude"),
                           row("codex-one", harness="codex", session="codex"))
        self.assertEqual(set(mails), {"claude-one", "codex-one"})
        for body in mails.values():
            self.assertIn(DO_NOT_PUSH, first_instruction(body))
            self.assertNotIn(FIX_YOURSELF, body)
            self.assertIn("verdict blocked --sha %s" % SHA_A, body)
            self.assertIn("1 independent family for 2 required opinion(s)", body)

    def test_a_third_free_family_keeps_the_current_text(self):
        # claude corrige : codex et gemini restent, la correction est recevable.
        mails = self.apply(row("claude-one", harness="claude", session="claude"),
                           row("codex-one", harness="codex", session="codex"),
                           row("gemini-one", harness="gemini", session="gemini"))
        for body in mails.values():
            self.assertNotIn(DO_NOT_PUSH, body)
            self.assertIn(FIX_YOURSELF, body)
            self.assertIn("this correction is receivable", body)

    def test_a_one_opinion_lot_keeps_the_current_text(self):
        mails = self.apply(row("claude-one", harness="claude", session="claude"),
                           row("codex-one", harness="codex", session="codex"),
                           paths=("src/ameesh/work.py",))
        self.assertEqual(len(mails), 1)
        body = next(iter(mails.values()))
        self.assertTrue(first_instruction(body).startswith("- read the diff"))
        self.assertIn(FIX_YOURSELF, body)
        self.assertNotIn(DO_NOT_PUSH, body)


class ManualMailTest(unittest.TestCase):
    FREEZE = {"id": 3, "sha": SHA_A}

    def test_a_short_preview_opens_with_do_not_push(self):
        text = rd.manual_reviewer_message(
            LOT, freeze=self.FREEZE,
            correction={"state": "short", "required": 2, "remaining": 1})
        self.assertIn(DO_NOT_PUSH, first_instruction(text))
        self.assertNotIn("fix it yourself", text)
        self.assertIn("verdict blocked --sha %s" % SHA_A, text)

    def test_any_other_preview_keeps_the_text(self):
        plain = rd.manual_reviewer_message(LOT, freeze=self.FREEZE)
        self.assertIn("fix it yourself", plain)
        for room in ({"state": "ok", "required": 2, "remaining": 2},
                     {"state": "unknown", "required": 2},
                     {"state": "unavailable"},
                     {"state": "short", "required": 0}):
            self.assertEqual(rd.manual_reviewer_message(
                LOT, freeze=self.FREEZE, correction=room), plain)


class ManualRequestTest(unittest.TestCase):
    """`work._reviewer_request` lit l'aperçu du destinataire avant d'écrire."""

    def send(self, check):
        sent = {}

        def fake_send(db, sender, to, body, **kw):
            sent["body"] = body
            return 1

        with mock.patch.object(work, "milestones", return_value=[]), \
                mock.patch.object(plan_git, "latest_freeze",
                                  return_value={"id": 3, "sha": SHA_A}), \
                mock.patch.object(mail, "review_requests", return_value=[]), \
                mock.patch.object(mail, "send", side_effect=fake_send), \
                mock.patch.object(review_correction, "check", side_effect=check):
            out = work._reviewer_request(object(), dict(LOT), "claude-one",
                                         "mesh-design", cfg=object())
        self.assertEqual(out, {"reviewer_request_message_id": 1})
        return sent["body"]

    def test_a_too_tight_correction_says_do_not_push(self):
        body = self.send(lambda *a, **kw: {"state": "short", "required": 2,
                                           "remaining": 1})
        self.assertIn(DO_NOT_PUSH, first_instruction(body))

    def test_an_unreadable_preview_keeps_the_text_and_sends(self):
        def boom(*a, **kw):
            raise RuntimeError("fetch failed")
        body = self.send(boom)
        self.assertIn("fix it yourself", body)


if __name__ == "__main__":
    unittest.main()
