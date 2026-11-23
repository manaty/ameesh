# SPDX-License-Identifier: AGPL-3.0-only
"""#8118 — un seul réveil par relecteur et par nouvelle tête.

Témoin du lot : trois regels successifs avec les mêmes relecteurs donnent
trois réveils par relecteur (un par tête gelée), pas six à neuf. La demande de
désignation du siège est le réveil ; l'avis que l'auteur envoie autour du gel
devient passif (lu au tour suivant) et ne double plus ce réveil.

La porte ne change pas (#2442) : la demande exacte reste déposée sur la tête
gelée et le verdict reste dû.
"""
from __future__ import annotations

import unittest

from ameesh import mail, work
from ameesh.work_reviewers import designated

from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture


class OneWakePerRegelTest(_ActivationFixture, PgTestCase):
    # -- helpers ----------------------------------------------------------
    def _verdict(self, actor, harness, sha, verdict="ok"):
        mark = work.milestone(self.db, self.lot, "verdict", sha=sha,
                              actor=actor, verdict=verdict, repo=self.repo.root,
                              after_commit=[])
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, 't', %s, %s)",
            ("milestone:" + str(int(mark["id"])), sha, actor,
             "s-" + harness, harness, self.lot))

    def _advance(self, text):
        self.repo.write("src/ameesh/mail.py", text + "\n")
        self.frozen = self.repo.commit(text)
        self.receipts(commits=[self.frozen])

    def _requests(self, name):
        return [m for m in self.mailbox()
                if m["recipient"] == name and m["kind"] == "request"]

    def _notifies(self, name):
        return [m for m in self.mailbox()
                if m["recipient"] == name and m["kind"] == "notify"]

    def _wakes(self, name):
        return [m for m in self.mailbox()
                if m["recipient"] == name and not mail.passive_reason(m)]

    def _author_note(self, name, **kwargs):
        return mail.send(self.db, "author", name,
                         "#%d : nouveau gel %s" % (self.lot, self.frozen),
                         kind="notify", work_item_id=str(self.lot), **kwargs)

    # -- witnesses --------------------------------------------------------
    def test_three_regels_wake_each_reviewer_once_per_head(self):
        for index in range(3):
            if index:
                self._advance("head%d" % (index + 1))
            _mark, out = self.freeze()
            self.assertEqual(out["state"], "designated", out)
            self.assertEqual(set(designated(work.get(self.db, self.lot))),
                             {"r1", "r2"})
            # L'auteur prévient les relecteurs après le gel, comme dans
            # l'incident (#8087, #8096) : l'avis ne doit pas rouvrir un tour.
            for name in ("r1", "r2"):
                self._author_note(name)
            for name in ("r1", "r2"):
                notes = self._notifies(name)
                self.assertEqual(len(notes), index + 1, notes)
                self.assertTrue(all(mail.passive_reason(m) for m in notes),
                                [(m["kind"], mail.passive_reason(m)) for m in notes])
                self.assertEqual(len(self._requests(name)), index + 1)
            # Le siège est réveillé (demande lue) puis rend son verdict : la
            # tête suivante aura sa propre demande.
            self.db.execute(
                "UPDATE agent_mailbox SET delivered_at = now()"
                " WHERE work_item_id = %s AND kind = 'request'",
                (str(self.lot),))
            for name, harness in (("r1", "claude"), ("r2", "codex")):
                self._verdict(name, harness, self.frozen)
        for name in ("r1", "r2"):
            wakes = self._wakes(name)
            self.assertEqual(len(wakes), 3,
                             [(m["kind"], m["body"][:50]) for m in wakes])
            self.assertEqual([m["kind"] for m in wakes], ["request"] * 3)

    def test_a_human_or_system_notice_always_wakes(self):
        self.freeze()
        before = len(self._wakes("r1"))
        mail.send(self.db, "human:alice", "r1", "#%d : question" % self.lot,
                  kind="notify", work_item_id=str(self.lot))
        mail.send(self.db, "ameesh", "r1", "#%d : rappel système" % self.lot,
                  kind="notify", work_item_id=str(self.lot))
        self.assertEqual(len(self._wakes("r1")), before + 2)

    def test_an_urgent_notice_is_never_passive(self):
        self.freeze()
        before = len(self._wakes("r1"))
        self._author_note("r1", payload={"urgent": True})
        self.assertEqual(len(self._wakes("r1")), before + 1)

    def test_a_notice_outside_the_review_lot_or_state_wakes(self):
        self.freeze()
        reviews_before = len(self._wakes("r1"))
        pilot_before = len(self._wakes("pilot"))
        # Destinataire hors siège : le pilote n'est pas relecteur désigné.
        mail.send(self.db, "author", "pilot", "#%d : pour information" % self.lot,
                  kind="notify", work_item_id=str(self.lot))
        # Lot remis en construction : plus de verdict dû sur une tête gelée.
        self.db.execute("UPDATE work_items SET state = 'build' WHERE id = %s",
                        (self.lot,))
        self._author_note("r1")
        self.assertEqual(len(self._wakes("pilot")), pilot_before + 1)
        self.assertEqual(len(self._wakes("r1")), reviews_before + 1)

    def test_after_an_ok_verdict_the_authors_notice_wakes_again(self):
        # Frontière de la règle : le réveil unique couvre le temps où le
        # verdict est dû ou qu'un B engage un nouveau gel. Une fois le verdict
        # `ok` rendu sur la tête gelée, un avis ordinaire de l'auteur réveille
        # de nouveau.
        self.freeze()
        self._verdict("r1", "claude", self.frozen)
        before = len(self._wakes("r1"))
        self._author_note("r1")
        self.assertEqual(len(self._wakes("r1")), before + 1)

    def test_a_blocked_verdict_keeps_the_fix_cycle_notice_passive(self):
        # Avant même le gel suivant, l'auteur annonce le correctif : le B rendu
        # engage un nouveau gel, donc l'avis attend le tour suivant et la
        # demande de désignation reste le seul réveil de la tête à venir.
        self.freeze()
        self._verdict("r1", "claude", self.frozen, verdict="blocked")
        before = len(self._wakes("r1"))
        self._author_note("r1")
        self.assertEqual(len(self._wakes("r1")), before)
        self.assertTrue(mail.passive_reason(self._notifies("r1")[0]))


if __name__ == "__main__":
    unittest.main()
