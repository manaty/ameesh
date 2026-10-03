# SPDX-License-Identifier: AGPL-3.0-only
"""`agent_mailbox` : messages durables entre agents.

Le dépôt d'un message est la seule écriture nécessaire : un trigger Postgres
émet `pg_notify('agent_mail', …)`, ce qui réveille l'exécuteur du destinataire.
La remise (`mark_delivered`) est faite par le hook du harnais ou par l'exécuteur
après un tour : un message non remis est représenté par `delivered_at IS NULL`.
"""
from __future__ import annotations

import json
from typing import Any, Sequence

from .db import Db

MAIL_COLUMNS = """
    id, sender, recipient, body, kind, payload, status, host,
    signature, signature_key, signed_payload, nonce,
    extract(epoch from created_at)::float8 as created_ts,
    extract(epoch from delivered_at)::float8 as delivered_ts,
    extract(epoch from signature_expires_at)::float8 as signature_expires_ts
"""


def send(
    db: Db,
    sender: str,
    recipient: str,
    body: str,
    *,
    host: str | None = None,
    kind: str = "notify",
    payload: dict | None = None,
    work_item_id: str | None = None,
    signature: str | None = None,
    signature_key: str | None = None,
    signed_payload: str | None = None,
    nonce: str | None = None,
    created_us: int | None = None,
    expires_us: int | None = None,
) -> int:
    """Dépose un message pour UN destinataire. Renvoie son id.

    Les champs `signed_*` viennent de `authority.sign_message` : un message
    signé fixe son propre horodatage (c'est lui qui est couvert par la
    signature) et son échéance.
    """
    rows = db.query(
        """
        INSERT INTO agent_mailbox
            (sender, recipient, body, host, kind, payload, work_item_id,
             signature, signature_key, signed_payload, nonce,
             created_at, signature_expires_at)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s,
                coalesce(to_timestamp(%s::bigint / 1000000.0), now()),
                to_timestamp(%s::bigint / 1000000.0))
        RETURNING id
        """,
        (sender, recipient, body, host, kind,
         json.dumps(payload or {}, ensure_ascii=False), work_item_id,
         signature, signature_key, signed_payload, nonce,
         created_us, expires_us),
    )
    return int(rows[0]["id"])


def unread(db: Db, recipient: str, limit: int = 200) -> list[dict]:
    """Messages non remis, du plus ancien au plus récent (comme la v0)."""
    return db.query(
        """
        SELECT %s
        FROM agent_mailbox
        WHERE recipient = %%s AND delivered_at IS NULL
        ORDER BY id
        LIMIT %%s
        """ % MAIL_COLUMNS,
        (recipient, int(limit)),
    )


def mark_delivered(db: Db, ids: Sequence[int]) -> int:
    """Marque remis (le hook les a injectés, ou l'exécuteur a fini son tour)."""
    clean = sorted({int(i) for i in ids})
    if not clean:
        return 0
    rows = db.query(
        """
        UPDATE agent_mailbox
           SET delivered_at = now(), status = 'delivered'
         WHERE delivered_at IS NULL AND id IN (%s)
        RETURNING id
        """ % ", ".join(str(i) for i in clean)
    )
    return len(rows)


def unread_counts(db: Db) -> dict[str, int]:
    rows = db.query(
        """
        SELECT recipient, count(*)::int as n
        FROM agent_mailbox
        WHERE delivered_at IS NULL
        GROUP BY recipient
        """
    )
    return {row["recipient"]: int(row["n"]) for row in rows}


def history(db: Db, recipient: str, limit: int = 50) -> list[dict]:
    return db.query(
        """
        SELECT %s
        FROM agent_mailbox
        WHERE recipient = %%s
        ORDER BY id DESC
        LIMIT %%s
        """ % MAIL_COLUMNS,
        (recipient, int(limit)),
    )


def pending_recipients(db: Db) -> list[str]:
    return [row["recipient"] for row in db.query(
        "SELECT DISTINCT recipient FROM agent_mailbox WHERE delivered_at IS NULL"
    )]


def normalize(row: dict) -> dict:
    """Forme commune aux deux backends : from, to, ts, text (+ id, kind, payload)."""
    return {
        "id": row.get("id"),
        "from": row.get("sender"),
        "to": row.get("recipient"),
        "ts": row.get("created_ts") or 0.0,
        "text": row.get("body") or "",
        "kind": row.get("kind") or "notify",
        "payload": row.get("payload") or {},
        "host": row.get("host"),
        # champs d'autorité (R4) : absents en repli fichier, présents en base
        "signature": row.get("signature"),
        "signature_key": row.get("signature_key"),
        "signed_payload": row.get("signed_payload"),
        "nonce": row.get("nonce"),
        "signature_expires_ts": row.get("signature_expires_ts"),
    }


def message_payload(row: Any) -> dict:
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except ValueError:
            return {}
    return payload or {}
