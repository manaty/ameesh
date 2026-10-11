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
    extract(epoch from signature_expires_at)::float8 as signature_expires_ts,
    meta->'consigne'->>'runner' as consigne_runner,
    meta->'consigne'->>'jeton' as consigne_jeton,
    (meta->'consigne'->>'epoch')::bigint as consigne_epoch,
    (meta->'consigne' IS NOT NULL
     OR coalesce((meta->>'deja_consigne')::boolean, false)) as deja_consigne,
    meta->'forwarded_from'->>'agent' as forwarded_from
"""

#: courrier en souffrance : non remis, et dont le destinataire est absent du
#: registre ou arrêté (`stopped`) — personne ne le lira. Une ligne par
#: (destinataire, expéditeur) ; partagé avec la vue par projet
#: (`storage.postgres.projects`), qui le lit dans sa requête unique.
DEAD_LETTERS_SQL = """
    SELECT m.recipient, m.sender, count(*)::bigint AS n,
           extract(epoch from min(m.created_at))::float8 AS oldest_ts,
           extract(epoch from max(m.created_at))::float8 AS newest_ts,
           (r.name IS NULL) AS unknown
      FROM agent_mailbox m
      LEFT JOIN agent_registry r ON r.name = m.recipient
     WHERE m.delivered_at IS NULL
       AND (r.name IS NULL OR r.status = 'stopped')
       -- L124 : une demande de décision attend un humain (`human:<id>`),
       -- jamais un agent : elle n'est pas en souffrance
       AND NOT (m.kind = 'request' AND (m.payload -> 'decision') IS NOT NULL)
     GROUP BY m.recipient, m.sender, r.name
