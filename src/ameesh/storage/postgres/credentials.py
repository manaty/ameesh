# SPDX-License-Identifier: AGPL-3.0-only
"""Postgres driver: credential health per host, harness and account
(L226a/L226b, migrations 0060 and 0068).

Only account NAMES pass here, never a secret.
"""
from __future__ import annotations

from .. import interface

#: the columns the supervisor role is granted (role-superviseur.sql): no
#: fingerprint — `listing` feeds the alerts, which that role reads
_COLUMNS = ("select host, harness, account, state, reason, source,"
            " extract(epoch from checked_at)::float8 as checked_ts,"
            " extract(epoch from failing_since)::float8 as failing_since_ts,"
            " extract(epoch from expires_at)::float8 as expires_ts,"
            " extract(epoch from last_failure_at)::float8 as last_failure_ts,"
            " failures")
_LISTING = _COLUMNS + " from credential_health"
#: and the fingerprint, for the sentinel and the runner (full role)
_ROW = _COLUMNS + ", failing_fingerprint from credential_health"


class Credentials(interface.Credentials):

    def report(self, host, harness, account, state, *, reason="", source,
               expires_at=None, fingerprint=None, expect_failures=None) -> dict:
        # a `failing` report always lands; any other only if no failure was
        # counted since the caller read the row (`expect_failures`)
        rows = self.db.query(
            "with old as (select state from credential_health"
            "             where host = %s and harness = %s and account = %s)"
            " insert into credential_health"
            " (host, harness, account, state, reason, source, checked_at, failing_since,"
            "  expires_at, last_failure_at, failing_fingerprint, failures)"
            " values (%s, %s, %s, %s, %s, %s, now(),"
            "         case when %s = 'failing' then now() end, to_timestamp(%s::float8),"
            "         case when %s = 'failing' then now() end, %s,"
            "         case when %s = 'failing' then 1 else 0 end)"
            " on conflict (host, harness, account) do update set"
            "   state = excluded.state, reason = excluded.reason,"
            "   source = excluded.source, checked_at = now(),"
            "   expires_at = excluded.expires_at,"
            "   failing_since = case when excluded.state <> 'failing' then null"
            "     else coalesce(credential_health.failing_since, now()) end,"
            "   last_failure_at = case when excluded.state = 'failing' then now()"
            "     else credential_health.last_failure_at end,"
            "   failing_fingerprint = excluded.failing_fingerprint,"
            "   failures = credential_health.failures + excluded.failures"
            " where excluded.state = 'failing' or %s::int is null"
            "    or credential_health.failures = %s::int"
            " returning state, (select state from old) as previous",
            (host, harness, account or "", host, harness, account or "", state,
             reason or "", source, state,
             None if expires_at is None else float(expires_at),
             state, fingerprint if state == "failing" else None, state,
             expect_failures, expect_failures))
        return rows[0] if rows else {"skipped": True}

    def annotate(self, host, harness, account, reason, *, expect_failures) -> bool:
        rows = self.db.query(
            "update credential_health set reason = %s"
            " where host = %s and harness = %s and account = %s"
            "   and state = 'failing' and failures = %s::int returning account",
            (reason, host, harness, account or "", int(expect_failures)))
        return bool(rows)

    def health(self, host, harness) -> dict:
        rows = self.db.query(_ROW + " where host = %s and harness = %s order by account",
                             (host, harness))
        return {row["account"]: row for row in rows}

    def listing(self, host=None) -> list:
        if host is None:
            return self.db.query(_LISTING + " order by host, harness, account")
        return self.db.query(_LISTING + " where host = %s order by harness, account", (host,))
