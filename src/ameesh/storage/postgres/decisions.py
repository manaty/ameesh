# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : demandes de décision au propriétaire (lot L124).

Aucune table neuve : une demande est une ligne de `agent_mailbox`
(`kind = 'request'`, objet `payload.decision`, destinataire `human:<id>`).
Elle reste « non remise » (`delivered_at IS NULL`) tant qu'elle attend : la
lecture des demandes en attente passe par l'index partiel des non-lus. La
clôture (réponse ou retrait) est UNE écriture conditionnelle : un seul
gagnant entre deux réponses simultanées.

`register_chat` inscrit l'agent de conversation du propriétaire (L123) dans
`agent_registry`, en mode `externe` : aucun exécuteur ne le réclame jamais.
"""
from __future__ import annotations

import json

from .. import interface
from .mailbox import PROJECT_SQL
from .registry import NAME_LOCK_CTE, name_lock_key

#: une ligne de `agent_mailbox` qui porte une demande de décision
_IS_DECISION = "kind = 'request' AND (payload -> 'decision') IS NOT NULL"

_COLUMNS = """
    id, sender, recipient, body, payload, work_item_id, status, host,
    extract(epoch from created_at)::float8   AS created_ts,
    extract(epoch from delivered_at)::float8 AS closed_ts,
    (__PROJECT__) AS sender_project
""".replace("__PROJECT__", PROJECT_SQL.replace("%s", "agent_mailbox.sender"))

#: L233: a mail that may carry a decision written in prose
_MAIL_COLUMNS = """
    m.id, m.sender, m.recipient, m.body, m.kind, m.work_item_id,
    extract(epoch from m.created_at)::float8 AS created_ts
"""

#: les demandes en attente pour la vue par projet (`projects.board`, L124) :
#: un sous-SELECT, inséré dans la requête unique de la vue
PENDING_BOARD_SQL = """
    SELECT m.id, m.recipient AS owner, m.sender AS requester,
           extract(epoch from m.created_at)::float8 AS created_ts,
           coalesce((m.payload -> 'decision' ->> 'urgent')::boolean, false) AS urgent,
           (m.payload -> 'decision' ->> 'due_ts')::float8 AS due_ts
      FROM agent_mailbox m
     WHERE m.delivered_at IS NULL AND m.kind = 'request'
       AND (m.payload -> 'decision') IS NOT NULL
     ORDER BY m.id
     LIMIT 500
