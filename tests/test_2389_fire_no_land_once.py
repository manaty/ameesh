# SPDX-License-Identifier: AGPL-3.0-only
"""#2389 : le passage du feu ne répète pas `no-land` à chaque tour.

Quand un hôte ne connaît pas la voie d'atterrissage d'un projet (config sans
`radar.projects`, aucune ligne de file partagée), le feu s'abstient et rend
`no-land` à chaque passage. C'est un état stable, pas un événement : le
journal ne doit le dire qu'une fois par lot et par feu, et le redire après
l'invalidation ou le retrait du feu pour un nouveau feu.
"""
from __future__ import annotations

import unittest
from unittest import mock

from . import support  # noqa: F401 - isole l'environnement de l'hôte
from ameesh.runner import Runner

A = "a" * 40
B = "b" * 40


class FireNoLandJournalTest(unittest.TestCase):
    def setUp(self):
        self.runner = Runner.__new__(Runner)
        self.runner.db = object()
        self.runner.cfg = mock.Mock()
        self.runner.dry_run = False
        self.runner._fire_no_land = {}

    def _pass(self, *entries):
        with mock.patch("ameesh.fire.run_pass", return_value=list(entries)) as run_pass, \
                mock.patch("ameesh.migrations.require_compatible_writer") as admission, \
                mock.patch("ameesh.runner.fresh_radar", side_effect=lambda cfg: cfg), \
                mock.patch("ameesh.runner.log_async") as journal:
            self.runner.fire_once(mock.Mock())
        run_pass.assert_called_once()
        admission.assert_called_once_with(self.runner.db)
        return [call.args[0] for call in journal.call_args_list]

    @staticmethod
    def _no_land(lot=42, sha=A, detail="this host has no explicit landing mode"):
        return {"lot": lot, "result": "no-land", "fire_sha": sha, "detail": detail}

    def test_no_land_is_said_once_for_the_same_fire(self):
        first = self._pass(self._no_land())
        second = self._pass(self._no_land())
        self.assertEqual(first, ["fire: lot #42 no-land — this host has no explicit "
                                 "landing mode"])
        self.assertEqual(second, [])

    def test_a_new_fire_says_no_land_again(self):
        self._pass(self._no_land(sha=A))
        again = self._pass(self._no_land(sha=B))
        self.assertEqual(again, ["fire: lot #42 no-land — this host has no explicit "
                                 "landing mode"])

    def test_each_lot_has_its_own_once(self):
        first = self._pass(self._no_land(lot=42), self._no_land(lot=43))
        self.assertEqual(len(first), 2)
        self.assertEqual(self._pass(self._no_land(lot=42), self._no_land(lot=43)), [])

    def test_an_ended_fire_arms_the_message_again(self):
        self._pass(self._no_land())
        self._pass({"lot": 42, "result": "invalidated", "fire_sha": A})
        again = self._pass(self._no_land())
        self.assertEqual(len(again), 1)

    def test_other_loud_results_are_still_said_at_each_pass(self):
        entry = {"lot": 42, "result": "error", "fire_sha": A, "error": "boom"}
        self.assertEqual(self._pass(entry), ["fire: lot #42 error — boom"])
        self.assertEqual(self._pass(entry), ["fire: lot #42 error — boom"])

    def test_quiet_results_stay_quiet(self):
        quiet = {"lot": 42, "result": "not-ready", "fire_sha": A, "detail": "CI not green"}
        self.assertEqual(self._pass(quiet), [])


if __name__ == "__main__":
    unittest.main()
