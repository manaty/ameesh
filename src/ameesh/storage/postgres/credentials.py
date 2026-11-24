# SPDX-License-Identifier: AGPL-3.0-only
"""Postgres driver: credential health per host, harness and account
(L226a/L226b, migrations 0060, 0068, 0604 and 0607).

Only account NAMES pass here, never a secret.
"""
from __future__ import annotations

from .. import interface

#: the columns the supervisor role is granted (role-superviseur.sql): no
#: fingerprint — `listing` feeds the alerts, which that role reads
_COLUMNS = ("select host, harness, account, state, reason, source,"
            " extract(epoch from checked_at)::float8 as checked_ts,"
            " extract(epoch from probed_at)::float8 as probed_ts,"
            " extract(epoch from probe_missing_since)::float8 as probe_missing_ts,"
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
            " (host, harness, account, state, reason, source, checked_at, probed_at,"
            "  failing_since, expires_at, last_failure_at, failing_fingerprint, failures)"
            " values (%s, %s, %s, %s, %s, %s, now(),"
            "         case when %s = 'sentinel' then now() end,"
            "         case when %s = 'failing' then now() end, to_timestamp(%s::float8),"
            "         case when %s = 'failing' then now() end, %s,"
            "         case when %s = 'failing' then 1 else 0 end)"
            " on conflict (host, harness, account) do update set"
            "   state = excluded.state, reason = excluded.reason,"
            "   source = excluded.source, checked_at = now(),"
            "   probed_at = case when excluded.source = 'sentinel' then now()"
            "     else credential_health.probed_at end,"
            "   expires_at = excluded.expires_at,"
            "   failing_since = case when excluded.state <> 'failing' then null"
            "     else coalesce(credential_health.failing_since, now()) end,"
            "   last_failure_at = case when excluded.state = 'failing' then now()"
            "     else credential_health.last_failure_at end,"
            "   failing_fingerprint = excluded.failing_fingerprint,"
            "   healthy_streak = 0, healthy_probe_at = null,"
            "   failures = credential_health.failures + excluded.failures"
            " where excluded.state = 'failing' or %s::int is null"
            "    or credential_health.failures = %s::int"
            " returning state, (select state from old) as previous",
            (host, harness, account or "", host, harness, account or "", state,
             reason or "", source, source, state,
             None if expires_at is None else float(expires_at),
             state, fingerprint if state == "failing" else None, state,
             expect_failures, expect_failures))
        if not rows and source == "sentinel":
            # #8062 : un échec de tour a gagné la course, le verdict n'est pas
            # écrit, mais la sonde a bien relu l'identifiant : sa date avance.
            self.db.query(
                "update credential_health set probed_at = now()"
                " where host = %s and harness = %s and account = %s"
                " returning account",
                (host, harness, account or ""))
            return {"skipped": True}
        return rows[0] if rows else {"skipped": True}

    def annotate(self, host, harness, account, reason, *, expect_failures) -> bool:
        rows = self.db.query(
            "update credential_health set reason = %s"
            " where host = %s and harness = %s and account = %s"
            "   and state = 'failing' and failures = %s::int returning account",
            (reason, host, harness, account or "", int(expect_failures)))
        return bool(rows)

    def touch(self, host, harness, account, *, expect_failures=None) -> bool:
        # #8062 : la sonde a relu l'identifiant sans changer de verdict
        # (`kept_failure`) ; `probed_at` est son battement propre, que les
        # écritures `turn` ne touchent pas. L'état et le compteur d'échecs
        # restent intacts ; `checked_at` suit la dernière lecture.
        rows = self.db.query(
            "update credential_health set checked_at = now(), probed_at = now()"
            " where host = %s and harness = %s and account = %s"
            "   and state = 'failing'"
            "   and (%s::int is null or failures = %s::int) returning account",
            (host, harness, account or "", expect_failures, expect_failures))
        return bool(rows)

    def probe_streak(self, host, harness, account, *, healthy, interval,
                     expect_failures=None) -> int | None:
        # #8200 : la sonde a relu l'identifiant sans preuve de credential
        # neuve. Pour `gh`/`scw`, un verdict sain est la preuve du serveur :
        # il compte une sonde de la série, espacée d'au moins `interval`
        # secondes ; un verdict non concluant rompt la série. L'ancre repart
        # si `last_failure_at` est plus récent qu'elle, pour qu'un échec
        # écrit par un N-1 (qui ne connaît pas ces colonnes) ne soit pas levé
        # par une série commencée avant lui. `probed_at` reste le battement
        # de la sonde (#8062).
        rows = self.db.query(
            "update credential_health set"
            " checked_at = now(), probed_at = now(),"
            " healthy_streak = case"
            "   when %s::boolean is false then 0"
            "   when healthy_probe_at is null"
            "     or healthy_probe_at < last_failure_at then 1"
            "   when now() - healthy_probe_at >= make_interval(secs => %s)"
            "     then healthy_streak + 1"
            "   else healthy_streak end,"
            " healthy_probe_at = case"
            "   when %s::boolean is false then null"
            "   when healthy_probe_at is null"
            "     or healthy_probe_at < last_failure_at then now()"
            "   when now() - healthy_probe_at >= make_interval(secs => %s)"
            "     then now()"
            "   else healthy_probe_at end"
            " where host = %s and harness = %s and account = %s"
            "   and state = 'failing'"
            "   and (%s::int is null or failures = %s::int)"
            " returning healthy_streak",
            (bool(healthy), float(interval), bool(healthy), float(interval),
             host, harness, account or "", expect_failures, expect_failures))
        return int(rows[0]["healthy_streak"]) if rows else None

    def health(self, host, harness) -> dict:
        rows = self.db.query(_ROW + " where host = %s and harness = %s order by account",
                             (host, harness))
        return {row["account"]: row for row in rows}

    def listing(self, host=None) -> list:
        if host is None:
            return self.db.query(_LISTING + " order by host, harness, account")
        return self.db.query(_LISTING + " where host = %s order by harness, account", (host,))

    def probe_transition(self):
        # #8062 : l'instant où 0604 est passée borne la transition N-1. La
        # table est dans le contrat du rôle superviseur (#7982).
        rows = self.db.query(
            "select extract(epoch from applied_at)::float8 as ts"
            " from schema_migrations where version = 604")
        return float(rows[0]["ts"]) if rows else None
