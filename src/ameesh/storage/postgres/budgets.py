# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : plafonds de budget du mesh (L70, migration 0042).

`put` est UNE transaction : verrou de la ligne (portée, fenêtre), comparaison
avec la valeur en place, écriture, puis journal `budget_events`. Deux
`ameesh budget set` concurrents s'ordonnent ; chacun journalise l'ancienne
valeur qu'il a réellement remplacée. Le déclencheur de 0042 émet le réveil
`ameesh_budget` au commit.
"""
from __future__ import annotations

from .. import interface


class Budgets(interface.Budgets):

    def limits(self) -> list:
        return self.db.query(
            "select scope, window_s, usd::float8 as usd, set_by,"
            " extract(epoch from updated_at)::float8 as updated_ts"
            " from budget_limits order by scope, window_s")

    def put(self, scope, window_s, usd, *, actor) -> tuple:
        with self.db.transaction() as tx:
            rows = tx.query(
                "select usd::float8 as usd from budget_limits"
                " where scope = %s and window_s = %s for update", (scope, int(window_s)))
            old = float(rows[0]["usd"]) if rows else None
            if (old is None and usd is None) or (
                    old is not None and usd is not None and float(usd) == old):
                return False, old
            if usd is None:
                tx.execute("delete from budget_limits where scope = %s and window_s = %s",
                           (scope, int(window_s)))
            else:
                tx.execute(
                    "insert into budget_limits (scope, window_s, usd, set_by)"
                    " values (%s, %s, %s, %s)"
                    " on conflict (scope, window_s) do update"
                    " set usd = excluded.usd, set_by = excluded.set_by, updated_at = now()",
                    (scope, int(window_s), float(usd), actor))
            tx.execute(
                "insert into budget_events (scope, window_s, old_usd, new_usd, actor)"
                " values (%s, %s, %s, %s, %s)",
                (scope, int(window_s), old, None if usd is None else float(usd), actor))
        return True, old

    def events(self, limit, scope=None) -> list:
        clauses, params = [], []
        if scope is not None:
            clauses.append("scope = %s")
            params.append(scope)
        params.append(int(limit))
        return self.db.query(
            "select id, scope, window_s, old_usd::float8 as old_usd,"
            " new_usd::float8 as new_usd, actor,"
            " extract(epoch from at)::float8 as at_ts from budget_events"
            + (" where " + " and ".join(clauses) if clauses else "")
            + " order by at desc, id desc limit %s", tuple(params))
