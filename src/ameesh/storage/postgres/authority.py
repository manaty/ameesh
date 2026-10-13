# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : clés des agents, approbations Ed25519, nonces consommés.

SQL déplacé tel quel depuis `ameesh.authority` (lot L1) et, pour la liste
des clés (`mesh key list`), depuis `ameesh.mesh_cli` (phase 2). La consommation
d'une approbation est décidée par l'`INSERT … ON CONFLICT DO NOTHING` sur la
clé primaire (approver, nonce) de `mesh_consumed_nonces` : la preuve survit
à la suppression de l'approbation.
"""
from __future__ import annotations

import json

from .. import interface

#: the registry row made by SQL on 2026-10-12 for the owner's key (#330)
STOPGAP_NAME = "smichea"

class Keys(interface.Keys):

    def info(self, agent) -> dict | None:
        rows = self.db.query(
            """
            SELECT name, holder, public_key, public_key_fingerprint, key_role,
                   extract(epoch from key_updated_at)::float8 as key_updated_ts,
                   extract(epoch from key_revoked_at)::float8 as key_revoked_ts
              FROM mesh_keys WHERE name = %s
            """,
            (agent,),
        )
        return rows[0] if rows else None

    def register(self, agent, public_key, fingerprint, role, note) -> bool:
        with self.db.transaction() as tx:
            if role == "owner":
                # the owner's authority on an agent: the owner's own gesture,
                # refused by the guard of 0364 outside the owner's role (#326 r2)
                tx.execute("SET LOCAL ameesh.owner_gesture = 'on'")
            rows = tx.query(
                """
                UPDATE agent_registry
                   SET public_key = %s,
                       public_key_fingerprint = %s,
                       key_role = %s,
                       key_updated_at = now(),
                       key_revoked_at = NULL,
                       status_text = coalesce(%s, status_text),
                       updated_at = now()
                 WHERE name = %s
                RETURNING name
                """,
                (public_key, fingerprint, role, note, agent),
            )
        return bool(rows)

    def register_human(self, name, human, public_key, fingerprint, role, *,
                       replace=False) -> str:
        with self.db.transaction() as tx:
            # the stopgap row of 2026-10-12 (0364), exactly: it only ever
            # stood for the owner's key
            tx.execute(
                """
                DELETE FROM agent_registry
                 WHERE name = %s AND name = %s AND responsible = %s
                   AND harness = 'none' AND mode = 'externe' AND turns = 0
                   AND session_id IS NULL AND lease_owner IS NULL AND canon_ref IS NULL
                """,
                (name, STOPGAP_NAME, "human:" + STOPGAP_NAME),
            )
            if tx.query("SELECT 1 FROM agent_registry WHERE name = %s", (name,)):
                return "agent"
            held = bool(tx.query("SELECT 1 FROM key_holders WHERE name = %s", (name,)))
            if not replace:
                if held:
                    return "exists"
                tx.execute(
                    """
                    INSERT INTO key_holders (name, human, public_key, public_key_fingerprint,
                                             key_role, key_updated_at)
                    VALUES (%s, %s, %s, %s, %s, now())
                    """,
                    (name, human, public_key, fingerprint, role),
                )
                return "created"
            # the owner's own gesture: the guard of 0364 checks the role
            tx.execute("SET LOCAL ameesh.owner_gesture = 'on'")
            tx.execute(
                """
                INSERT INTO key_holders (name, human, public_key, public_key_fingerprint,
                                         key_role, key_updated_at, key_revoked_at)
                VALUES (%s, %s, %s, %s, %s, now(), NULL)
                ON CONFLICT (name) DO UPDATE
                   SET human = excluded.human,
                       public_key = excluded.public_key,
                       public_key_fingerprint = excluded.public_key_fingerprint,
                       key_role = excluded.key_role,
                       key_updated_at = now(),
                       key_revoked_at = NULL
                """,
                (name, human, public_key, fingerprint, role),
            )
            return "replaced" if held else "created"

    def revoke(self, agent) -> bool:
        held = self.db.query(
            """
            UPDATE key_holders SET key_revoked_at = now()
             WHERE name = %s AND public_key IS NOT NULL AND key_revoked_at IS NULL
            RETURNING name
            """,
            (agent,),
        )
        if held:
            return True
        rows = self.db.query(
            """
            UPDATE agent_registry
               SET key_revoked_at = now(), updated_at = now()
             WHERE name = %s AND public_key IS NOT NULL AND key_revoked_at IS NULL
            RETURNING name
            """,
            (agent,),
        )
        return bool(rows)

    def owner_registrations(self, since) -> list[dict]:
        return self.db.query(
            """
            SELECT e.id, e.name, e.holder, e.event, e.fingerprint, e.db_user,
                   e.owner_gesture, extract(epoch from e.at)::float8 as at_ts
              FROM key_events e
             WHERE e.key_role = 'owner' AND e.event IN ('registered', 'replaced')
               AND CASE WHEN e.owner_gesture THEN e.at >= to_timestamp(%s)
                        -- #326 r4: a confirmation covers the key it names,
                        -- registered before it by the server's clock (`at`,
                        -- set by the trigger) — never by the id, a sequence's
                        -- value; a registration still uncommitted when the
                        -- owner confirmed has no confirmation of its own
                        ELSE NOT EXISTS (SELECT 1 FROM key_events c
                                          WHERE c.name = e.name AND c.event = 'confirmed'
                                            AND c.fingerprint = e.fingerprint
                                            AND c.at > e.at)
                   END
             ORDER BY e.at, e.id
            """,
            (since,),
        )

    def confirm(self, name) -> int:
        with self.db.transaction() as tx:
            tx.execute("SET LOCAL ameesh.owner_gesture = 'on'")
            # one confirmation per key the owner can see now: the current one,
            # and every owner registration of that name already committed
            rows = tx.query(
                """
                INSERT INTO key_events (name, holder, event, key_role, fingerprint)
                SELECT DISTINCT ON (fingerprint) name, holder, 'confirmed', key_role,
                       fingerprint
                  FROM (SELECT name, holder, key_role,
                               public_key_fingerprint AS fingerprint, 0 AS rank
                          FROM mesh_keys WHERE name = %s AND public_key IS NOT NULL
                        UNION ALL
                        SELECT name, holder, key_role, fingerprint, 1
                          FROM key_events
                         WHERE name = %s AND key_role = 'owner'
                           AND event IN ('registered', 'replaced')
                           AND fingerprint IS NOT NULL) seen
                 ORDER BY fingerprint, rank
                RETURNING id
                """,
                (name, name),
            )
        return max((int(r["id"]) for r in rows), default=0)

    def registered(self) -> list[dict]:
        return self.db.query(
            """
            SELECT name, holder, public_key_fingerprint, key_role,
                   extract(epoch from key_updated_at)::float8 as key_updated_ts,
                   extract(epoch from key_revoked_at)::float8 as key_revoked_ts
              FROM mesh_keys
             WHERE public_key IS NOT NULL
             ORDER BY name
            """
        )


class Approvals(interface.Approvals):

    def create(self, *, approver, action, artifact_kind, artifact_hash, decision, nonce,
               signed_payload, signature, signature_key, created_us, expires_us,
               meta) -> int:
        rows = self.db.query(
            """
            INSERT INTO mesh_approvals
                (approver, action, artifact_kind, artifact_hash, decision, nonce,
                 signed_payload, signature, signature_key, created_at, expires_at, meta)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                    to_timestamp(%s::bigint / 1000000.0),
                    to_timestamp(%s::bigint / 1000000.0), %s::jsonb)
            RETURNING id
            """,
            (approver, action, artifact_kind, artifact_hash, decision, nonce,
             signed_payload, signature, signature_key, created_us, expires_us,
             json.dumps(meta or {}, ensure_ascii=False)),
        )
        return int(rows[0]["id"])

    def recent(self, *, action, artifact_hash, limit) -> list[dict]:
        sql = """
            SELECT id, approver, action, artifact_kind, artifact_hash, decision, nonce,
                   signed_payload, signature, signature_key,
                   extract(epoch from created_at)::float8 as created_ts,
                   extract(epoch from expires_at)::float8 as expires_ts,
                   extract(epoch from consumed_at)::float8 as consumed_ts,
                   consumed_by, unconsumed, unexpired, key_known, key_live, key_matches,
                   key_is_owner
              FROM mesh_approvals_status
             WHERE true
        """
        params: list = []
        if action:
            sql += " AND action = %s"
            params.append(action)
        if artifact_hash:
            sql += " AND artifact_hash = %s"
            params.append(artifact_hash.lower())
        sql += " ORDER BY id DESC LIMIT %s"
        params.append(int(limit))
        return self.db.query(sql, tuple(params))

    def candidates(self, action, artifact_hash, decision) -> list[dict]:
        return self.db.query(
            """
            SELECT id, approver, action, artifact_kind, artifact_hash, decision, nonce,
                   signed_payload, signature, signature_key,
                   extract(epoch from created_at)::float8 as created_ts,
                   extract(epoch from expires_at)::float8 as expires_ts,
                   extract(epoch from consumed_at)::float8 as consumed_ts,
                   consumed_by, unconsumed, unexpired, key_known, key_live, key_matches,
                   key_is_owner
              FROM mesh_approvals_status
             WHERE action = %s AND artifact_hash = %s AND decision = %s
             ORDER BY id DESC LIMIT 20
            """,
            (action, artifact_hash.lower(), decision),
        )

    def consume(self, approval_id, by) -> bool:
        """Consomme une approbation une seule fois, par (approver, nonce).

        L'INSERT ... ON CONFLICT est la décision atomique : s'il n'insère
        rien, le nonce était déjà consommé. La mise à jour de la ligne n'est
        qu'un affichage (la vérité est dans mesh_consumed_nonces, qui survit à
        la suppression de l'approbation). Deux instructions, comme avant :
        celles de la connexion (ou de la transaction) passée.
        """
        rows = self.db.query(
            """
            INSERT INTO mesh_consumed_nonces (approver, nonce, approval_id, consumed_by)
            SELECT approver, nonce, id, %s FROM mesh_approvals WHERE id = %s
            ON CONFLICT (approver, nonce) DO NOTHING
            RETURNING approver, nonce
            """,
            (by, int(approval_id)),
        )
        if not rows:
            return False
        self.db.execute(
            "UPDATE mesh_approvals SET consumed_at = now(), consumed_by = %s WHERE id = %s",
            (by, int(approval_id)),
        )
        return True


class Nonces(interface.Nonces):

    def state(self, approver, nonce) -> dict | None:
        rows = self.db.query(
            """
            SELECT approver, consumed_by,
                   extract(epoch from consumed_at)::float8 as consumed_ts
              FROM mesh_consumed_nonces
             WHERE approver = %s AND nonce = %s
            """,
            (approver, nonce),
        )
        return rows[0] if rows else None

    def consume(self, approver, nonce, *, by, challenge, authenticator_id, exp, iat,
                clock_skew) -> bool:
        """Consomme (approver, nonce) une seule fois (`ameesh_receipt_consume`,
        UNE transaction) : authentificateur lu sous verrou partagé (`FOR
        SHARE`) s'il est donné ; APRÈS ce dernier verrou et juste avant
        l'écriture, échéances signées recontrôlées à l'heure réelle de la
        base ; décision par `INSERT … ON CONFLICT DO NOTHING` sur la clé
        primaire, échéances recontrôlées après lui. Échéance dépassée : erreur
        SQL `ameesh_echeance [expired|iat_future]` (DbError), rien n'est
        consommé."""
        rows = self.db.query(
            "SELECT ameesh_receipt_consume(%s::text, %s::text, %s::text, %s::text, "
            "%s::bigint, %s::bigint, %s::bigint, %s::integer) AS consumed",
            (approver, nonce, by, challenge or None, authenticator_id, exp, iat,
             int(clock_skew)),
        )
        return bool(rows and rows[0].get("consumed"))
