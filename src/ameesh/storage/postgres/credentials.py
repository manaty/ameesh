# SPDX-License-Identifier: AGPL-3.0-only
"""Postgres driver: credential health per host, harness and account
(L226a/L226b, migration 0058).

Only account NAMES pass here, never a secret.
"""
from __future__ import annotations

from .. import interface

_ROW = ("select host, harness, account, state, reason, source,"
        " extract(epoch from checked_at)::float8 as checked_ts,"
        " extract(epoch from failing_since)::float8 as failing_since_ts"
        " from credential_health")


class Credentials(interface.Credentials):

    def report(self, host, harness, account, state, *, reason="", source) -> dict:
        rows = self.db.query(
            "with old as (select state from credential_health"
            "             where host = %s and harness = %s and account = %s)"
            " insert into credential_health"
            " (host, harness, account, state, reason, source, checked_at, failing_since)"
            " values (%s, %s, %s, %s, %s, %s, now(),"
            "         case when %s = 'failing' then now() end)"
            " on conflict (host, harness, account) do update set"
            "   state = excluded.state, reason = excluded.reason,"
            "   source = excluded.source, checked_at = now(),"
            "   failing_since = case when excluded.state <> 'failing' then null"
            "     else coalesce(credential_health.failing_since, now()) end"
            " returning state, (select state from old) as previous",
            (host, harness, account or "", host, harness, account or "", state,
             reason or "", source, state))
        return rows[0] if rows else {}

    def health(self, host, harness) -> dict:
        rows = self.db.query(_ROW + " where host = %s and harness = %s order by account",
                             (host, harness))
        return {row["account"]: row for row in rows}

    def listing(self, host=None) -> list:
        if host is None:
            return self.db.query(_ROW + " order by host, harness, account")
        return self.db.query(_ROW + " where host = %s order by harness, account", (host,))
