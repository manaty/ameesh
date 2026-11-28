# SPDX-License-Identifier: AGPL-3.0-only
"""#8396 : un lot canon livré ne reste pas « qa_merge, en attente du paquet ».

Témoin #8378 (canon home, PR manaty/home#337 fusionnée à 8de43678, jalon
`merged` à 22:57Z) : le débit le comptait encore dans `slow_waits` en
`qa_merge` depuis 22:19Z, avec le geste « rappeler au pilote de le ranger dans
le paquet de 04:00 ». Une note d'entretien du canon
(`review-canon-refresh:…:checked:`) avait été écrite après la fusion avec
l'état `validated` lu avant elle : le journal finissait sur un état ouvert et
`_reopened` rouvrait la revue.

Une reprise de revue se prouve par un vrai retour au travail (`intake`,
`build`, `qa`) après le jalon terminal ; un `validated` seul, écrit après la
fusion, est une note d'entretien et non une reprise. Un lot canon livré ne
passe jamais par un paquet ameesh (§11 bis : la cible d'un paquet est `main`
d'ameesh ou `develop` de Nexlink).
"""
from __future__ import annotations

import unittest

from ameesh import debit

from .test_6423_debit_qa_merge_wake import H, T, event, item, run, reviewed


class DeliveredCanonLotTest(unittest.TestCase):
    def _report(self, *, rework=False):
        freeze_at = T - 4 * H
        milestones, events = reviewed(8378, 100, freeze_at)
        milestones = milestones + [{
            "work_item_id": 8378, "id": 102, "kind": "merged",
            "at_ts": T - 2 * H, "sha": "a" * 40, "actor": "mesh-design",
            "verdict": None, "note": "",
        }]
        events = events + [
            event(8378, 101, T - 2 * H, "merge seen", state="merged"),
        ]
        if rework:
            events.extend([
                event(8378, 102, T - 90 * 60, "merged → build", state="build"),
                event(8378, 103, T - 80 * 60, "build → qa", state="qa"),
                event(8378, 104, T - 70 * 60, "quorum", state="validated"),
            ])
        events.append(event(
            8378, 105, T - H,
            "review-canon-refresh:8378:100:%s:checked: canon policy "
            "rechecked: designated" % ("b" * 64),
            actor="ameesh", state="validated"))
        return run(
            items=[item(8378, app="ameesh",
                        branch="canon/8378-tests-reels-nexlink",
                        branch_target="main")],
            milestones=milestones, events=events)

    def test_a_delivered_canon_lot_is_not_waiting_for_a_packet(self):
        report = self._report()
        self.assertEqual(report["qa"], [])
        self.assertNotIn((8378, "qa_merge"),
                         [(row["lot"], row["kind"]) for row in report["slow_waits"]])
        self.assertEqual(report["slow_waits"], [])

    def test_a_real_rework_after_the_merge_still_waits_for_the_packet(self):
        report = self._report(rework=True)
        self.assertIn((8378, "qa_merge"),
                      [(row["lot"], row["kind"]) for row in report["slow_waits"]])


class ReopenedTest(unittest.TestCase):
    def test_a_bare_validated_note_after_the_merge_does_not_reopen(self):
        events = [event(1, 1, T, "fusion", state="merged"),
                  event(1, 2, T + 1, "review-canon-refresh", state="validated")]
        self.assertFalse(debit._reopened(events, T - 1))

    def test_a_validated_after_a_real_rework_stays_open(self):
        events = [event(1, 1, T, "fusion", state="merged"),
                  event(1, 2, T + 1, "reprise", state="build"),
                  event(1, 3, T + 2, "quorum", state="validated")]
        self.assertTrue(debit._reopened(events, T - 1))

    def test_a_later_terminal_closes_a_reopened_validated(self):
        events = [event(1, 1, T, "fusion", state="merged"),
                  event(1, 2, T + 1, "reprise", state="build"),
                  event(1, 3, T + 2, "nouvelle fusion", state="merged"),
                  event(1, 4, T + 3, "review-canon-refresh", state="validated")]
        self.assertFalse(debit._reopened(events, T - 1))
