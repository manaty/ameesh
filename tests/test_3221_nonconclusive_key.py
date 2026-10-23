# SPDX-License-Identifier: AGPL-3.0-only
"""#3221 — la clé du lot de correction garde les parenthèses du nom du check.

Panier de #3209 (relecture ameesh-cx2 sur 7d4541d7) : `nonconclusive_source`
retirait toute parenthèse finale. Le paquet consigne `nom (ÉTATS)`, la
release le nom brut : `tests (linux) (CANCELLED)` au paquet et
`tests (linux)` à la release avaient deux clés, et la release confondait
`tests (linux)` et `tests (windows)` sous la clé `tests`. Seul un suffixe
fait d'états connus est retiré.
"""
from __future__ import annotations

import types
import unittest

from ameesh import packet_automerge, radar, release_cutoff

from .test_3209_skipped_by_construction import _Lots


class KeyTest(unittest.TestCase):

    def key(self, entry):
        return radar.nonconclusive_source("ameesh", entry)

    def test_packet_and_release_share_the_key_of_a_parenthesised_name(self):
        self.assertEqual(self.key("tests (linux) (CANCELLED)"), self.key("tests (linux)"))

    def test_two_parenthesised_names_keep_two_keys(self):
        self.assertNotEqual(self.key("tests (linux)"), self.key("tests (windows)"))

    def test_only_a_suffix_of_known_states_is_removed(self):
        self.assertEqual(self.key("tests (CANCELLED)"), "nonconclusive:ameesh:tests")
        self.assertEqual(self.key("ci/docs* (ERROR, SUCCESS)"),
                         "nonconclusive:ameesh:ci/docs*")
        self.assertEqual(self.key("tests (linux)"), "nonconclusive:ameesh:tests (linux)")


class ReleaseLotTest(unittest.TestCase):

    def setUp(self):
        self.lots = _Lots(self)

    def test_the_release_opens_one_lot_per_parenthesised_check(self):
        packet_automerge._open_correction(
            object(), {"app": "ameesh"}, slug="manaty/ameesh", number=2427,
            sha="b" * 40, base="main", consigned=["tests (linux) (CANCELLED)"],
            actor="ameesh")
        plan = types.SimpleNamespace(head="d" * 40, version="1.20.0",
                                     ci={"inconclusive": ["tests (linux)", "tests (windows)"]})
        release_cutoff._open_correction(object(), types.SimpleNamespace(name="ameesh"), plan)
        self.assertEqual(len(self.lots.rows), 2)
        self.assertIn("tests (windows)", self.lots.rows[1]["title"])
        self.assertEqual(len(self.lots.notes), 1)
        self.assertEqual(self.lots.notes[0][0], self.lots.rows[0]["id"])


if __name__ == "__main__":
    unittest.main()
