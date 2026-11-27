# SPDX-License-Identifier: AGPL-3.0-only
"""#8320 : trois mesures fausses du débit (analyse de l'auditeur, 27/11).

1. le geste `qa_merge` disait « prochain paquet horaire » en dur : il nomme le
   créneau déclaré (`packets.cutoffs`), sinon « dès la fusion du paquet en
   cours » ;
2. l'âge `qa_merge` courait du gel : il court du dernier verdict `ok` de la
   tête gelée (témoin #8294, 3 h 41 affichées pour 6 min) ;
3. les heures-agent retenues par empreinte comptaient un lot pour chaque
   agent au repos (témoin #8312, 38,7 h-agent pour un lot).
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from ameesh import debit

from .test_3256_debit_capacity import H, SPLIT, T as CAP_T, measure, split_lot, team
from .test_6423_debit_qa_merge_wake import event, frozen, item, mark, run, verdict


def _ts(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(
        tzinfo=timezone.utc).timestamp()


NOW = _ts("2026-11-26 16:16")


def _report(*, verdict_at: float, **extra):
    freeze_at = _ts("2026-11-26 12:35")
    return run(items=[item(8294, app="ameesh", assignee="auteur")],
               milestones=[frozen(8294, 100, freeze_at),
                           verdict(8294, 101, verdict_at)],
               events=[event(8294, 100, freeze_at,
                             mark(8294, 100, ["orchestrateur"]))],
               now=NOW, wait_s=60, **extra)


def _qa_merge(report):
    rows = [row for row in report["slow_waits"] if row["kind"] == "qa_merge"]
    assert len(rows) == 1, report["slow_waits"]
    return rows[0]


class QaMergeAgeTest(unittest.TestCase):
    def test_age_runs_from_the_ok_verdict_not_the_freeze(self):
        row = _qa_merge(_report(verdict_at=_ts("2026-11-26 16:10")))
        self.assertAlmostEqual(row["age_s"], 6 * 60, delta=1)
        self.assertEqual(row["since"], "2026-11-26T16:10:00Z")

    def test_an_ok_older_than_the_freeze_keeps_the_freeze(self):
        # même tête, verdict rendu avant un gel répété : jamais avant le gel
        row = _qa_merge(_report(verdict_at=_ts("2026-11-26 12:00")))
        self.assertEqual(row["since"], "2026-11-26T12:35:00Z")


class QaMergeGestureTest(unittest.TestCase):
    def test_the_gesture_names_the_declared_slot(self):
        # canon à créneaux pairs (Europe/Paris) : 16:16Z = 17:16 locale → 18:00
        row = _qa_merge(_report(verdict_at=_ts("2026-11-26 16:10"),
                                zone="Europe/Paris",
                                next_packet=_ts("2026-11-26 17:00")))
        self.assertIn("dans le paquet de 18:00", row["gesture"])
        self.assertNotIn("horaire", row["gesture"])

    def test_without_cadence_the_packet_follows_the_current_one(self):
        row = _qa_merge(_report(verdict_at=_ts("2026-11-26 16:10")))
        self.assertIn("dès la fusion du paquet en cours", row["gesture"])
        self.assertNotIn("horaire", row["gesture"])


class FootprintHoursTest(unittest.TestCase):
    def test_one_held_lot_counts_once_for_five_idle_agents(self):
        notes = [{"work_item_id": 90, "source": SPLIT,
                  "note": "roadmap: épuisée ameesh", "at_ts": CAP_T - 3 * H}]
        held = {"id": 8312, "app": "ameesh", "state": "intake",
                "created_ts": CAP_T - 2 * H}
        cap = measure(team(5, 0), roadmap_notes=notes,
                      open_lots=[split_lot(), held],
                      footprint={8312: [{"id": 50, "state": "build"}]})
        self.assertEqual(cap["projects"][0]["idle_agent_hours_footprint"], 2.0)
        self.assertEqual(cap["mesh"]["idle_agent_hours_footprint"], 2.0)


if __name__ == "__main__":
    unittest.main()
