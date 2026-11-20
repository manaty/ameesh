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
from .registry import (
    NAME_LOCK_SQL, NOT_EXTERNAL_SQL, PASSIVE_SQL, canon_governed_sql, name_lock_key)

#: projet du fil d'un agent (`fil.agent_project`), en SQL : son équipe s'il est
#: gouverné par le canon, sinon son chantier, désormais `workstream` (un
#: paramètre : le nom de l'agent)
PROJECT_SQL = (
    "SELECT CASE WHEN " + canon_governed_sql("r")
    + " AND coalesce(btrim(r.team), '') <> '' THEN btrim(r.team) ELSE r.workstream END"
    " FROM agent_registry r WHERE r.name = %s")

#: colonnes d'un message rendues aux appelants. `work_item_id` (#811 R3) porte
#: le lot du message : la règle du dossier de lot (`lot_workdir.deliverable`)
#: s'applique alors sans une requête par ligne.
MAIL_COLUMNS = """
    id, sender, recipient, body, kind, payload, status, host,
    coalesce(btrim(work_item_id), '') as work_item_id,
    in_reply_to,
    signature, signature_key, signed_payload, nonce,
    extract(epoch from created_at)::float8 as created_ts,
    extract(epoch from delivered_at)::float8 as delivered_ts,
    extract(epoch from signature_expires_at)::float8 as signature_expires_ts,
    meta->'consigne'->>'runner' as consigne_runner,
    meta->'consigne'->>'jeton' as consigne_jeton,
    (meta->'consigne'->>'epoch')::bigint as consigne_epoch,
    meta->'consigne'->>'turn' as consigne_turn,
    (meta->'consigne' IS NOT NULL
     OR coalesce((meta->>'deja_consigne')::boolean, false)) as deja_consigne,
    meta->'forwarded_from'->>'agent' as forwarded_from
"""

#: courrier en souffrance : non remis, et dont le destinataire est absent du
#: registre ou arrêté (`stopped`) — personne ne le lira. Une ligne par
#: (destinataire, expéditeur) ; partagé avec la vue par projet
#: (`storage.postgres.projects`), qui le lit dans sa requête unique.
#: #6424 : une boîte `externe` (session humaine hors exécuteur) n'est jamais
#: morte : son courrier attend sa prochaine session, il n'est ni compté ni
#: expiré comme une souffrance.
DEAD_LETTERS_SQL = """
    SELECT m.recipient, m.sender, count(*)::bigint AS n,
           extract(epoch from min(m.created_at))::float8 AS oldest_ts,
           extract(epoch from max(m.created_at))::float8 AS newest_ts,
           (r.name IS NULL) AS unknown
      FROM agent_mailbox m
      LEFT JOIN agent_registry r ON r.name = m.recipient
     WHERE m.delivered_at IS NULL
       AND (r.name IS NULL OR r.status = 'stopped')
       AND coalesce(r.mode, 'execute') <> 'externe'
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

#: L125 : le prédicat d'un message PASSIF (`PASSIVE_SQL`) vit dans
#: `registry` depuis #5586 (le compte groupé d'`overview()` le lit sans import
#: circulaire) ; il reste exporté ici pour ses lecteurs.

#: `human_messages` : les messages d'humains depuis `since_ts`, lus par les
#: index partiels de 0558 au lieu d'un parcours de `agent_mailbox_recent_idx`
#: (732 ms sur 201 k lignes, #6422). La CTE ne rend que des ids : chaque
#: branche a son index — humain ou payload `human`, chat externe (le registre
#: donne `mode`), noms humains déclarés (anciens messages sans marqueur, sur
#: `agent_mailbox_sender_idx`) — et la jointure finale part de la clé primaire.
HUMAN_MESSAGES_SQL = """
    WITH candidates AS (
        SELECT id FROM agent_mailbox
         WHERE created_at >= to_timestamp(%s)
           AND (starts_with(sender, 'human:')
                OR payload @> '{"human": true}'::jsonb)
      UNION
        SELECT m.id FROM agent_mailbox m
         WHERE m.created_at >= to_timestamp(%s)
           AND starts_with(m.sender, 'chat-')
           AND EXISTS (SELECT 1 FROM agent_registry r
                        WHERE r.name = m.sender
                          AND coalesce(r.mode, 'execute') = 'externe')
        __DECLARED__
    )
    SELECT m.id, m.sender, m.recipient, m.body, m.kind, m.payload,
           m.work_item_id, w.id AS lot_id,
           extract(epoch from m.created_at)::float8 AS created_ts
      FROM agent_mailbox m
      JOIN candidates c ON c.id = m.id
      LEFT JOIN work_items w
        ON w.id = CASE WHEN btrim(coalesce(m.work_item_id, '')) ~ '^[0-9]{1,18}$'
                       THEN btrim(m.work_item_id)::bigint END
     WHERE NOT m.payload @> '{"ack": true}'::jsonb
       AND coalesce(m.payload ->> 'cc', '') = ''
       AND NOT (m.kind = 'request' AND (m.payload -> 'decision') IS NOT NULL)
     ORDER BY m.id DESC
     LIMIT %s