"""


class Decisions(interface.Decisions):

    def create(self, *, sender, recipient, body, payload, work_item_id, host) -> dict:
        rows = self.db.query(
            """
            INSERT INTO agent_mailbox
                (sender, recipient, body, host, kind, payload, work_item_id, status)
            VALUES (%s, %s, %s, %s, 'request', %s::jsonb, %s, 'pending')
            RETURNING id, extract(epoch from created_at)::float8 AS created_ts,
                      (__PROJECT__) AS sender_project
            """.replace("__PROJECT__", PROJECT_SQL),
            (sender, recipient, body, host,
             json.dumps(payload or {}, ensure_ascii=False), work_item_id, sender),
        )
        return rows[0]

    def get(self, decision_id) -> dict | None:
        rows = self.db.query(
            "SELECT %s FROM agent_mailbox WHERE id = %%s AND %s" % (_COLUMNS, _IS_DECISION),
            (int(decision_id),))
        return rows[0] if rows else None

    def pending(self, *, limit) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM agent_mailbox WHERE delivered_at IS NULL AND %s"
            " ORDER BY id LIMIT %%s" % (_COLUMNS, _IS_DECISION), (int(limit),))

    def history(self, *, limit) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM agent_mailbox WHERE delivered_at IS NOT NULL AND %s"
            " ORDER BY id DESC LIMIT %%s" % (_COLUMNS, _IS_DECISION), (int(limit),))

    def pending_for_lot(self, lot) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM agent_mailbox WHERE delivered_at IS NULL AND %s"
            " AND btrim(work_item_id) = %%s ORDER BY id" % (_COLUMNS, _IS_DECISION),
            (str(lot).strip(),))

    def close(self, decision_id, *, status, record) -> dict | None:
        # Une seule instruction : la condition (encore en attente) et
        # l'écriture ne se séparent pas — deux réponses, un seul gagnant.
        rows = self.db.query(
            """
            UPDATE agent_mailbox
               SET status = %s, delivered_at = now(),
                   payload = jsonb_set(payload, '{decision}',
                                       (payload -> 'decision') || %s::jsonb)
             WHERE id = %s AND delivered_at IS NULL AND __IS_DECISION__
            RETURNING __COLUMNS__
            """.replace("__IS_DECISION__", _IS_DECISION).replace("__COLUMNS__", _COLUMNS),
            (status, json.dumps(record or {}, ensure_ascii=False), int(decision_id)),
        )
        return rows[0] if rows else None

    def source_mail(self, message_id) -> dict | None:
        rows = self.db.query(
            "SELECT %s FROM agent_mailbox m WHERE m.id = %%s" % _MAIL_COLUMNS,
            (int(message_id),))
        return rows[0] if rows else None

    def prose_candidates(self, *, recipients, since_ts, limit) -> list[dict]:
        # The cue words are matched by the caller (decisions.prose_cues): here
        # only the shape — mail written by an agent to bureau or a human, not
        # a request, reply, alert or receipt, not yet converted into a request.
        return self.db.query(
            """
            SELECT __COLUMNS__ FROM agent_mailbox m
             WHERE m.created_at >= to_timestamp(%s)
               AND (m.recipient = ANY(ARRAY(SELECT jsonb_array_elements_text(%s::jsonb)))
                    OR m.recipient LIKE 'human:%%')
               AND coalesce(m.kind, 'notify') NOT IN ('request', 'reply', 'event', 'receipt')
               AND m.sender <> 'ameesh' AND m.sender NOT LIKE 'human:%%'
               AND m.sender NOT LIKE 'chat-%%'
               AND NOT EXISTS (
                   SELECT 1 FROM agent_mailbox r
                    WHERE r.kind = 'request' AND (r.payload -> 'decision') IS NOT NULL
                      AND r.payload -> 'decision' ->> 'source_mail' = m.id::text)
             ORDER BY m.id DESC
             LIMIT %s
            """.replace("__COLUMNS__", _MAIL_COLUMNS),
            (float(since_ts), json.dumps(list(recipients)), int(limit)))[::-1]

    def register_chat(self, name, *, human, host, harness, cwd, status_text) -> dict | None:
        # L123 : un agent `execute` du même nom, ou un bail vivant, ne se
        # transforment jamais en conversation (rien n'est écrit : la ligne
        # n'est pas rendue). La session est oubliée : le hook de la nouvelle
        # session l'inscrit (`upsert_unleased` n'écrit qu'une session vide).
        rows = self.db.query(
            """
            __LOCK__
            INSERT INTO agent_registry
                (name, workstream, harness, host, cwd, session_id, status, status_text,
                 mode, responsible, created_by, last_seen, updated_at)
            SELECT %s, '', %s, %s, %s, NULL, 'idle', %s, 'externe', %s, %s, now(), now()
              FROM l
            ON CONFLICT (name) DO UPDATE SET
                harness = excluded.harness,
                host = excluded.host,
                cwd = excluded.cwd,
                session_id = NULL,
                session_account = NULL,
                status = CASE WHEN agent_registry.status IN ('stopped', 'dead')
                              THEN 'idle' ELSE agent_registry.status END,
                status_text = excluded.status_text,
                responsible = excluded.responsible,
                created_by = excluded.created_by,
                last_seen = now(),
                updated_at = now()
             WHERE agent_registry.mode = 'externe'
               AND (agent_registry.lease_owner IS NULL
                    OR agent_registry.lease_expires_at IS NULL
                    OR agent_registry.lease_expires_at <= clock_timestamp())
            RETURNING name, mode, responsible, harness, host
            """.replace("__LOCK__", NAME_LOCK_CTE),
            (name_lock_key(name), name, harness, host, cwd, status_text, human, human),
        )
        return rows[0] if rows else None
