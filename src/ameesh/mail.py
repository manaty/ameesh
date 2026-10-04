# SPDX-License-Identifier: AGPL-3.0-only
"""`agent_mailbox` : messages durables entre agents.

Le dépôt d'un message est la seule écriture nécessaire : un trigger Postgres
émet `pg_notify('agent_mail', …)`, ce qui réveille l'exécuteur du destinataire.
La remise (`mark_delivered`) est faite par le hook du harnais ou par l'exécuteur
après un tour : un message non remis est représenté par `delivered_at IS NULL`.

La boîte n'est que la file de distribution : chaque message déposé est aussi
écrit dans son **fil lisible** (`fil.record`, R12). Un corps illisible (vide,
JSON seul, binaire, bloc encodé) est refusé avant tout dépôt ; un fil
inaccessible ne fait jamais perdre le message.

Le SQL est dans le stockage (`storage.of(db).mailbox`, spec §10).
"""
from __future__ import annotations

import json
from typing import Any, Sequence

from . import fil, storage
from .db import Db
from .storage.postgres import mailbox as _pg

#: projet du fil d'un agent (`fil.agent_project`), en SQL : son équipe s'il est
#: gouverné par le canon, sinon son chantier — pilote Postgres, alias de
#: compatibilité (voir `storage.postgres.mailbox`)
PROJECT_SQL = _pg.PROJECT_SQL

#: colonnes rendues pour un message (pilote Postgres ; alias de compatibilité)
MAIL_COLUMNS = _pg.MAIL_COLUMNS


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
    allow_structured: bool = False,
    thread: bool = True,
    thread_meta: dict | None = None,
) -> int:
    """Dépose un message pour UN destinataire, l'écrit dans son fil. Renvoie son id.

    Les champs `signed_*` viennent de `authority.sign_message` : un message
    signé fixe son propre horodatage (c'est lui qui est couvert par la
    signature) et son échéance.

    `allow_structured` lève le refus des corps illisibles (tests et outils
    seulement). `thread=False` laisse l'écriture du fil à l'appelant (envoi à
    « all » regroupé en une entrée, import déjà écrit). Projet du fil : celui
    de l'expéditeur, sinon du destinataire — son équipe (`team`) s'il est
    gouverné par le canon, sinon son chantier —, sinon AMEESH_PROJECT, sinon
    `default` ; lot : `work_item_id`.
    """
    fil.ensure_readable(body, allow_structured)
    row = storage.of(db).mailbox.send(
        sender, recipient, body, host=host, kind=kind, payload=payload,
        work_item_id=work_item_id, signature=signature, signature_key=signature_key,
        signed_payload=signed_payload, nonce=nonce, created_us=created_us,
        expires_us=expires_us)
    message_id = int(row["id"])
    if thread:
        # Le message est déposé : à partir d'ici, rien ne doit le faire perdre
        # (fil.record ne lève jamais).
        meta = {"host": host, "signature_key": signature_key}
        if kind != "notify":
            meta["kind"] = kind
        meta.update(thread_meta or {})
        fil.record(
            db.cfg, db, sender=sender, recipients=[recipient], text=body,
            ts=row.get("created_ts"),
            project=fil.project_for(db.cfg, row.get("sender_project"),
                                    row.get("recipient_project")),
            lot=work_item_id, mailbox_ids=[message_id], meta=meta)
    return message_id


def unread(db: Db, recipient: str, limit: int = 200) -> list[dict]:
    """Messages non remis, du plus ancien au plus récent (comme la v0)."""
    return storage.of(db).mailbox.unread(recipient, limit)


def unread_urgent(db: Db, recipient: str, limit: int = 50) -> list[dict]:
    """Messages non remis marqués `payload.urgent` (interruption de tour, 0018)."""
    return storage.of(db).mailbox.unread_urgent(recipient, limit)


def get(db: Db, message_id: int) -> dict | None:
    """Un message par son id (l'écouteur NOTIFY ne porte que l'id)."""
    return storage.of(db).mailbox.get(message_id)


def mark_delivered(db: Db, ids: Sequence[int]) -> int:
    """Marque remis (le hook les a injectés, ou l'exécuteur a fini son tour)."""
    return storage.of(db).mailbox.mark_delivered(ids)


def unread_counts(db: Db) -> dict[str, int]:
    return storage.of(db).mailbox.unread_counts()


def history(db: Db, recipient: str, limit: int = 50) -> list[dict]:
    return storage.of(db).mailbox.history(recipient, limit)


def pending_recipients(db: Db) -> list[str]:
    return storage.of(db).mailbox.pending_recipients()


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


def is_event(row: Any) -> bool:
    return (row.get("kind") or "") == "event"


def is_urgent(row: Any) -> bool:
    """Un événement `urgent` perce le regroupement (C9)."""
    return bool(message_payload(row).get("urgent"))
