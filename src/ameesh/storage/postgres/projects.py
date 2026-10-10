# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : lecture de la vue par projet (`ameesh projects`, L62).

Une seule instruction, un seul aller-retour : les agents (état, non-lus, lot
en cours, dépense 24 h) et les lots ouverts sont agrégés en deux tableaux
JSON dans une même ligne. Lecture seule, sans migration ; les deux pilotes
(psql, psycopg) rendent les colonnes `json` déjà décodées.

Lot en cours (correctif du 2026-10-11) : trois candidats, tous OUVERTS — un
lot fusionné ou fermé n'est jamais « en cours » : le dernier lot cité par
l'agent dans ses `CITED_SCAN` derniers messages (`mail_lot_*`), le lot de sa
session (`session_lot_*`), le lot ouvert assigné le plus récent
(`assigned_lot_*`). Une référence de lot (`work_item_id` d'un message, lot
de session) est un numéro, ou une étiquette qui désigne UN SEUL lot ouvert
(issue, fiche du plan, branche, premier mot du titre : même règle que
`work.open_by_ref`).
"""
from __future__ import annotations

from .. import interface

#: états de lot terminés (même liste que `work_items` ailleurs)
_CLOSED = "('merged', 'promoted', 'closed')"
#: messages de l'agent examinés au plus pour trouver le lot qu'il cite
CITED_SCAN = 200


def _lot_ref(expr: str) -> str:
    """Le numéro du lot que désigne la référence `expr` (texte) : un numéro
    (`12`, `#12`), sinon l'étiquette unique d'un lot ouvert (`open_labels`)."""
    ref = "btrim(%s)" % expr
    return ("CASE WHEN %s ~ '^#?[0-9]{1,18}$' THEN ltrim(%s, '#')::bigint"
            " ELSE (SELECT l.id FROM open_labels l WHERE l.ref = lower(%s)) END"
            % (ref, ref, ref))

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
), open_labels AS (
    -- une étiquette (issue, fiche du plan, branche, premier mot du titre) qui
    -- désigne UN SEUL lot ouvert ; ambiguë, elle ne désigne rien
    SELECT k.ref, min(w.id) AS id
      FROM work_items w
     CROSS JOIN LATERAL (VALUES (lower(w.issue_ref)), (lower(w.package_id)),
                                (lower(w.branch)),
                                (lower(rtrim(split_part(btrim(w.title), ' ', 1), ':')))) k(ref)
     WHERE w.state NOT IN __CLOSED__ AND coalesce(k.ref, '') <> ''
     GROUP BY k.ref
    HAVING count(DISTINCT w.id) = 1
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
           ml.id AS mail_lot_id, ml.title AS mail_lot_title, ml.state AS mail_lot_state,
           a.id AS assigned_lot_id, a.title AS assigned_lot_title,
           a.state AS assigned_lot_state,
           coalesce(a.n, 0)::bigint AS open_lots,
           coalesce(s.usd_24h, 0)::float8 AS usd_24h,
           coalesce(s.turns_24h, 0)::bigint AS turns_24h,
           lu.body AS last_update_body, lu.ts AS last_update_ts,
           lu.work_item_id AS last_update_lot,
           (SELECT extract(epoch from least(
                       (SELECT min(e.created_at) FROM work_item_events e
                         WHERE e.work_item_id = coalesce(ml.id, sw.id, a.id)
                           AND (e.actor IN (r.name, 'agent:' || r.name)
                                OR position(('assigné à ' || r.name) in e.note) = 1)),
                       (SELECT min(m.created_at) FROM agent_mailbox m
                         WHERE m.sender = r.name
                           AND m.work_item_id = coalesce(ml.id, sw.id, a.id)::text)))::float8
           ) AS on_task_since_ts
      FROM agent_registry r
      LEFT JOIN spend_pending p ON p.agent = r.name
      LEFT JOIN unread u ON u.recipient = r.name
      LEFT JOIN spend s ON s.agent = r.name
      LEFT JOIN assigned a ON a.assignee = r.name
      -- le lot de la session : numéro ou étiquette, s'il est OUVERT
      LEFT JOIN LATERAL (
          SELECT w.id, w.title, w.state FROM work_items w
           WHERE w.id = __SESSION_REF__ AND w.state NOT IN __CLOSED__) sw ON true
      -- le dernier lot OUVERT cité par l'agent dans ses derniers messages
      -- (hors événements ; index agent_mailbox_sender_idx de 0044)
      LEFT JOIN LATERAL (
          SELECT w.id, w.title, w.state
            FROM (SELECT m.id, m.work_item_id AS ref
                    FROM agent_mailbox m
                   WHERE m.sender = r.name AND m.kind <> 'event'
                   ORDER BY m.id DESC LIMIT __CITED_SCAN__) c
            JOIN work_items w ON w.id = __MAIL_REF__ AND w.state NOT IN __CLOSED__
           WHERE coalesce(btrim(c.ref), '') <> ''
           ORDER BY c.id DESC LIMIT 1) ml ON true
      -- L96 : la dernière avancée de l'agent, lue dans le fil (son dernier
      -- message, index agent_mailbox_sender_idx de 0044)
      LEFT JOIN LATERAL (
          SELECT left(m.body, 400) AS body, m.work_item_id,
                 extract(epoch from m.created_at)::float8 AS ts
            FROM agent_mailbox m
           WHERE m.sender = r.name AND m.kind <> 'event'
           ORDER BY m.id DESC LIMIT 1) lu ON true
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
""".replace("__SESSION_REF__", _lot_ref("r.session_work_item")) \
  .replace("__MAIL_REF__", _lot_ref("c.ref")) \
  .replace("__CITED_SCAN__", str(CITED_SCAN)) \
  .replace("__CLOSED__", _CLOSED)


class Projects(interface.Projects):

    def board(self, *, max_lots) -> dict:
        rows = self.db.query(_BOARD_SQL, (int(max_lots),))
        row = rows[0] if rows else {}
        return {"agents": list(row.get("agents") or []), "lots": list(row.get("lots") or [])}