"""


def human_messages_query(*, since_ts, humans, limit) -> tuple[str, list]:
    """(SQL, paramètres) de `human_messages` : la CTE de 0558, construite pour
    les noms humains déclarés (branche absente s'il n'y en a aucun)."""
    names = sorted({str(n) for n in humans or () if str(n).strip()})
    declared = (
        "  UNION\n"
        "    SELECT id FROM agent_mailbox\n"
        "     WHERE created_at >= to_timestamp(%s)\n"
        "       AND sender IN (__NAMES__)"
    ).replace("__NAMES__", ", ".join(["%s"] * len(names))) if names else ""
    params = [float(since_ts), float(since_ts)]
    if names:
        params.append(float(since_ts))
        params.extend(names)
    params.append(int(limit))
    return HUMAN_MESSAGES_SQL.replace("__DECLARED__", declared), params


class Mailbox(interface.Mailbox):

    def lock_recipient(self, recipient):
        # L148 : `workstream` est le nom courant ; `chantier` reste émis, à la
        # même valeur, pour les lecteurs qui ne sont pas encore renommés.
        rows = self.db.query(
            "SELECT name, workstream, workstream AS chantier"
            " FROM agent_registry WHERE name = %s FOR SHARE",
            (recipient,))
        return rows[0] if rows else None

    def send(self, sender, recipient, body, *, host, kind, payload, work_item_id,
             signature, signature_key, signed_payload, nonce, created_us,
             expires_us) -> dict:
        # #6641: all structured review writers share the lot lock, including
        # manual requests and automatic paths outside PgPort. No network work
        # runs under this lock. Reservations lock the mailbox row too: once a
        # request may have been shown, it must never be rewritten.
        review = (payload or {}).get('review_designation')
        manual = (payload or {}).get('review_request')
        args = dict(host=host, kind=kind, payload=payload, work_item_id=work_item_id,
                    signature=signature, signature_key=signature_key,
                    signed_payload=signed_payload, nonce=nonce,
                    created_us=created_us, expires_us=expires_us)
        if kind == 'request' and work_item_id and (isinstance(review, dict)
                                                  or isinstance(manual, dict)):
            with self.db.transaction() as tx:
                box = Mailbox(tx)
                return box._send_review(sender, recipient, body, args,
                                        automatic=isinstance(review, dict))
        return self._insert(sender, recipient, body, **args)

    def send_reply(self, sender, recipient, body, *, host, kind, payload,
                   work_item_id, signature, signature_key, signed_payload,
                   nonce, created_us, expires_us, in_reply_to) -> dict:
        """#7672 : dépôt corrélé, réservé au chemin public (voir l'interface)."""
        return self._insert(
            sender, recipient, body, host=host, kind=kind, payload=payload,
            work_item_id=work_item_id, signature=signature,
            signature_key=signature_key, signed_payload=signed_payload,
            nonce=nonce, created_us=created_us, expires_us=expires_us,
            in_reply_to=in_reply_to)

    def _send_review(self, sender, recipient, body, args, *, automatic):
        from ...review_designation import _request_sha
        lot = int(args['work_item_id'])
        item = self.db.query(
            'SELECT id, state FROM work_items WHERE id = %s FOR NO KEY UPDATE', (lot,))
        if not item:
            raise ValueError('review request refused: lot not found')
        registered = self.db.query(
            'SELECT retired_at FROM agent_registry WHERE name = %s FOR SHARE', (recipient,))
        if registered and registered[0].get('retired_at') is not None:
            raise interface.RecipientRetired('%s is retired: nothing deposited' % recipient)
        sha = _request_sha({'payload': args['payload']}) or ''
        if not automatic:
            if not sha:
                freeze = self.db.query(
                    "SELECT sha FROM work_item_milestones WHERE work_item_id = %s"
                    " AND kind = 'frozen' ORDER BY at DESC, id DESC LIMIT 1", (lot,))
                sha = str(freeze[0].get('sha') or '').lower() if freeze else ''
            verdict = self.db.query(
                "SELECT id FROM work_item_milestones WHERE work_item_id = %s"
                " AND kind = 'verdict' AND actor = %s AND lower(sha) = %s LIMIT 1",
                (lot, recipient, sha)) if sha else []
            pending = self.db.query(
                "SELECT id FROM agent_mailbox WHERE recipient = %s AND work_item_id = %s"
                " AND kind = 'request' AND delivered_at IS NULL"
                " AND (payload ? 'review_designation' OR payload ? 'review_request') LIMIT 1",
                (recipient, str(lot)))
            if verdict or pending:
                raise ValueError('review request refused: ' + (
                    'verdict already recorded on this head' if verdict else
                    'review designation or request pending'))
        else:
            candidates = self.db.query(
                "SELECT id, payload FROM agent_mailbox WHERE recipient = %s"
                " AND work_item_id = %s AND kind = 'request' AND delivered_at IS NULL"
                " AND ((payload ? 'review_designation' AND sender = %s)"
                " OR payload ? 'review_request')"
                " AND signature IS NULL AND meta->'consigne' IS NULL"
                " AND NOT coalesce((meta->>'deja_consigne')::boolean, false)"
                " ORDER BY id ASC FOR UPDATE", (recipient, str(lot), sender))
            if candidates and not args['signature']:
                prior = candidates[0]
                # Recheck in the UPDATE as well: a read/reservation wins if it
                # acquired the row first. Keep the original queue age so rapid
                # freezes cannot postpone delivery indefinitely.
                rows = self.db.query(
                    "UPDATE agent_mailbox SET body = %s, payload = %s::jsonb, sender = %s"
                    " WHERE id = %s AND delivered_at IS NULL AND meta->'consigne' IS NULL"
                    " AND NOT coalesce((meta->>'deja_consigne')::boolean, false)"
                    " RETURNING id, extract(epoch from created_at)::float8 AS created_ts",
                    (body, json.dumps(args['payload'], ensure_ascii=False), sender, prior['id']))
                if rows:
                    from .work import WorkItems
                    WorkItems(self.db).note(lot, item[0]['state'],
                        'review-designation-replaced: ' + json.dumps({
                            'message': int(prior['id']), 'reviewer': recipient,
                            'old_sha': _request_sha(prior), 'new_sha': sha}, sort_keys=True), sender)
                    # Retire legacy duplicates only while never exposed. All
                    # candidates are locked, serializing with reservations.
                    for stale in candidates[1:]:
                        retired = self.db.query(
                            "UPDATE agent_mailbox SET delivered_at = clock_timestamp(),"
                            " status = 'delivered' WHERE id = %s AND delivered_at IS NULL"
                            " AND meta->'consigne' IS NULL"
                            " AND NOT coalesce((meta->>'deja_consigne')::boolean, false)"
                            " RETURNING id", (stale['id'],))
                        if retired:
                            WorkItems(self.db).note(lot, item[0]['state'],
                                'review-designation-replaced: ' + json.dumps({
                                    'message': int(stale['id']), 'reviewer': recipient,
                                    'old_sha': _request_sha(stale), 'new_sha': sha,
                                    'retired': True, 'replacement': int(prior['id'])},
                                    sort_keys=True), sender)
                    return rows[0]
        return self._insert(sender, recipient, body, **args)

    def _insert(self, sender, recipient, body, *, host, kind, payload, work_item_id,
                signature, signature_key, signed_payload, nonce, created_us,
                expires_us, in_reply_to=None) -> dict:
        # #3460 : un retiré n'est jamais destinataire. Le retrait est relu
        # dans l'instruction qui dépose, sous un verrou partagé de sa ligne
        # (`retire` la prend en exclusif) : un retrait committé après un
        # précontrôle gagne, un retrait en cours attend ce dépôt. Un nom hors
        # du registre (humain, service) n'a pas de ligne et reste déposé.
        #
        # #7672/#6881 : le courrier ordinaire ne NOMME pas `in_reply_to`.
        # Postgres contrôle une colonne nommée dans l'INSERT même quand sa
        # valeur est NULL, et le droit d'INSERT par colonne du bureau ne
        # l'inclut pas (aucun GRANT élargi pour un détail de corrélation).
        # Seul `send_reply` nomme la colonne ; le chemin ordinaire ne la lit
        # pas non plus dans son RETURNING.
        reply = in_reply_to is not None
        rows = self.db.query(
            """
            WITH registered AS (
                SELECT retired_at FROM agent_registry
                 WHERE name = %s OR %s = ANY(aliases) FOR SHARE)
            INSERT INTO agent_mailbox
                (sender, recipient, body, host, kind, payload, work_item_id,
                 __REPLY_COLUMN__signature, signature_key, signed_payload, nonce,
                 created_at, signature_expires_at)
            SELECT %s, %s, %s, %s, %s, %s::jsonb, %s, __REPLY_VALUE__%s, %s, %s, %s,
                   coalesce(to_timestamp(%s::bigint / 1000000.0), now()),
                   to_timestamp(%s::bigint / 1000000.0)
             WHERE NOT EXISTS (SELECT 1 FROM registered WHERE retired_at IS NOT NULL)
            RETURNING id, extract(epoch from created_at)::float8 AS created_ts,
                      __REPLY_RETURN__(__SENDER_PROJECT__) AS sender_project,
                      (__RECIPIENT_PROJECT__) AS recipient_project
            """.replace("__REPLY_COLUMN__",
                        "in_reply_to,\n                 " if reply else "")
               .replace("__REPLY_VALUE__", "%s, " if reply else "")
               .replace("__REPLY_RETURN__",
                        "in_reply_to,\n                      " if reply else "")
               .replace("__SENDER_PROJECT__", PROJECT_SQL % "agent_mailbox.sender")
               .replace("__RECIPIENT_PROJECT__", PROJECT_SQL % "agent_mailbox.recipient"),
            # #4299: the stored names, which 0516 maps from an alias.
            (recipient, recipient, sender, recipient, body, host, kind,
             json.dumps(payload or {}, ensure_ascii=False), work_item_id)
            + (() if in_reply_to is None else (int(in_reply_to),))
            + (signature, signature_key, signed_payload, nonce,
               created_us, expires_us),
        )
        if not rows:
            raise interface.RecipientRetired(
                "%s is retired: nothing deposited (ameesh agent restore %s)"
                % (recipient, recipient))
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

    def unread_active(self, recipient, limit, *, after_id=None,
                      since=None) -> list[dict]:
        # #7861 : les bornes sont dans la requête, avant LIMIT : un préfixe
        # de plus de `limit` non-lus ne cache ni les suivants (curseur), ni
        # ceux créés depuis `since`.
        params = [recipient]
        apres = ""
        if after_id is not None:
            apres = " AND id > %s"
            params.append(int(after_id))
        depuis = ""
        if since is not None:
            depuis = " AND created_at >= to_timestamp(%s)"
            params.append(float(since))
        params.append(int(limit))
        return self.db.query(
            """
            SELECT %s
            FROM agent_mailbox
            WHERE recipient = %%s AND delivered_at IS NULL
              AND NOT %s
              %s%s
            ORDER BY id
            LIMIT %%s
            """ % (MAIL_COLUMNS, PASSIVE_SQL.format(t="agent_mailbox"),
                   apres, depuis),
            tuple(params),
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

    def unread_human(self, recipient, limit, *, names=None) -> list[dict]:
        """L77/B13 : les non-lus d'un humain ACTIF, filtrés DANS la requête —
        le préfixe des `UNREAD_LIMIT` non-lus ordinaires ne les masque plus.

        `names` (None = aucun) couvre les noms déclarés humains sur l'hôte
        (ancien `AMEESH_HUMANS`) ; `payload.human` et l'ancien
        `sender human:*` restent reconnus, comme `mail.is_human`.

        L77/B14 : les passifs (copie `cc`, accusé…) sont écartés par la même
        règle que `mail.passive_reason` (`PASSIVE_SQL`). Sans ce filtre, 200
        copies humaines passives remplissaient la fenêtre devant un humain
        actif et le rang 2 le manquait encore.
        """
        return self._unread_human(recipient, limit, names=names, after_id=None)

    def unread_human_page(self, recipient, limit, *, after_id=None, names=None) -> list[dict]:
        """L77/B14 : la page suivante des humains actifs, après `after_id`.

        Le serveur médié borne chaque page à 200 : un préfixe de plus de 200
        humains retenus se franchit par curseur (`id > after_id`), sans
        livrer les retenus."""
        return self._unread_human(recipient, limit, names=names, after_id=after_id)

    def _unread_human(self, recipient, limit, *, names, after_id) -> list[dict]:
        names = [str(name) for name in (names or ()) if str(name)]
        # `IN` explicite plutôt qu'un tableau lié : le pilote psql rend les
        # listes Python en littéral texte, pas en `text[]` (#286).
        declares = ""
        params = [recipient]
        apres = ""
        if after_id is not None:
            apres = " AND id > %s"
            params.append(int(after_id))
        if names:
            declares = " OR sender IN (" + ", ".join(["%s"] * len(names)) + ")"
            params.extend(names)
        params.append(int(limit))
        return self.db.query(
            """
            SELECT %s
            FROM agent_mailbox
            WHERE recipient = %%s AND delivered_at IS NULL%s
              AND (payload @> '{"human": true}'::jsonb
                   OR starts_with(sender, 'human:')%s)
              AND NOT %s
            ORDER BY id
            LIMIT %%s
            """ % (MAIL_COLUMNS, apres, declares,
                   PASSIVE_SQL.format(t="agent_mailbox")),
            params,
        )

    def unread_mid_turn(self, recipient, limit, *, include_human=True) -> list[dict]:
        """L514/#885 : le filtre de `mail.mid_turn` DANS la requête — un
        verdict, un arbitrage, un message humain ou urgent, sans borne sur les
        autres non-lus (les 200 premiers ne sont pas un plafond de lecture).

        L77/B11 : `include_human=False` écarte tout message humain au sens de
        `mail.is_human` — `payload.human` (même urgent) ou ancien
        `sender human:*` — ; la file du tour résident les réserve et les solde.
        """
        humains = "" if include_human else (
            " AND NOT (m.payload @> '{\"human\": true}'::jsonb"
            " OR starts_with(m.sender, 'human:'))")
        return self.db.query(
            """
            SELECT %s
            FROM agent_mailbox m
            WHERE recipient = %%s AND delivered_at IS NULL
              AND (payload @> '{"urgent": true}'::jsonb
                   OR payload @> '{"human": true}'::jsonb
                   OR payload @> '{"verdict": true}'::jsonb
                   OR payload @> '{"arbitration": true}'::jsonb
                   OR payload ? 'reassigned')%s
              AND NOT EXISTS (
                  SELECT 1 FROM agent_registry v
                  WHERE v.name = m.recipient AND (%s)
              )
            ORDER BY id
            LIMIT %%s
            """ % (MAIL_COLUMNS, humains, self.ACTIVE_SQL),
            (recipient, int(limit)),
        )

    def by_idempotency_key(self, sender, recipient, key) -> int | None:
        rows = self.db.query(
            "SELECT id FROM agent_mailbox WHERE sender = %s AND recipient = %s"
            " AND payload->>'idempotency_key' = %s ORDER BY id LIMIT 1",
            (sender, recipient, key))
        return int(rows[0]["id"]) if rows else None

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
               SET delivered_at = clock_timestamp(), status = 'delivered'
             WHERE delivered_at IS NULL AND id IN (%s)
            RETURNING id
            """ % ", ".join(str(i) for i in clean)
        )
        return len(rows)

    def withdraw(self, ids, note) -> list[int]:
        clean = sorted({int(i) for i in ids})
        if not clean:
            return []
        rows = self.db.query(
            """
            UPDATE agent_mailbox AS m
               SET delivered_at = clock_timestamp(), status = 'delivered',
                   meta = m.meta || jsonb_build_object('retire', %%s::jsonb)
             WHERE m.delivered_at IS NULL AND m.id IN (%s)
               AND m.sender = 'ameesh' AND m.payload ? 'ci'
               AND NOT (m.meta->'consigne' IS NOT NULL
                        AND coalesce((m.meta->'consigne'->>'expire')::float8, 0)
                            > extract(epoch from clock_timestamp()))
            RETURNING m.id
            """ % ", ".join(str(i) for i in clean),
            (json.dumps(note, ensure_ascii=False),))
        return sorted(int(row["id"]) for row in rows)

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
    def _lease_sql(owner, *, reservation=False) -> str:
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
    def _lease_params(owner, epoch) -> tuple:
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
                ttl_seconds, limit=200, turn_id=None) -> list[dict]:
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
                           'turn', %%s::text,
                           'ts', extract(epoch from clock_timestamp())::float8,
                           'expire', extract(epoch from clock_timestamp())::float8
                                     + %%s::float8),
                       'deja_consigne', cibles.deja)
              FROM cibles
             WHERE m.id = cibles.id
            RETURNING %s, cibles.deja AS deja_avant
        """ % (filtre, self._lease_sql(owner, reservation=True), self.ACTIVE_SQL,
               MAIL_COLUMNS.replace("id,", "m.id,", 1))
        params = ((recipient, recipient) + self._lease_params(owner, epoch)
                  + (int(limit), owner or None, int(epoch) if owner else None,
                     token, porteur, turn_id, float(ttl_seconds)))
        rows = self.db.query(sql, params)
        for row in rows:
            # l'état AVANT cette réservation : « re-livré » si une réservation
            # antérieure n'a jamais été soldée
            row["deja_consigne"] = bool(row.pop("deja_avant", False))
        return sorted(rows, key=lambda row: int(row["id"]))

    def deliver(self, recipient, owner, epoch, token, ids) -> list[int]:
        # #796 : `clock_timestamp()`, pas `now()` — l'horodatage de remise est
        # celui de CHAQUE message (l'instant réel où il entre dans le contexte,
        # ici l'injection par le hook ou le lancement du tour), pas l'instant
        # de la transaction, identique pour tout un lot. C'est ce qui rend le
        # délai de réaction (created_at → delivered_at) mesurable.
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
               SET delivered_at = clock_timestamp(), status = 'delivered'
              FROM v
             WHERE m.recipient = %%s AND m.delivered_at IS NULL AND m.id IN (%s)
               AND m.meta->'consigne'->>'jeton' = %%s
               AND %s
            RETURNING m.id
            """ % (", ".join(str(i) for i in clean), self._lease_sql(owner)),
            (recipient, recipient, token) + self._lease_params(owner, epoch),
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

    def requeue_interrupted(self, recipient, owner, epoch, token, turn_id,
                            ids) -> dict:
        """#7673 (E2c) : voir `interface.Mailbox.requeue_interrupted`.

        Deux instructions dans une transaction, jamais une seule : la réponse
        corrélée (`public.mail.reply`) verrouille la même ligne du registre
        avant d'écrire, donc une réponse concurrente est soit validée avant
        notre verrou — et l'instantané de la SECONDE instruction, pris après
        lui, la voit —, soit attend notre commit et trouve une demande dont la
        consigne a disparu (refus « pas la consigne du tour courant »). Une
        seule instruction garderait l'instantané d'avant l'attente et
        remettrait en attente une demande déjà répondue.
        """
        clean = sorted({int(i) for i in ids})
        result = {"guard": "missing", "requeued": [], "replied": []}
        if not clean:
            return result
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT lease_owner, lease_epoch FROM agent_registry"
                " WHERE name = %s FOR UPDATE", (recipient,))
            if not rows:
                return result
            held, at = rows[0]["lease_owner"], rows[0]["lease_epoch"]
            if held == owner and int(at) == int(epoch):
                result["guard"] = "held"
            elif held is None and int(at) == int(epoch) + 1:
                result["guard"] = "released"
            else:
                result["guard"] = "refused"
                return result
            capture = """
                m.recipient = %s AND m.id = ANY(%s::bigint[])
                AND m.kind = 'request'
                AND m.meta->'consigne'->>'runner' = %s
                AND m.meta->'consigne'->>'epoch' = %s
                AND m.meta->'consigne'->>'jeton' = %s
                AND (%s::text IS NULL OR m.meta->'consigne'->>'turn' = %s::text)
                AND NOT coalesce((m.meta ? 'retire'), false)
            """
            params = (recipient, clean, owner, str(int(epoch)), token,
                      turn_id, turn_id)
            requeued = tx.query(
                """
                UPDATE agent_mailbox AS m
                   SET delivered_at = NULL, status = 'pending',
                       meta = (m.meta - 'consigne') || jsonb_build_object(
                           'deja_consigne', true,
                           'remise_interrompue', jsonb_build_object(
                               'runner', %%s::text, 'epoch', %%s::bigint,
                               'turn', %%s::text,
                               'ts', extract(epoch from clock_timestamp())::float8))
                 WHERE %s
                   AND NOT EXISTS (SELECT 1 FROM agent_mailbox r
                                    WHERE r.in_reply_to = m.id)
                RETURNING m.id
                """ % capture,
                (owner, int(epoch), turn_id) + params)
            replied = tx.query(
                "SELECT m.id FROM agent_mailbox m WHERE " + capture
                + " AND EXISTS (SELECT 1 FROM agent_mailbox r"
                  " WHERE r.in_reply_to = m.id)",
                params)
        result["requeued"] = sorted(int(row["id"]) for row in requeued)
        result["replied"] = sorted(int(row["id"]) for row in replied)
        return result

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

    def history_page(self, actor, *, peer=None, after_id=None, limit=100,
                     since_ts=None, until_ts=None, kind=None) -> list[dict]:
        """Contrat public 1.2 (E2a) : voir `interface.Mailbox.history_page`.

        Le curseur est l'id, stable quand de nouveaux messages arrivent ; la
        page est rendue du plus ancien au plus récent. Le filtre de périmètre
        (acteur expéditeur ou destinataire, ou conversation avec `peer`) est
        dans la requête, jamais appliqué après la borne.
        """
        limit = max(1, min(int(limit), 200))
        clauses = ["m.id > %s"]
        params: list = [int(after_id or 0)]
        if peer is None:
            clauses.append("(m.sender = %s OR m.recipient = %s)")
            params += [actor, actor]
        else:
            clauses.append(
                "((m.sender = %s AND m.recipient = %s)"
                " OR (m.sender = %s AND m.recipient = %s))")
            params += [actor, peer, peer, actor]
        if since_ts is not None:
            clauses.append("m.created_at >= to_timestamp(%s)")
            params.append(float(since_ts))
        if until_ts is not None:
            clauses.append("m.created_at <= to_timestamp(%s)")
            params.append(float(until_ts))
        if kind is not None:
            clauses.append("m.kind = %s")
            params.append(kind)
        params.append(limit + 1)
        return self.db.query(
            "SELECT " + MAIL_COLUMNS
            + " FROM agent_mailbox m WHERE " + " AND ".join(clauses)
            + " ORDER BY m.id ASC LIMIT %s",
            params)

    def pending_requests(self, recipient, owner, epoch, *, sender=None,
                         limit=50) -> list[dict]:
        """Contrat public 1.3 (E2b) : voir `interface.Mailbox.pending_requests`.

        Le marqueur comptable (`spend_pending`) est joint par destinataire :
        il nomme le tour courant. La consigne de la demande doit porter le
        MÊME `turn_id` que ce marqueur (le tour qui l'a réservée), le bail doit
        être vivant et détenu par `owner`/`epoch` dans la même requête, et la
        remise doit être dans la fenêtre du marqueur. Une demande d'un tour
        antérieur, dont la consigne a survécu à une reprise, n'est donc jamais
        « en attente » pour le tour suivant : elle sera re-livrée par le
        protocole de réservation, pas répondue sous un bail périmé.
        """
        limit = max(1, min(int(limit), 200))
        clauses = [
            "m.recipient = %s",
            "m.kind = 'request'",
            "v.lease_owner = %s",
            "v.lease_epoch = %s",
            "v.lease_expires_at > clock_timestamp()",
            "m.meta->'consigne'->>'runner' = %s",
            "m.meta->'consigne'->>'epoch' = %s",
            "m.meta->'consigne'->>'turn' = p.turn_id",
            "(m.delivered_at IS NULL OR m.delivered_at >= p.created_at)",
            "NOT coalesce((m.meta ? 'retire'), false)",
            "NOT EXISTS (SELECT 1 FROM agent_mailbox r WHERE r.in_reply_to = m.id)",
        ]
        params: list = [recipient, owner, int(epoch), owner, str(int(epoch))]
        if sender is not None:
            clauses.append("m.sender = %s")
            params.append(sender)
        params.append(limit + 1)
        return self.db.query(
            """
            SELECT m.id, m.sender, m.recipient, m.body, m.kind, m.payload,
                   coalesce(btrim(m.work_item_id), '') AS work_item_id,
                   m.in_reply_to,
                   extract(epoch from m.created_at)::float8 AS created_ts,
                   extract(epoch from m.delivered_at)::float8 AS delivered_ts,
                   p.turn_id AS turn_id
              FROM agent_mailbox m
              JOIN spend_pending p ON p.agent = m.recipient
              JOIN agent_registry v ON v.name = m.recipient
             WHERE """ + " AND ".join(clauses)
            + " ORDER BY m.id ASC LIMIT %s",
            params)

    def reply_target(self, request_id) -> dict | None:
        """#7672 : voir `interface.Mailbox.reply_target`.

        La demande est verrouillée `FOR SHARE` avant toute décision : un
        retrait concurrent (qui prend la ligne en écriture) attend la réponse.
        Le marqueur comptable est lu ensuite, lui aussi `FOR SHARE`, dans la
        même transaction : `pending_spend_clear` ou `pending_spend_begin`
        attendent le commit de la réponse, donc le marqueur qui a autorisé le
        tour ne peut pas disparaître ni changer sous la réponse déjà décidée
        (constat B de #7672). Absent, il n'y a rien à verrouiller : la réponse
        est refusée, jamais déposée sans tour courant. La consigne, elle,
        n'est pas réécrite après la remise (le protocole de réservation la
        fige), donc la lire sans verrou ne peut pas changer sous les pieds de
        l'appelant.
        """
        rows = self.db.query(
            """
            SELECT m.id, m.sender, m.recipient, m.kind, m.body, m.payload,
                   coalesce(btrim(m.work_item_id), '') AS work_item_id,
                   m.in_reply_to,
                   extract(epoch from m.created_at)::float8 AS created_ts,
                   extract(epoch from m.delivered_at)::float8 AS delivered_ts,
                   m.meta->'consigne'->>'runner' AS consigne_runner,
                   m.meta->'consigne'->>'epoch' AS consigne_epoch,
                   m.meta->'consigne'->>'turn' AS consigne_turn,
                   coalesce((m.meta ? 'retire'), false) AS withdrawn
              FROM agent_mailbox m
             WHERE m.id = %s
             FOR SHARE OF m
            """,
            (int(request_id),))
        if not rows:
            return None
        request = dict(rows[0])
        # Verrou pris jusqu'au commit : le marqueur qui autorise la réponse ne
        # peut plus être effacé ni remplacé avant que celle-ci soit écrite.
        marker = self.db.query(
            """
            SELECT agent, turn_id,
                   extract(epoch from created_at)::float8 AS marker_created_ts
              FROM spend_pending
             WHERE agent = %s
             FOR SHARE
            """,
            (request["recipient"],))
        request["marker_present"] = bool(marker)
        request["marker_turn_id"] = marker[0]["turn_id"] if marker else None
        request["marker_created_ts"] = (marker[0]["marker_created_ts"]
                                        if marker else None)
        replies = self.db.query(
            """
            SELECT id, sender, recipient, body, kind, payload, in_reply_to,
                   coalesce(btrim(work_item_id), '') AS work_item_id,
                   extract(epoch from created_at)::float8 AS created_ts,
                   extract(epoch from delivered_at)::float8 AS delivered_ts
              FROM agent_mailbox
             WHERE in_reply_to = %s
             FOR SHARE
            """,
            (int(request_id),))
        return {"request": request, "replies": replies}

    def staging_emission(self, recipient, sender, key) -> int | None:
        rows = self.db.query(
            """
            SELECT id FROM agent_mailbox
            WHERE recipient = %s AND sender = %s
              AND payload -> 'staging_actif' ->> 'emission' = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (recipient, sender, key),
        )
        return int(rows[0]["id"]) if rows else None

    def review_requests(self, recipient, lot, *, for_update=False) -> list[dict]:
        """#2504 : les `request` du lot, filtrés avant toute borne.

        `history` lit les N derniers messages PUIS l'appelant filtre : sous un
        fort volume de courrier, une demande de relecture du même lot sortait de
        la fenêtre et la désignation en renvoyait une seconde. Ici le
        destinataire, la nature et le lot sont dans le WHERE ; le lot reste
        petit, aucune borne n'est nécessaire (une borne rouvrirait le défaut).

        #2563 : le lot est comparé à `work_item_id` sans fonction, pour rester
        sur l'index partiel 0413. Tous les écrivains de `request` posent
        `str(int(lot))` : l'égalité simple est exacte."""
        if for_update:
            # #7891: same lock order as reserve/deliver and mail.send.
            self.lock_recipient(recipient)
        return self.db.query(
            """
            SELECT %s
            FROM agent_mailbox
            WHERE recipient = %%s
              AND kind = 'request'
              AND work_item_id = %%s
            ORDER BY id %s%s
            """ % (MAIL_COLUMNS, "ASC" if for_update else "DESC",
                   " FOR UPDATE" if for_update else ""),
            (recipient, str(lot)),
        )

    def delivered_for_lot(self, recipient, lot, limit) -> list[dict]:
        """#2539 : les derniers messages déjà remis de CE lot, filtrés dans la
        requête (destinataire + lot), sans fenêtre de temps — un verdict ancien
        reste lisible dans le brief de son lot.

        #2752 : comme #2563, le lot est comparé à `work_item_id` sans
        fonction, pour rester sur l'index partiel 0413. Tous les écrivains
        posent `str(int(lot))` : l'égalité simple est exacte."""
        return self.db.query(
            """
            SELECT %s
            FROM agent_mailbox
            WHERE recipient = %%s
              AND work_item_id = %%s
              AND delivered_at IS NOT NULL
            ORDER BY id DESC
            LIMIT %%s
            """ % MAIL_COLUMNS,
            (recipient, str(lot or "").strip(), int(limit)),
        )

    def pending_recipients(self) -> list[str]:
        # #6424 : une boîte `externe` n'est soldée par aucun runner ; son
        # courrier reste en attente, hors des lectures de service.
        return [row["recipient"] for row in self.db.query(
            "SELECT DISTINCT m.recipient FROM agent_mailbox m"
            " WHERE m.delivered_at IS NULL AND " + NOT_EXTERNAL_SQL.format(t="m")
        )]

    def pending_recipients_sorted(self) -> list[str]:
        # #6424 : PAS un décompte. Seul lecteur : `ameesh export-v0 --all`,
        # le retour arrière du courrier vers la v0, qui réécrit chaque boîte
        # puis la marque remise (`mesh_cli.cmd_export_v0`).
        # Exclure les boîtes `externe` laisserait leur courrier en base au
        # lieu de le rendre à la v0 : toutes les boîtes sont listées.
        return [row["recipient"] for row in self.db.query(
            "SELECT DISTINCT recipient FROM agent_mailbox "
            "WHERE delivered_at IS NULL ORDER BY recipient")]

    def unread_total(self) -> int:
        return self.db.query(
            "SELECT count(*)::int AS n FROM agent_mailbox m"
            " WHERE m.delivered_at IS NULL AND " + NOT_EXTERNAL_SQL.format(t="m")
        )[0]["n"]

    # -- demandes d'humains sans lot (L130) -----------------------------------
    def human_messages(self, *, since_ts, humans, limit) -> list[dict]:
        """Les messages d'humains depuis `since_ts` (L130).

        #6422 : le filtre `created_at` sans destinataire lisait tout
        `agent_mailbox_recent_idx` (732 ms). Les candidats viennent des index
        partiels de 0558 (humains, chat externe) et de `sender_idx` pour les
        noms déclarés ; la jointure finale part de la clé primaire.
        """
        rows = self.db.query(
            *human_messages_query(since_ts=since_ts, humans=humans, limit=limit))
        return list(reversed(rows))

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
        if old.startswith("human:"):
            raise interface.MailboxRefused(
                "%s is a human mailbox: use ameesh decisions, not forward" % old)
        with self.db.transaction() as tx:
            # L976 (R2 delta codex3, B1 résiduel) : le verrou par nom est pris
            # AVANT toute lecture du registre, et les chemins de création
            # prennent le même (`registry.NAME_LOCK_CTE`). L'absence d'une
            # ligne — qu'un `FOR UPDATE` ne verrouille pas — ne peut donc plus
            # se glisser entre la lecture et la copie : une inscription
            # concurrente attend notre commit, et si elle l'a gagné, la lecture
            # ci-dessous la voit (instantané de l'instruction suivante).
            # Deux renvois croisés prennent les verrous par ordre de clé.
            for key in sorted({name_lock_key(old), name_lock_key(new)}):
                tx.query(NAME_LOCK_SQL, (key,))
            # Puis la décision sous le verrou des deux lignes du registre
            # (`FOR UPDATE`, par ordre de nom : deux renvois croisés ne
            # s'interbloquent pas) et le statut est relu après l'attente
            # éventuelle. Une relance de `old` ou un arrêt de `new` committé
            # entre la lecture et la copie bloquait jusqu'ici sans être revu :
            # il bloque désormais sur ce verrou jusqu'à notre commit, donc
            # jamais entre la décision et la copie.
            rows = tx.query(
                "SELECT name, status, retired_at IS NOT NULL AS retired"
                " FROM agent_registry"
                " WHERE name IN (%s, %s) ORDER BY name FOR UPDATE", (old, new))
            locked = {row["name"]: row["status"] for row in rows}
            retired = {row["name"] for row in rows if row["retired"]}
            if old == new:
                raise interface.MailboxRefused(
                    "old and new recipient are the same (%s)" % old)
            if old in locked and locked[old] != "stopped":
                raise interface.MailboxRefused(
                    "%s is not stopped (status %s): its mailbox is read, forward refused"
                    % (old, locked[old]))
            if new not in locked:
                raise interface.MailboxRefused(
                    "%s is not in the registry: forward to a live agent" % new)
            if locked[new] == "stopped":
                raise interface.MailboxRefused(
                    "%s is stopped: forward to a live agent" % new)
            if new in retired:
                # #3460 : un retrait committé pendant l'attente du verrou gagne
                raise interface.MailboxRefused(
                    "%s is retired: forward to a live agent" % new)
            ids = sorted(int(row["id"]) for row in tx.query(
                "SELECT m.id FROM agent_mailbox m"
                " WHERE m.recipient = %%s AND m.delivered_at IS NULL"
                "   AND NOT (m.kind = 'request' AND (m.payload -> 'decision') IS NOT NULL)"
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
                   SET delivered_at = clock_timestamp(), status = 'delivered',
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

    def expire_dead_letters(self, recipient, note, before_ts, dry_run=False) -> list[dict]:
        # L976 : expire le courrier en souffrance d'une boîte morte. La
        # décision (boîte morte) est prise sous le verrou par nom
        # (`registry.NAME_LOCK_*`, partagé avec les chemins de création), puis
        # sous le verrou de la ligne du registre quand elle existe (`FOR
        # SHARE`) : une relance concurrente comme une inscription d'un nom
        # absent bloquent jusqu'à notre commit, et le statut est relu après
        # l'attente — le courrier d'un vivant n'est jamais expiré (relectures
        # codex3, B1 puis B1 résiduel). Mêmes exclusions que `dead_letters`
        # (L124 : une demande de décision attend un humain) et que `forward`
        # (réservation active : le message est peut-être montré). `before_ts` :
        # seuls les messages reçus avant ce seuil (la rétention). `dry_run` :
        # lecture seule, sans verrou.
        v = ("WITH v AS (SELECT name, status FROM agent_registry"
             " WHERE name = %%s FOR SHARE) ")
        where = """
             WHERE m.recipient = %%s
               AND m.delivered_at IS NULL
               AND m.created_at <= to_timestamp(%%s)
               AND NOT (m.kind = 'request' AND (m.payload -> 'decision') IS NOT NULL)
               AND NOT (%s)
               AND (NOT EXISTS (SELECT 1 FROM v) OR (SELECT status FROM v) = 'stopped')
            """ % _RESERVED_SQL
        columns = ("m.id, m.sender, m.kind,"
                   " extract(epoch from m.created_at)::float8 AS created_ts")
        if dry_run:
            rows = self.db.query((v + "SELECT %s FROM agent_mailbox AS m%s")
                                 % (columns, where),
                                 (recipient, recipient, float(before_ts)))
        else:
            # L976 (relecture codex3, B1 résiduel) : le verrou par nom est pris
            # dans une instruction À PART, avant la décision : une inscription
            # concurrente prend le même verrou (chemins de création), donc soit
            # elle a gagné et la lecture de l'instruction suivante la voit,
            # soit elle attend notre commit — l'absence ne se glisse plus entre
            # la lecture et l'écriture (une seconde lecture sans verrou ne
            # suffit pas : l'instantané d'une même instruction ne se rafraîchit
            # pas après l'attente sur le verrou).
            with self.db.transaction() as tx:
                tx.query(NAME_LOCK_SQL, (name_lock_key(recipient),))
                rows = tx.query(
                    (v + """
                    UPDATE agent_mailbox AS m
                       SET delivered_at = now(), status = 'delivered',
                           meta = m.meta || jsonb_build_object('expired', %%s::jsonb)
                    %s
                     RETURNING %s
                    """) % (where, columns),
                    (recipient, json.dumps(note or {}, ensure_ascii=False), recipient,
                     float(before_ts)))
        return sorted(rows, key=lambda row: int(row["id"]))
