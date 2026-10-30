# SPDX-License-Identifier: AGPL-3.0-only
"""#4850 — la lecture d'un tour dans le grand livre passe par l'index de 0523.

Sans cet index, `LEDGER_SQL` remonte la clé primaire en filtrant : un tour
absent lit toute la table (830 ms sur 60 149 lignes, base commune, 30/10).
"""
from __future__ import annotations

import json
import unittest

from ameesh import db as db_mod
from ameesh.storage.postgres.turn_observation import LEDGER_SQL

from .support import PgTestCase


class TurnCostsTurnIdIndexTest(PgTestCase):
    INDEX = "turn_costs_turn_id_idx"

    def test_the_index_is_partial_on_turn_id(self):
        rows = self.db.query(
            "SELECT pg_get_indexdef(i.indexrelid) AS def,"
            "       pg_get_expr(i.indpred, i.indrelid) AS pred"
            "  FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
            " WHERE c.relname = %s"
            "   AND c.relnamespace = current_schema()::regnamespace",
            (self.INDEX,))
        self.assertEqual(len(rows), 1)
        self.assertIn("(turn_id)", rows[0]["def"])
        self.assertEqual(rows[0]["pred"], "(turn_id IS NOT NULL)")

    def test_the_ledger_read_uses_the_index(self):
        # le pilote psql enveloppe chaque lecture dans un agrégat JSON, où
        # `EXPLAIN` ne peut pas entrer : le plan se lit sous psycopg.
        if not isinstance(self.db, db_mod.PsycopgDriver):
            self.skipTest("EXPLAIN is wrapped by the psql driver")
        # assez de lignes pour que le plan ne dépende pas d'un départage
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, turn, spend_key, turn_id)"
            " SELECT 't4850', 'claude', g, 't4850:' || g, 'tour-' || g"
            "   FROM generate_series(1, 5000) g")
        self.db.execute("ANALYZE turn_costs")
        with self.db.transaction() as tx:
            plan = json.dumps(tx.query(
                "EXPLAIN (FORMAT JSON) " + LEDGER_SQL, ("absent", "absent")))
        self.assertIn(self.INDEX, plan)


if __name__ == "__main__":
    unittest.main()
