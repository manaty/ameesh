# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : boîte aux lettres (`agent_mailbox`).

SQL déplacé tel quel depuis `ameesh.mail` (lot L1) et, pour les destinataires
de `export-v0` et le compte des non-lus de `doctor`, depuis `ameesh.mesh_cli`
et `ameesh.cli` (phase 2). Le dépôt est la seule
écriture nécessaire : un trigger Postgres émet `pg_notify('agent_mail', …)`.
"""
from __future__ import annotations

import json

from .. import interface
from .registry import canon_governed_sql

#: projet du fil d'un agent (`fil.agent_project`), en SQL : son équipe s'il est
#: gouverné par le canon, sinon son chantier (un paramètre : le nom de l'agent)
PROJECT_SQL = (
    "SELECT CASE WHEN " + canon_governed_sql("r")
    + " AND coalesce(btrim(r.team), '') <> '' THEN btrim(r.team) ELSE r.chantier END"
    " FROM agent_registry r WHERE r.name = %s")

MAIL_COLUMNS = """
    id, sender, recipient, body, kind, payload, status, host,
    signature, signature_key, signed_payload, nonce,
    extract(epoch from created_at)::float8 as created_ts,
    extract(epoch from delivered_at)::float8 as delivered_ts,
    extract(epoch from signature_expires_at)::float8 as signature_expires_ts
"""


class Mailbox(interface.Mailbox):

    def send(self, sender, recipient, body, *, host, kind, payload, work_item_id,
             signature, signature_key, signed_payload, nonce, created_us,
             expires_us) -> dict:
        rows = self.db.query(
            """
            INSERT INTO agent_mailbox
                (sender, recipient, body, host, kind, payload, work_item_id,
                 signature, signature_key, signed_payload, nonce,
                 created_at, signature_expires_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s,
                    coalesce(to_timestamp(%s::bigint / 1000000.0), now()),
                    to_timestamp(%s::bigint / 1000000.0))
            RETURNING id, extract(epoch from created_at)::float8 AS created_ts,
                      (__PROJECT__) AS sender_project,
                      (__PROJECT__) AS recipient_project
            """.replace("__PROJECT__", PROJECT_SQL),
            (sender, recipient, body, host, kind,
             json.dumps(payload or {}, ensure_ascii=False), work_item_id,
             signature, signature_key, signed_payload, nonce,
             created_us, expires_us, sender, recipient),
        )
        return rows[0]

    def unread(self, recipient, limit) -> list[dict]:
        return self.db.query(
            """
            SELECT %s
            FROM agent_mailbox
            WHERE recipient = %%s AND delivered_at IS NULL
            ORDER BY id
            LIMIT %%s
            """ % MAIL_COLUMNS,
            (recipient, int(limit)),
        )

    def unread_urgent(self, recipient, limit) -> list[dict]:
        return self.db.query(
            """
            SELECT %s
            FROM agent_mailbox
            WHERE recipient = %%s AND delivered_at IS NULL
              AND payload @> '{"urgent": true}'::jsonb
            ORDER BY id
            LIMIT %%s
            """ % MAIL_COLUMNS,
            (recipient, int(limit)),
        )

    def get(self, message_id) -> dict | None:
        rows = self.db.query("SELECT %s FROM agent_mailbox WHERE id = %%s" % MAIL_COLUMNS,
                             (int(message_id),))
        return rows[0] if rows else None

    def mark_delivered(self, ids) -> int:
        # ids entiers seulement : ils entrent tels quels dans le texte SQL
        clean = sorted({int(i) for i in ids})
        if not clean:
            return 0
        rows = self.db.query(
            """
            UPDATE agent_mailbox
               SET delivered_at = now(), status = 'delivered'
             WHERE delivered_at IS NULL AND id IN (%s)
            RETURNING id
            """ % ", ".join(str(i) for i in clean)
        )
        return len(rows)

    def unread_counts(self) -> dict[str, int]:
        rows = self.db.query(
            """
            SELECT recipient, count(*)::int as n
            FROM agent_mailbox
            WHERE delivered_at IS NULL
            GROUP BY recipient
            """
        )
        return {row["recipient"]: int(row["n"]) for row in rows}

    def history(self, recipient, limit) -> list[dict]:
        return self.db.query(
            """
            SELECT %s
            FROM agent_mailbox
            WHERE recipient = %%s
            ORDER BY id DESC
            LIMIT %%s
            """ % MAIL_COLUMNS,
            (recipient, int(limit)),
        )

    def pending_recipients(self) -> list[str]:
        return [row["recipient"] for row in self.db.query(
            "SELECT DISTINCT recipient FROM agent_mailbox WHERE delivered_at IS NULL"
        )]

    def pending_recipients_sorted(self) -> list[str]:
        return [row["recipient"] for row in self.db.query(
            "SELECT DISTINCT recipient FROM agent_mailbox "
            "WHERE delivered_at IS NULL ORDER BY recipient")]

    def unread_total(self) -> int:
        return self.db.query(
            "SELECT count(*)::int AS n FROM agent_mailbox WHERE delivered_at IS NULL"
        )[0]["n"]
