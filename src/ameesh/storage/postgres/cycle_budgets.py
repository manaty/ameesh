# SPDX-License-Identifier: AGPL-3.0-only
"""Cycle credit ledger (#7518). All mutations lock the same account.

No transport call lives inside the transaction. A duplicate attempt is refused,
including after settlement: the gate never grants permission to send twice.
"""
from __future__ import annotations

import json


class CycleBudgets:
    @staticmethod
    def _cycle_lock(tx, account):
        tx.query("select pg_advisory_xact_lock(hashtextextended("
                 "current_schema() || ':credit-cycle:' || %s, 0))", (account,))

    @staticmethod
    def _cycle_used(tx, account, cycle_id):
        return int(tx.query(
            "select coalesce(sum(case when state = 'settled' then actual_micro"
            " else greatest(bound_micro, coalesce(actual_micro, 0)) end), 0)::bigint"
            " as used from credit_requests"
            " where billing_account = %s and (cycle_id = %s or state <> 'settled'"
            " or closed_at >= (select observed_at from credit_cycles where id = %s))",
            (account, cycle_id, cycle_id))[0]['used'])

    @staticmethod
    def _cycle_not_breached(tx, account):
        if tx.query(
                "select id from credit_cycles where billing_account = %s and breached limit 1",
                (account,)):
            raise ValueError(
                "account blocked after a bound breach; operator reconciliation required")

    def cycle_put(self, account, cap, credit, observed, expires, fresh, source, actor):
        with self.db.transaction() as tx:
            self._cycle_lock(tx, account)
            self._cycle_not_breached(tx, account)
            # A new balance statement may refresh the envelope before expiry,
            # but replaying a statement never resets it. Pending liabilities and
            # results settled after the new observation continue to count.
            previous = tx.query(
                "select observed_at < %s::timestamptz as newer from credit_cycles"
                " where billing_account = %s order by id desc limit 1", (observed, account))
            if previous and not previous[0]['newer']:
                raise ValueError('a strictly newer dated credit statement is required')
            rows = tx.query(
                "insert into credit_cycles (billing_account, cap_micro, credit_micro,"
                " observed_at, expires_at, fresh_until, source, actor)"
                " select %s, %s, %s, %s, %s, %s, %s, %s"
                " where %s::timestamptz <= clock_timestamp()"
                " and %s::timestamptz > clock_timestamp() returning id",
                (account, cap, credit, observed, expires, fresh, source, actor, observed, fresh))
            if not rows:
                raise ValueError('credit statement is future-dated or already stale')
            return int(rows[0]['id'])

    def cycle_status(self, account):
        rows = self.db.query(
            "select *, observed_at::text as observed, expires_at::text as expires,"
            " fresh_until::text as fresh from credit_cycles"
            " where billing_account = %s order by id desc limit 1", (account,))
        if not rows:
            return None
        row = rows[0]
        row['used_micro'] = self._cycle_used(self.db, account, row['id'])
        return row

    def cycle_reserve(self, account, agent, attempt, bound, pricing):
        with self.db.transaction() as tx:
            self._cycle_lock(tx, account)
            self._cycle_not_breached(tx, account)
            rows = tx.query(
                "select id, cap_micro, observed_at <= clock_timestamp()"
                " and fresh_until > clock_timestamp() and expires_at > clock_timestamp()"
                " as valid from credit_cycles where billing_account = %s"
                " order by id desc limit 1", (account,))
            if not rows or not rows[0]['valid']:
                raise ValueError('credit envelope absent, expired or stale')
            if tx.query('select attempt_id from credit_requests where attempt_id = %s', (attempt,)):
                raise ValueError('attempt already reserved; never resend it')
            row = rows[0]
            used = self._cycle_used(tx, account, row['id'])
            if used + bound > int(row['cap_micro']):
                raise ValueError('cycle credit exhausted (including uncertain reservations)')
            tx.execute(
                "insert into credit_requests (attempt_id, cycle_id, billing_account,"
                " agent, bound_micro, pricing) values (%s, %s, %s, %s, %s, %s::jsonb)",
                (attempt, row['id'], account, agent, bound, json.dumps(pricing)))
            return int(row['id'])

    def cycle_settle(self, account, attempt, actual):
        with self.db.transaction() as tx:
            self._cycle_lock(tx, account)
            rows = tx.query('select state, bound_micro, actual_micro from credit_requests'
                            ' where billing_account = %s and attempt_id = %s for update',
                            (account, attempt))
            if not rows:
                raise ValueError('unknown attempt')
            row = rows[0]
            # A kept liability is never automatically released by a late result.
            if row['state'] != 'open':
                if (row['state'] == 'settled' and actual == row['actual_micro']) or (
                        row['state'] == 'kept' and actual is None):
                    return
                raise ValueError('attempt already closed; inconsistent settlement')
            if actual is not None and actual > int(row['bound_micro']):
                # An adapter violated its bound: retain the liability AND close
                # admission for this projection. Commit before raising outside.
                tx.execute('update credit_cycles set breached = true where billing_account = %s',
                           (account,))
                breach = True
            else:
                breach = False
            tx.execute('update credit_requests set state = %s, actual_micro = %s,'
                       ' closed_at = clock_timestamp() where attempt_id = %s',
                       ('kept' if actual is None or breach else 'settled', actual, attempt))
        if breach:
            raise ValueError('actual cost exceeded reserved bound; cycle admission stopped')
