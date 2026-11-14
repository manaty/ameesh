# SPDX-License-Identifier: AGPL-3.0-only
"""#7327 : `work.activity`, la lecture agrégée des lots d'un hôte.

L'équivalence est prouvée contre la lecture par agent de l'interface (qui
appelle `items`) sur une base migrée : mêmes lots non terminaux, même dernier
`updated_at` (lots fermés compris), même troncature à `limit`.
"""
from __future__ import annotations

import unittest

from ameesh import storage
from ameesh.storage import interface

from . import support


class WorkActivityEquivalenceTest(support.PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE work_items RESTART IDENTITY CASCADE")
        self.store = storage.of(self.db).work

    def lot(self, ident: int, assignee: str, state: str, updated: float,
            *, wake: float | None = None, planned: str | None = None,
            close_reason: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO work_items"
            " (id, title, type, state, assignee, updated_at, wake_at,"
            "  planned_start, close_reason)"
            " VALUES (%s, %s, 'evolution', %s, %s, to_timestamp(%s),"
            "         to_timestamp(%s), %s::date, %s)",
            (ident, "lot %d" % ident, state, assignee, updated, wake, planned,
             close_reason))

    def test_aggregate_matches_the_per_agent_read(self):
        # a1 : un lot de chaque famille ; le lot fermé est le plus récent et
        # doit borner latest_updated_ts sans apparaître dans `open`.
        self.lot(1, 'a1', 'build', 5000.0, wake=9000.0, planned='2026-01-01')
        self.lot(2, 'a1', 'qa', 4000.0)
        self.lot(3, 'a1', 'blocked', 3000.0, wake=2000.0)
        self.lot(4, 'a1', 'waiting_human', 2500.0)
        self.lot(5, 'a1', 'closed', 9000.0, close_reason='abandoned')
        self.lot(6, 'a1', 'merged', 5500.0)
        # a2 n'a aucun lot ; a3 dépasse la borne de troncature.
        self.lot(7, 'a3', 'build', 7000.0)
        self.lot(8, 'a3', 'qa', 6000.0)
        self.lot(9, 'a3', 'blocked', 5000.0, wake=8000.0)

        names = ['a1', 'a2', 'a3']
        aggregate = self.store.activity(names)
        per_agent = interface.WorkItems.activity(self.store, names)
        self.assertEqual(aggregate, per_agent)

        self.assertEqual([lot['id'] for lot in aggregate['a1']['open']],
                         [1, 2, 3, 4])
        self.assertEqual(aggregate['a1']['latest_updated_ts'], 9000.0)
        self.assertEqual(aggregate['a2'], {'latest_updated_ts': None, 'open': []})
        # planned_start reste None : `items` ne projette pas cette colonne, et
        # held_actions doit lire exactement la même valeur.
        self.assertTrue(all(lot['planned_start'] is None
                            for lot in aggregate['a1']['open']))

        bounded = self.store.activity(names, limit=2)
        self.assertEqual(bounded, interface.WorkItems.activity(self.store, names, limit=2))
        self.assertIsNone(bounded['a3']['open'])
        self.assertIsNone(bounded['a1']['open'])
        self.assertEqual(bounded['a3']['latest_updated_ts'], 7000.0)

    def test_unknown_or_empty_agents_are_zeroed_not_omitted(self):
        self.lot(10, 'a1', 'build', 1000.0)
        out = self.store.activity(['a1', 'inconnu', 'a1'])
        self.assertEqual(set(out), {'a1', 'inconnu'})
        self.assertEqual(out['inconnu'], {'latest_updated_ts': None, 'open': []})
        self.assertEqual(self.store.activity([]), {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
