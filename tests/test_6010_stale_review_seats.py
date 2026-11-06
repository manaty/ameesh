# SPDX-License-Identifier: AGPL-3.0-only
"""#6010 : un siège de relecture périmé ne tient plus l'agent hors du repos.

Témoin du 06/11 01:05Z : `ameesh debit --hours 24 --json` comptait 7 agents
`held` pour des sièges « pending freeze » et « designated review pending »
dont les lots cités étaient déjà livrés. `_review_seats` lisait le journal
cumulatif des notes `sleep exempted:` sans regarder si le gel attendait
encore, et `_journal_exemption` ne notait qu'une fois par agent pour toute la
vie du lot de découpage.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import debit, file_decoupage

from tests.test_3256_debit_capacity import H, SPLIT, T, measure, team


def exhausted(at=T - 2 * H):
    return {"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh",
            "at_ts": at}


def seat(name, why, at=T - H):
    return {"work_item_id": 90, "source": SPLIT, "at_ts": at,
            "note": "sleep exempted: %s: %s" % (name, why)}


class StaleSeatTest(unittest.TestCase):
    def test_a_seat_on_a_delivered_lot_holds_nobody(self):
        # le lot #130 cité n'est plus parmi les lots ouverts (livré)
        notes = [exhausted(),
                 seat("ameesh-idle-0", "only eligible reviewer of family deepseek "
                                       "for pending freeze #130"),
                 seat("ameesh-idle-1", "designated review pending on #131")]
        cap = measure(team(2, 8), roadmap_notes=notes, open_lots=[])
        self.assertEqual(cap["held"], [])
        self.assertEqual(sorted(cap["projects"][0]["idle_agents"]),
                         ["ameesh-idle-0", "ameesh-idle-1"])

    def test_a_seat_on_a_merged_lot_holds_nobody(self):
        notes = [exhausted(), seat("ameesh-idle-0", "designated review pending on #130")]
        lots = [{"id": 130, "app": "ameesh", "state": "merged"}]
        cap = measure(team(2, 8), roadmap_notes=notes, open_lots=lots)
        self.assertEqual(cap["held"], [])

    def test_a_seat_on_a_waiting_lot_still_holds(self):
        notes = [exhausted(), seat("ameesh-idle-0", "designated review pending on #130")]
        lots = [{"id": 130, "app": "ameesh", "state": "qa"}]
        cap = measure(team(2, 8), roadmap_notes=notes, open_lots=lots)
        self.assertEqual(cap["held"], [{"agent": "ameesh-idle-0", "reason":
                                        "review seat: designated review pending on #130"}])

    def test_the_latest_note_of_the_agent_is_its_state(self):
        # un siège sur #130 (livré) puis sur #140 (en attente) : seul le
        # dernier vaut ; et l'inverse ne tient plus personne
        lots = [{"id": 140, "app": "ameesh", "state": "qa"}]
        notes = [exhausted(),
                 seat("ameesh-idle-0", "designated review pending on #130", T - H),
                 seat("ameesh-idle-0", "designated review pending on #140", T - H / 2)]
        seats = debit._review_seats(notes, debit._roadmap_verdicts(notes), lots)
        self.assertEqual(seats, {"ameesh-idle-0":
                                 "review seat: designated review pending on #140"})
        notes = [exhausted(),
                 seat("ameesh-idle-0", "designated review pending on #140", T - H),
                 seat("ameesh-idle-0", "unread mail from pilote (5 min)", T - H / 2)]
        self.assertEqual(debit._review_seats(notes, debit._roadmap_verdicts(notes),
                                             lots), {})


class JournalExemptionTest(unittest.TestCase):
    def run_journal(self, noted, why, since_ts=0.0):
        with mock.patch.object(file_decoupage.work, "events_noted",
                               return_value=noted) as read, \
                mock.patch.object(file_decoupage.work, "note") as write:
            file_decoupage._journal_exemption(object(), 90, "impl-1", why,
                                              since_ts=since_ts)
        return read, write

    def test_the_read_is_bounded_to_the_current_exhaustion(self):
        read, write = self.run_journal([], "designated review pending on #130",
                                       since_ts=T - H)
        self.assertEqual(read.call_args.args[-1], T - H)
        write.assert_called_once()

    def test_a_new_seat_is_journaled_once(self):
        old = [{"note": "sleep exempted: impl-1: designated review pending on #130"}]
        _, write = self.run_journal(old, "designated review pending on #140")
        self.assertEqual(write.call_args.args[2],
                         "sleep exempted: impl-1: designated review pending on #140")
        _, write = self.run_journal(old, "designated review pending on #130")
        write.assert_not_called()

    def test_other_motives_stay_one_trace_per_agent(self):
        old = [{"note": "sleep exempted: impl-1: unread mail from pilote (5 min)"}]
        _, write = self.run_journal(old, "unread mail from pilote (9 min)")
        write.assert_not_called()

    def test_the_exhaustion_instant_is_the_last_one(self):
        notes = [{"id": 1, "note": "roadmap: épuisée ameesh", "created_ts": 10.0},
                 {"id": 2, "note": "roadmap: rouverte ameesh", "created_ts": 20.0},
                 {"id": 3, "note": "roadmap: épuisée ameesh", "created_ts": 30.0}]
        self.assertEqual(file_decoupage._exhausted_since(notes, "ameesh"), 30.0)
        self.assertEqual(file_decoupage._exhausted_since([], "ameesh"), 0.0)


if __name__ == "__main__":
    unittest.main()
