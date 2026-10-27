# SPDX-License-Identifier: AGPL-3.0-only
"""#4044 — témoin de la ligne corrigée de #4041 dans `_readiness`.

Sur un véhicule prouvé par ses membres (§12, #2803), la porte lit la classe
légère du véhicule ; la file doit pourtant rendre la classe du diff gelé
entier (`_class_of`). Un retour de la précédence à `gate["class"]` rougit
ici : la classe ET la note de file sont assertées, via `enter`.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import merge_queue

TIP = "b" * 40
TARGET_TIP = "c" * 40
WHY = ("2 constituent(s) reviewed on their frozen head(s); "
       "no proper vehicle delta")
SENSITIVE = {"class": "sensitive", "why": "supabase/tests/584_x.sql: sql"}
LIGHT = {"class": "light", "why": "docs/a.md: doc"}


def _enter(branch, klass, gate):
    row = {"id": 4044, "state": "frozen", "branch": branch,
           "branch_target": "main", "assignee": "author"}
    queue_add = mock.Mock(return_value={"lot": 4044})
    store = mock.Mock()
    store.work.queue_add = queue_add
    with mock.patch.object(merge_queue.work, "get", return_value=row), \
            mock.patch.object(merge_queue.work, "milestones", return_value=[]), \
            mock.patch.object(merge_queue.plan_git, "latest_freeze",
                              return_value={"sha": TIP, "id": 7}), \
            mock.patch.object(merge_queue, "fetch_fresh",
                              return_value=({branch: TIP, "main": TARGET_TIP},
                                            "/clone")), \
            mock.patch.object(merge_queue, "pinned", return_value=TIP), \
            mock.patch.object(merge_queue.packet_guard, "check_merge"), \
            mock.patch.object(merge_queue, "_class_of", return_value=dict(klass)), \
            mock.patch.object(merge_queue, "review_gate", return_value=dict(gate)), \
            mock.patch.object(merge_queue.storage, "of", return_value=store), \
            mock.patch.object(merge_queue, "source_of", return_value="repo"), \
            mock.patch.dict("os.environ", {"AGENT_MAIL_NAME": "", "AMEESH_ROLE": ""}):
        ready_row = merge_queue.enter(
            None, 4044, repo="/repo",
            ci=lambda _b: {"number": 3097, "head": TIP, "green": True,
                           "draft": False})
    return ready_row, queue_add.call_args.kwargs["note"]


class ReadinessVehicleClassTest(unittest.TestCase):
    def test_a_proven_vehicle_keeps_the_sensitive_class_of_its_diff(self):
        row, note = _enter(
            "integration/paquet-h-20261027-43", SENSITIVE,
            {"ok": True, "required": 0, "class": "light", "vehicle_why": WHY,
             "waived": False})
        self.assertEqual(row["class"], "sensitive")
        self.assertIn(
            "class sensitive (supabase/tests/584_x.sql: sql), §12 vehicle proven "
            "by its members: %s; 0 verdicts on the vehicle (#2803)" % WHY, note)
        self.assertNotIn("class light", note)

    def test_an_ordinary_light_lot_stays_light_without_a_vehicle_note(self):
        row, note = _enter(
            "lot/4044-x", LIGHT,
            {"ok": True, "required": 0, "class": "light", "vehicle_why": "",
             "waived": False})
        self.assertEqual(row["class"], "light")
        self.assertEqual(row["vehicle"], "")
        self.assertIn("class light (docs/a.md: doc)", note)
        self.assertNotIn("vehicle", note)


if __name__ == "__main__":
    unittest.main()
