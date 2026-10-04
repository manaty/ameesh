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

class Keys(interface.Keys):

    def info(self, agent) -> dict | None:
        rows = self.db.query(
            """
            SELECT name, public_key, public_key_fingerprint, key_role,
                   extract(epoch from key_updated_at)::float8 as key_updated_ts,
                   extract(epoch from key_revoked_at)::float8 as key_revoked_ts
              FROM agent_registry WHERE name = %s
            """,
            (agent,),
        )
        return rows[0] if rows else None

    def register(self, agent, public_key, fingerprint, role, note) -> bool:
        rows = self.db.query(
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

    def revoke(self, agent) -> bool:
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

    def registered(self) -> list[dict]:
        return self.db.query(
            """
            SELECT name, public_key_fingerprint, key_role,
                   extract(epoch from key_updated_at)::float8 as key_updated_ts,
                   extract(epoch from key_revoked_at)::float8 as key_revoked_ts
              FROM agent_registry
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
