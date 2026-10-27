# SPDX-License-Identifier: AGPL-3.0-only
"""#4041 — la trace de file garde la classe du diff et nomme la preuve du véhicule.

Sur un paquet dont les membres sont prouvés sur leur tête gelée, la porte lit
la classe légère du véhicule (#2803). `queue add` affichait alors « class
light » suivi de la raison sensible du diff gelé entier (#4022, #3839), d'où
un faux signalement de porte. La porte ne change pas ; la trace rend la classe
du diff et nomme à part la preuve du véhicule (§12).
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import merge_queue

HEAD = "a" * 40
KLASS = {"class": "sensitive", "why": "supabase/tests/584_x.sql: sql",
         "files": [{"path": "supabase/tests/584_x.sql", "class": "sensitive"}]}


def _gate(vehicle, actors=()):
    marks = [{"kind": "verdict", "verdict": "ok", "sha": HEAD, "actor": actor,
              "at_ts": index, "id": index + 1} for index, actor in enumerate(actors)]
    with mock.patch.object(merge_queue, "actor_family",
                           side_effect=lambda _db, name, **_: {
                               "author": "deepseek", "rev1": "codex"}.get(name, "")):
        return merge_queue.review_gate(None, marks, HEAD, klass=dict(KLASS),
                                       item={"assignee": "author"},
                                       vehicle=vehicle)


class VehicleClassTraceTest(unittest.TestCase):
    def test_a_proven_packet_names_the_vehicle_proof_apart_from_the_class(self):
        gate = _gate({"ok": True, "required": 0, "klass": None,
                      "why": "2 constituent(s) reviewed on their frozen head(s); "
                             "no proper vehicle delta"})
        self.assertTrue(gate["ok"])
        self.assertEqual(gate["required"], 0)
        self.assertEqual(
            merge_queue._vehicle_note(gate),
            "§12 vehicle proven by its members: 2 constituent(s) reviewed on their "
            "frozen head(s); no proper vehicle delta; 0 verdicts on the vehicle (#2803)")

    def test_a_proper_delta_names_its_class_and_its_verdicts(self):
        gate = _gate({"ok": None, "required": 1, "own_paths": ["src/a.py"],
                      "klass": {"class": "normal", "why": "src/a.py: code"}},
                     actors=["rev1"])
        self.assertTrue(gate["ok"])
        note = merge_queue._vehicle_note(gate)
        self.assertIn("proper delta normal (src/a.py: code)", note)
        self.assertIn("1 verdict on the vehicle", note)

    def test_an_ordinary_lot_has_no_vehicle_note(self):
        gate = _gate(None, actors=["rev1"])
        self.assertEqual(gate["vehicle_why"], "")
        self.assertEqual(merge_queue._vehicle_note(gate), "")


if __name__ == "__main__":
    unittest.main()
