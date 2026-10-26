# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : fenêtre courte du fournisseur, par compte (#3779).

Les journaux `account_rate_events` (429) et `account_turn_starts` (débuts de
tour) de la migration 0491 ne portent que des noms, des hôtes et le texte
d'erreur borné du fournisseur — aucun secret, aucune invite. Les instants sont
ceux de la base (`now()`), communs aux hôtes : une passe d'alertes ou un
exécuteur d'un autre fuseau ne décale ni le cooldown ni l'écart entre débuts.
"""
from __future__ import annotations

from .. import interface


class AccountWindows(interface.AccountWindows):

    def record(self, harness, account, host, agent, kind, detail) -> None:
        self.db.execute(
            "insert into account_rate_events"
            " (harness, account, host, agent, kind, detail)"
            " values (%s, %s, %s, %s, %s, %s)",
            (harness, account, host or "", agent or "", kind, detail),
        )

    def recent(self, harness, account, since_s) -> dict:
        rows = self.db.query(
            """
            SELECT count(*)::bigint AS count,
                   extract(epoch FROM max(at))::float8 AS last_at_ts,
                   (array_agg(agent ORDER BY at DESC))[1] AS last_agent,
                   (array_agg(host ORDER BY at DESC))[1] AS last_host,
                   (array_agg(detail ORDER BY at DESC))[1] AS last_detail
              FROM account_rate_events
             WHERE harness = %s AND account = %s
               AND at >= now() - make_interval(secs => %s)
            """,
            (harness, account, float(since_s)),
        )
        return rows[0] if rows else {
            "count": 0, "last_at_ts": None, "last_agent": None,
            "last_host": None, "last_detail": None,
        }

    def active(self, harness, account) -> list[dict]:
        return self.db.query(
            """
            SELECT name, host,
                   extract(epoch FROM status_since)::float8 AS status_since_ts
              FROM agent_registry
             WHERE harness = %s AND status = 'running'
               AND lease_owner IS NOT NULL
               AND lease_expires_at > clock_timestamp()
               AND (session_account IS NULL OR session_account = %s)
             ORDER BY status_since, name
            """,
            (harness, account),
        )

    def start(self, harness, account, host, agent) -> None:
        self.db.execute(
            "insert into account_turn_starts (harness, account, host, agent)"
            " values (%s, %s, %s, %s)",
            (harness, account, host or "", agent or ""),
        )

    def last_start(self, harness, account) -> dict:
        rows = self.db.query(
            """
            SELECT extract(epoch FROM max(at))::float8 AS last_at_ts,
                   (array_agg(agent ORDER BY at DESC))[1] AS last_agent,
                   (array_agg(host ORDER BY at DESC))[1] AS last_host
              FROM account_turn_starts
             WHERE harness = %s AND account = %s
            """,
            (harness, account),
        )
        return rows[0] if rows else {
            "last_at_ts": None, "last_agent": None, "last_host": None,
        }