"""

#: une réservation ACTIVE (non échue) : le message est peut-être en train
#: d'être montré ; un renvoi ne le prend pas
_RESERVED_SQL = """
    m.meta->'consigne' IS NOT NULL
    AND coalesce((m.meta->'consigne'->>'expire')::float8, 0)
        > extract(epoch from clock_timestamp())
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

    # -- réservation : un seul protocole pour l'exécuteur et le hook ----------
    #
    # Toute remise passe par : réserver (jeton unique) → montrer → solder la
    # réservation qui porte CE jeton. Chaque instruction verrouille d'abord la
    # ligne du destinataire dans `agent_registry` (FOR UPDATE), comme les
    # opérations fencées du registre : réservations et remises d'un même
    # destinataire sont sérialisées, et le bail (owner, epoch, échéance) est
    # contrôlé sur la version courante de la ligne, après l'attente éventuelle.
    #
    # Une réservation est ACTIVE tant qu'elle n'a pas expiré et qu'elle
    # appartient au bail courant (ou à une identité explicite, sans bail). Une
    # réservation inactive (panne, bail repris) peut être reprise : le message
    # est alors marqué `deja_consigne` et signalé « re-livré ».

    @staticmethod
    def _bail_sql(owner, *, reservation=False) -> str:
        if owner:
            return ("v.lease_owner = %s AND v.lease_epoch = %s"
                    " AND v.lease_expires_at > clock_timestamp()")
        if reservation:
            # L46 : une identité sans bail (explicite, liaison de session) ne
            # réserve JAMAIS le courrier d'un agent qui détient un bail vivant
            # — il est mené par l'exécuteur, qui le lui remet dans ses tours.
            # Contrôlé sous le verrou de la ligne, dans l'instruction même.
            return ("(v.lease_owner IS NULL OR v.lease_expires_at IS NULL"
                    " OR v.lease_expires_at <= clock_timestamp())")
        return "TRUE"

    @staticmethod
    def _bail_params(owner, epoch) -> tuple:
        return (owner, int(epoch)) if owner else ()

    ACTIVE_SQL = """
        m.meta->'consigne' IS NOT NULL
        AND coalesce((m.meta->'consigne'->>'expire')::float8, 0)
            > extract(epoch from clock_timestamp())
        AND (m.meta->'consigne'->>'epoch' IS NULL
             OR (m.meta->'consigne'->>'runner' = v.lease_owner
                 AND (m.meta->'consigne'->>'epoch')::bigint = v.lease_epoch))
    """

    def reserve(self, recipient, owner, epoch, token, *, ids=None, porteur,
                ttl_seconds, limit=200) -> list[dict]:
        filtre = ""
        if ids is not None:
            clean = sorted({int(i) for i in ids})
            if not clean:
                return []
            filtre = "AND m.id IN (%s)" % ", ".join(str(i) for i in clean)
        sql = """
            WITH v AS (
                SELECT name, lease_owner, lease_epoch, lease_expires_at
                  FROM agent_registry WHERE name = %%s FOR UPDATE
            ), cibles AS (
                SELECT m.id, (m.meta->'consigne' IS NOT NULL
                              OR coalesce((m.meta->>'deja_consigne')::boolean, false))
                             AS deja
                  FROM agent_mailbox m, v
                 WHERE m.recipient = %%s AND m.delivered_at IS NULL %s
                   AND %s
                   AND NOT (%s)
                 ORDER BY m.id
                 LIMIT %%s
                 FOR UPDATE OF m
            )
            UPDATE agent_mailbox AS m
               SET meta = m.meta || jsonb_build_object(
                       'consigne', jsonb_build_object(
                           'runner', %%s::text, 'epoch', %%s::bigint,
                           'jeton', %%s::text, 'porteur', %%s::text,
                           'ts', extract(epoch from clock_timestamp())::float8,
                           'expire', extract(epoch from clock_timestamp())::float8
                                     + %%s::float8),
                       'deja_consigne', cibles.deja)
              FROM cibles
             WHERE m.id = cibles.id
            RETURNING %s, cibles.deja AS deja_avant
        """ % (filtre, self._bail_sql(owner, reservation=True), self.ACTIVE_SQL,
               MAIL_COLUMNS.replace("id,", "m.id,", 1))
        params = ((recipient, recipient) + self._bail_params(owner, epoch)
                  + (int(limit), owner or None, int(epoch) if owner else None,
                     token, porteur, float(ttl_seconds)))
        rows = self.db.query(sql, params)
        for row in rows:
            # l'état AVANT cette réservation : « re-livré » si une réservation
            # antérieure n'a jamais été soldée
            row["deja_consigne"] = bool(row.pop("deja_avant", False))
        return sorted(rows, key=lambda row: int(row["id"]))

    def deliver(self, recipient, owner, epoch, token, ids) -> list[int]:
        clean = sorted({int(i) for i in ids})
        if not clean:
            return []
        rows = self.db.query(
            """
            WITH v AS (
                SELECT name, lease_owner, lease_epoch, lease_expires_at
                  FROM agent_registry WHERE name = %%s FOR UPDATE
            )
            UPDATE agent_mailbox AS m
               SET delivered_at = now(), status = 'delivered'
              FROM v
             WHERE m.recipient = %%s AND m.delivered_at IS NULL AND m.id IN (%s)
               AND m.meta->'consigne'->>'jeton' = %%s
               AND %s
            RETURNING m.id
            """ % (", ".join(str(i) for i in clean), self._bail_sql(owner)),
            (recipient, recipient, token) + self._bail_params(owner, epoch),
        )
        return sorted(int(row["id"]) for row in rows)

    def release(self, recipient, token, ids) -> int:
        clean = sorted({int(i) for i in ids})
        if not clean:
            return 0
        rows = self.db.query(
            """
            WITH v AS (
                SELECT name FROM agent_registry WHERE name = %%s FOR UPDATE
            )
            UPDATE agent_mailbox AS m
               SET meta = m.meta - 'consigne'
              FROM v
             WHERE m.recipient = %%s AND m.delivered_at IS NULL AND m.id IN (%s)
               AND m.meta->'consigne'->>'jeton' = %%s
            RETURNING m.id
            """ % ", ".join(str(i) for i in clean),
            (recipient, recipient, token),
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

    # -- courrier en souffrance ----------------------------------------------
    def dead_letters(self) -> list[dict]:
        return self.db.query(DEAD_LETTERS_SQL + " ORDER BY m.recipient, m.sender")

    def forward(self, old, new, by) -> list[dict]:
        # UNE transaction : verrou des messages en attente de `old` (hors
        # réservation active), copie pour `new` (expéditeur, corps, nature,
        # charge, lot, hôte et date d'origine ; la signature, qui couvre le
        # destinataire, ne suit pas), puis l'original remis et noté. Un
        # message pris entre-temps par une remise (verrou de ligne, WHERE
        # réévalué) n'est pas renvoyé.
        with self.db.transaction() as tx:
            ids = sorted(int(row["id"]) for row in tx.query(
                "SELECT m.id FROM agent_mailbox m"
                " WHERE m.recipient = %%s AND m.delivered_at IS NULL"
                "   AND NOT (%s)"
                " ORDER BY m.id FOR UPDATE" % _RESERVED_SQL, (old,)))
            if not ids:
                return []
            copies = tx.query(
                """
                INSERT INTO agent_mailbox
                    (sender, recipient, body, kind, payload, work_item_id, host,
                     created_at, meta)
                SELECT m.sender, %%s, m.body, m.kind, m.payload, m.work_item_id, m.host,
                       m.created_at,
                       (m.meta - 'consigne' - 'deja_consigne' - 'forwarded')
                       || jsonb_build_object('forwarded_from', jsonb_build_object(
                              'agent', m.recipient, 'message_id', m.id, 'by', %%s::text,
                              'ts', extract(epoch from clock_timestamp())::float8,
                              'signed', m.signature IS NOT NULL))
                  FROM agent_mailbox m
                 WHERE m.id IN (%s)
                 ORDER BY m.id
                RETURNING id, (meta->'forwarded_from'->>'message_id')::bigint AS original_id
                """ % ", ".join(str(i) for i in ids),
                (new, by))
            pairs = sorted((int(c["original_id"]), int(c["id"])) for c in copies)
            rows = tx.query(
                """
                UPDATE agent_mailbox AS m
                   SET delivered_at = now(), status = 'delivered',
                       meta = (m.meta - 'consigne')
                              || jsonb_build_object('forwarded', jsonb_build_object(
                                     'to', %%s::text, 'message_id', v.new_id,
                                     'by', %%s::text,
                                     'ts', extract(epoch from clock_timestamp())::float8))
                  FROM (VALUES %s) AS v(original_id, new_id)
                 WHERE m.id = v.original_id
                RETURNING m.id AS original_id, v.new_id, m.sender, m.kind, m.work_item_id,
                          extract(epoch from m.created_at)::float8 AS created_ts
                """ % ", ".join("(%d, %d)" % pair for pair in pairs),
                (new, by))
        return sorted(rows, key=lambda row: int(row["original_id"]))
