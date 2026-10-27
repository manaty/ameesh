# SPDX-License-Identifier: AGPL-3.0-only
"""#4041 — la trace de classe d'un véhicule de paquet dit la règle du §12.

Sur un paquet dont les membres sont prouvés sur leur tête gelée, la porte rend
la classe légère du véhicule (#2803). `queue add` affichait alors « class
light » suivi de la raison sensible du diff gelé entier (#4022, #3839), ce qui
a fait signaler à tort une porte défaillante. La classe rendue ne change pas ;
la raison nomme la règle du véhicule puis la classe du diff gelé.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import merge_queue

HEAD = "a" * 40
KLASS = {"class": "sensitive", "why": "supabase/tests/584_x.sql: sql",
         "files": [{"path": "supabase/tests/584_x.sql", "class": "sensitive"}]}


def _gate(vehicle):
    with mock.patch.object(merge_queue, "actor_family", return_value="deepseek"):
        return merge_queue.review_gate(None, [], HEAD, klass=dict(KLASS),
                                       item={"assignee": "author"},
                                       vehicle=vehicle)


class VehicleClassTraceTest(unittest.TestCase):
    def test_a_proven_packet_names_the_vehicle_rule_and_the_frozen_class(self):
        gate = _gate({"ok": True, "required": 0, "klass": None,
                      "why": "2 constituent(s) reviewed on their frozen head(s); "
                             "no proper vehicle delta"})
        self.assertTrue(gate["ok"])
        self.assertEqual(gate["class"], "light")
        why = merge_queue._class_why(gate, KLASS)
        self.assertTrue(why.startswith("§12 vehicle: 2 constituent(s) reviewed"), why)
        self.assertIn("frozen diff: sensitive — supabase/tests/584_x.sql: sql", why)

    def test_a_proper_delta_names_its_own_class(self):
        gate = _gate({"ok": None, "required": 1, "own_paths": ["src/a.py"],
                      "klass": {"class": "normal", "why": "src/a.py: code"}})
        self.assertEqual(gate["class"], "normal")
        why = merge_queue._class_why(gate, KLASS)
        self.assertIn("class of its proper delta (src/a.py: code)", why)
        self.assertIn("frozen diff: sensitive", why)

    def test_an_ordinary_lot_keeps_its_class_reason(self):
        gate = _gate(None)
        self.assertEqual(gate["class_why"], "")
        self.assertEqual(merge_queue._class_why(gate, KLASS), KLASS["why"])


if __name__ == "__main__":
    unittest.main()
