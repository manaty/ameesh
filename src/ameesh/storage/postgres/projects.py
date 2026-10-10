# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : lecture de la vue par projet (`ameesh projects`, L62).

Une seule instruction, un seul aller-retour : les agents (état, non-lus, lot
en cours, dépense 24 h) et les lots ouverts sont agrégés en deux tableaux
JSON dans une même ligne. Lecture seule, sans migration ; les deux pilotes
(psql, psycopg) rendent les colonnes `json` déjà décodées.
"""
from __future__ import annotations

from .. import interface

#: états de lot terminés (même liste que `work_items` ailleurs)
_CLOSED = "('merged', 'promoted', 'closed')"

_BOARD_SQL = """
WITH unread AS (
    SELECT recipient, count(*)::bigint AS n
      FROM agent_mailbox
     WHERE delivered_at IS NULL
     GROUP BY recipient
), spend AS (
    SELECT agent, coalesce(sum(usd), 0)::float8 AS usd_24h, count(*)::bigint AS turns_24h
      FROM turn_costs
     WHERE recorded_at >= now() - interval '24 hours' AND void_reason IS NULL
     GROUP BY agent
), assigned AS (
    SELECT DISTINCT ON (assignee) assignee, id, title, state,
           count(*) OVER (PARTITION BY assignee)::bigint AS n
      FROM work_items
     WHERE assignee IS NOT NULL AND state NOT IN __CLOSED__
     ORDER BY assignee, updated_at DESC, id DESC
), agents AS (
    SELECT r.name, r.chantier, r.team, r.harness, r.host, r.provider, r.credential_mode,
           r.status, r.status_text, r.mode, r.stop_reason, r.responsible,
           (r.lease_owner IS NOT NULL
            AND r.lease_expires_at > clock_timestamp()) AS lease_live,
           extract(epoch from p.created_at)::float8     AS turn_started_ts,
           extract(epoch from r.status_since)::float8   AS status_since_ts,
           extract(epoch from r.last_turn_at)::float8   AS last_turn_ts,
           extract(epoch from r.updated_at)::float8     AS updated_ts,
           extract(epoch from r.last_seen)::float8      AS last_seen_ts,
           coalesce(u.n, 0)::bigint AS unread,
           sw.id AS session_lot_id, sw.title AS session_lot_title,
           sw.state AS session_lot_state,
           a.id AS assigned_lot_id, a.title AS assigned_lot_title,
           a.state AS assigned_lot_state,
           coalesce(a.n, 0)::bigint AS open_lots,
           coalesce(s.usd_24h, 0)::float8 AS usd_24h,
           coalesce(s.turns_24h, 0)::bigint AS turns_24h
      FROM agent_registry r
      LEFT JOIN spend_pending p ON p.agent = r.name
      LEFT JOIN unread u ON u.recipient = r.name
      LEFT JOIN spend s ON s.agent = r.name
      LEFT JOIN assigned a ON a.assignee = r.name
      LEFT JOIN LATERAL (
          SELECT w.id, w.title, w.state FROM work_items w
           WHERE w.id = CASE WHEN r.session_work_item ~ '^[0-9]{1,18}$'
                             THEN r.session_work_item::bigint END) sw ON true
), lots AS (
    SELECT w.id, w.title, w.state, w.app, w.workstream, w.assignee,
           k.team AS package_team,
           extract(epoch from w.updated_at)::float8 AS updated_ts,
           count(*) OVER ()::bigint AS total
      FROM work_items w
      LEFT JOIN work_packages k ON k.id = w.package_id
     WHERE w.state NOT IN __CLOSED__
     ORDER BY w.updated_at DESC, w.id DESC
     LIMIT %s
)
SELECT (SELECT coalesce(json_agg(agents ORDER BY agents.name), '[]'::json) FROM agents)
           AS agents,
       (SELECT coalesce(json_agg(lots ORDER BY lots.updated_ts DESC, lots.id DESC),
                        '[]'::json) FROM lots) AS lots
""".replace("__CLOSED__", _CLOSED)


class Projects(interface.Projects):

    def board(self, *, max_lots) -> dict:
        rows = self.db.query(_BOARD_SQL, (int(max_lots),))
        row = rows[0] if rows else {}
        return {"agents": list(row.get("agents") or []), "lots": list(row.get("lots") or [])}
