# SPDX-License-Identifier: AGPL-3.0-only
"""#3333 — panier #3288 : `blocked_freeze` suspendu sous retenue, une lecture.

Relecture auditeur de #3288 (27be8fd4) : `slo.lot_steps` faisait courir
`blocked_freeze` sur tout lot ouvert ; une retenue (`blocked`,
`waiting_human`) ou un hold de pilote levé (#1664) ne la suspendait pas.
Un lot parqué volontairement avec un avis bloquant criait donc « réassigner
maintenant » vers le bureau ou l'auditeur à la levée. Attendu : sous retenue,
aucune ligne ; après la levée, l'horloge repart de la levée (comme le délai
intake, #2916). Même passe : la passe d'alertes lisait `_owed_read` deux fois
(avis dus puis corrections dues) ; une seule lecture suffit.

Le troisième point du panier (un rebloquage du même relecteur sur la même
tête relançait l'horloge) est corrigé par B1 de #3288 (ba421769f) et couvert
par `test_3288_fixes_owed.FixesOwedTest.test_two_blocks_of_the_same_reviewer_keep_the_first_start`.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from ameesh import slo, work
from ameesh import review_designation_activation as activation

from .test_3288_fixes_owed import H, _Base

NOW = 1_800_000_000.0


def _steps(state: str, events: list, *, blocked_ago: float = 6 * H,
           hold_releases=()) -> list:
    item = {"id": 7, "state": state, "priority": 2, "assignee": "author",
            "created_ts": NOW - 10 * H}
    fix = {"lot": 7, "name": "author", "sha": "a" * 40,
           "blocked_ts": NOW - blocked_ago, "blocked_by": "reviewer"}
    rows = slo.lot_steps(item, {}, events, now=NOW, fixes=[fix],
                         hold_releases=hold_releases)
    return [r for r in rows if r["step"] == "blocked_freeze"]


def _ev(ident: int, state: str, ago: float) -> dict:
    return {"id": ident, "state": state, "created_ts": NOW - ago, "note": ""}


class BlockedFreezeHoldTest(unittest.TestCase):
    """`lot_steps` seul : la retenue suspend l'horloge de la correction."""

    def test_no_line_while_the_lot_is_held(self):
        for state in slo.HOLDS:
            with self.subTest(state=state):
                events = [_ev(1, "qa", 7 * H), _ev(2, state, 5 * H)]
                self.assertEqual(_steps(state, events), [])

    def test_the_clock_restarts_at_the_release(self):
        # bloqué il y a 6 h, parqué de -5 h à -1 h : 1 h de retard, pas 6.
        events = [_ev(1, "qa", 7 * H), _ev(2, "blocked", 5 * H),
                  _ev(3, "qa", 1 * H)]
        [line] = _steps("qa", events)
        self.assertEqual(line["elapsed_s"], int(H))
        self.assertEqual(line["level"], 0)
        self.assertEqual(line["hold_released_ts"], round(NOW - H, 3))

    def test_a_hold_before_the_block_does_not_move_the_clock(self):
        events = [_ev(1, "waiting_human", 9 * H), _ev(2, "qa", 8 * H)]
        [line] = _steps("qa", events)
        self.assertEqual(line["elapsed_s"], int(6 * H))
        self.assertEqual(line["level"], 3)
        self.assertNotIn("hold_released_ts", line)

    def test_a_lifted_pilot_hold_restarts_the_clock(self):
        events = [_ev(1, "qa", 7 * H)]
        [line] = _steps("qa", events, hold_releases=[NOW - 0.5 * H])
        self.assertEqual(line["elapsed_s"], int(0.5 * H))
        self.assertEqual(line["level"], 0)

    def test_without_a_hold_the_block_still_escalates(self):
        [line] = _steps("qa", [_ev(1, "qa", 7 * H)])
        self.assertEqual((line["level"], tuple(line["targets"])),
                         (3, ("bureau", "auditeur")))


class BlockedFreezeHoldPassTest(_Base):
    """La passe d'alertes : retenue tue, levée datée, une seule lecture."""

    def test_a_held_lot_raises_no_fix_alert_then_restarts(self):
        lot, _sha = self.blocked(age=6 * H)
        self.assertEqual([a["level"] for a in self.slo_alerts()], [3])
        work.move(self.db, lot, "blocked")
        self.assertEqual(self.slo_alerts(), [])
        work.move(self.db, lot, "qa")
        # la levée vient d'avoir lieu : le délai repart, aucun dépassement
        self.assertEqual(self.slo_alerts(), [])
        fixes = [f for f in activation.due_fixes(self.db) if f["lot"] == lot]
        self.assertEqual(len(fixes), 1, "la correction reste due")

    def test_the_pass_reads_owed_work_once(self):
        self.blocked(age=3 * H)
        real = activation._owed_read
        with mock.patch.object(activation, "_owed_read",
                               side_effect=real) as spy:
            alerts = self.slo_alerts()
        self.assertEqual(len(alerts), 1, alerts)
        self.assertEqual(spy.call_count, 1)

    def test_an_unreadable_read_drops_both_halves_only(self):
        self.blocked(age=3 * H)
        with mock.patch.object(activation, "_owed_read",
                               side_effect=RuntimeError("base illisible")):
            self.assertEqual(self.slo_alerts(), [])

    def test_owed_work_matches_the_two_queues(self):
        lot, _sha = self.blocked(age=3 * H)
        pending, fixes = activation.owed_work(self.db)
        self.assertEqual(pending, activation.pending_reviews(self.db))
        self.assertEqual(fixes, activation.due_fixes(self.db))
        self.assertEqual([f["lot"] for f in fixes], [lot])


if __name__ == "__main__":
    unittest.main()
