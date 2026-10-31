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

    def put(self, scope, window_s, usd, *, actor, lower_only=False, ceiling=None) -> tuple:
        with self.db.transaction() as tx:
            # A first cap has no row to lock. Serialize its creation too,
            # before reading the value used by lower_only and the journal.
            tx.query("SELECT pg_advisory_xact_lock(hashtextextended("
                     "current_schema() || ':budget_limits:' || %s || ':' || %s, 0))",
                     (scope, str(int(window_s))))
            rows = tx.query(
                "select usd::float8 as usd from budget_limits"
                " where scope = %s and window_s = %s for update", (scope, int(window_s)))
            old = float(rows[0]["usd"]) if rows else None
            if lower_only:
                # L87: compared under the lock, so a concurrent change cannot
                # turn this lowering into a raise
                current = old if old is not None else ceiling
                if usd is None or (current is not None and float(usd) >= current):
                    raise ValueError("not a lowering: %s is not below the cap in force (%s)"
                                     % (usd, "none" if current is None else current))
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

    # -- #1469 (migration 0443): daily envelope per billing account ----------
    #: the calendar day of each envelope, in its own zone, and its bounds
    _USAGE = (
        "with p as (select coalesce(%s::timestamptz, clock_timestamp()) as at),"
        " w as ("
        " select billing_account, usd_per_day, tz, (p.at at time zone tz)::date as period"
        " from account_budgets, p{where}),"
        " d as (select w.*, (period::timestamp at time zone tz) as day_start,"
        " ((period + 1)::timestamp at time zone tz) as day_end from w)"
        " select d.billing_account, d.usd_per_day::float8 as usd, d.tz,"
        " d.period::text as period, extract(epoch from d.day_start)::float8 as day_start_ts,"
        " extract(epoch from d.day_end)::float8 as reset_ts,"
        " coalesce((select sum(c.usd) from turn_costs c"
        "   where c.billing_account = d.billing_account and c.recorded_at >= d.day_start"
        "   and c.recorded_at < d.day_end and c.void_reason is null"
        "   and c.source <> 'device'), 0)::float8 as settled,"
        " coalesce((select sum(r.usd) from account_reservations r"
        "   where r.billing_account = d.billing_account and r.state = 'open'), 0)::float8"
        "   as reserved,"
        " coalesce((select sum(r.usd) from account_reservations r"
        "   where r.billing_account = d.billing_account and r.state = 'kept'"
        "   and r.period = d.period), 0)::float8 as kept"
        " from d order by d.billing_account")

    def account_usage(self, account=None, *, at=None) -> list:
        """Per envelope: usd (cap), tz, period, day_start_ts, reset_ts, settled,
        reserved, kept — for the calendar day containing `at` (default now)."""
        if account is None:
            return self.db.query(self._USAGE.format(where=""), (at,))
        return self.db.query(self._USAGE.format(where=" where billing_account = %s"),
                             (at, account))

    def account_put(self, account, usd, *, actor, tz=None) -> tuple:
        scope = "account:%s" % account
        with self.db.transaction() as tx:
            rows = tx.query("select usd_per_day::float8 as usd, tz from account_budgets"
                            " where billing_account = %s for update", (account,))
            old = float(rows[0]["usd"]) if rows else None
            old_tz = rows[0]["tz"] if rows else None
            if usd is None and old is None:
                return False, old
            if usd is not None and old == float(usd) and (tz is None or tz == old_tz):
                return False, old
            if usd is None:
                tx.execute("delete from account_budgets where billing_account = %s",
                           (account,))
            else:
                tx.execute(
                    "insert into account_budgets (billing_account, usd_per_day, tz, set_by)"
                    " values (%s, %s, %s, %s) on conflict (billing_account) do update"
                    " set usd_per_day = excluded.usd_per_day, tz = excluded.tz,"
                    " set_by = excluded.set_by, updated_at = now()",
                    (account, float(usd), tz or old_tz or "Europe/Paris", actor))
            tx.execute(
                "insert into budget_events (scope, window_s, old_usd, new_usd, actor)"
                " values (%s, 0, %s, %s, %s)",
                (scope, old, None if usd is None else float(usd), actor))
        return True, old

    def account_reserve(self, account, agent, usd, spend_key) -> dict:
        """Atomic admission (advisory lock on the account, one transaction).

        The instant of the decision is read once AFTER the lock (a wait across
        midnight must not use the day of the BEGIN), and serves for the usage,
        the period and `created_at`. A reservation of this agent still open
        belongs to a turn that never settled: it is `kept` (counted in full
        for its day), never released. Rend {"ok", "id", "usage"}: `usage` is
        the envelope before this reservation (None: reserved, no cap).
        """
        with self.db.transaction() as tx:
            return self.account_reserve_tx(tx, account, agent, usd, spend_key)

    def account_reserve_tx(self, tx, account, agent, usd, spend_key) -> dict:
        """Internal: caller owns transaction, after the tranche row lock."""
        tx.query("select pg_advisory_xact_lock(hashtext(%s))",
                 ("ameesh-account-budget:%s" % account,))
        at = tx.query("select clock_timestamp()::text as at")[0]["at"]
        tx.execute("update account_reservations set state = 'kept', closed_at = %s,"
                   " closed_by = 'unsettled turn' where agent = %s and state = 'open'",
                   (at, agent))
        rows = tx.query(self._USAGE.format(where=" where billing_account = %s"),
                        (at, account))
        usage = rows[0] if rows else None
        if usage is not None:
            used = usage["settled"] + usage["reserved"] + usage["kept"]
            if used + float(usd) > usage["usd"] + 1e-9:
                return {"ok": False, "id": None, "usage": usage}
        got = tx.query(
            "insert into account_reservations (billing_account, agent, usd, period,"
            " spend_key, created_at) values (%s, %s, %s, (%s::timestamptz at time zone"
            " coalesce((select tz from account_budgets where billing_account = %s),"
            " 'Europe/Paris'))::date, %s, %s) returning id",
            (account, agent, float(usd), at, account, spend_key, at))
        return {"ok": True, "id": int(got[0]["id"]), "usage": usage}

    def account_get(self, spend_key) -> dict | None:
        """The reservation of this ledger key (the opened-turn snapshot).

        Returns billing_account, usd, period and state, or None. The latest
        row wins if a ledger key ever names more than one reservation (the
        repair path deduplicates by key; this read never guesses a state)."""
        rows = self.db.query(
            "select billing_account, usd::float8 as usd,"
            " period::text as period, state from account_reservations"
            " where spend_key = %s order by id desc limit 1", (spend_key,))
        return rows[0] if rows else None

    def account_settle(self, spend_key, *, state, actor) -> dict | None:
        """Settles the reservation of ONE turn, found by its ledger key.

        In the same transaction the turn's ledger row (same `spend_key`) is
        tagged with the reservation's account: the real cost takes the place
        of the reservation atomically. A reservation already `kept` (the agent
        reserved again meanwhile) keeps counting; its row is still tagged."""
        if self.db.query('select id from tranche_reservations where spend_key = %s', (spend_key,)):
            from .turn_admission import settle_local_reservation
            return settle_local_reservation(self.db, spend_key, state=state, actor=actor)
        with self.db.transaction() as tx:
            rows = tx.query("select id, billing_account, agent, state,"
                            " usd::float8 as usd from account_reservations"
                            " where spend_key = %s for update", (spend_key,))
            if not rows:
                return None
            row = rows[0]
            tx.execute("update turn_costs set billing_account = %s"
                       " where spend_key = %s and billing_account is null",
                       (row["billing_account"], spend_key))
            if row["state"] == "open":
                tx.execute("update account_reservations set state = %s,"
                           " closed_at = clock_timestamp(), closed_by = %s where id = %s",
                           (state, actor, row["id"]))
        return row

    def account_release(self, reservation_id, *, actor) -> dict | None:
        """A human releases a kept (or stuck open) reservation."""
        tranche = self.db.query(
            'select r.id from tranche_reservations r join account_reservations a'
            ' on a.spend_key = r.spend_key where a.id = %s', (int(reservation_id),))
        if tranche:
            from .turn_admission import tranche_release
            row = self.db.query('select * from account_reservations where id = %s',
                                (int(reservation_id),))[0]
            tranche_release(self.db, tranche[0]['id'], actor=actor,
                            why='human account reservation release')
            return row
        rows = self.db.query(
            "update account_reservations set state = 'released', closed_at = now(),"
            " closed_by = %s where id = %s and state in ('kept', 'open')"
            " returning id, billing_account, agent, usd::float8 as usd, period::text as period",
            (actor, int(reservation_id)))
        return rows[0] if rows else None

    def account_kept(self, account=None) -> list:
        clauses = ["state in ('kept', 'open')"]
        params: list = []
        if account is not None:
            clauses.append("billing_account = %s")
            params.append(account)
        return self.db.query(
            "select id, billing_account, agent, usd::float8 as usd, period::text as period,"
            " state, extract(epoch from created_at)::float8 as created_ts"
            " from account_reservations where " + " and ".join(clauses)
            + " order by created_at desc limit 50", tuple(params))
