# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : registre des authentificateurs (§8.2), son verrou, le
journal des synchronisations (`authenticator_syncs`, 0023).

SQL déplacé tel quel depuis `ameesh.receipts` (verrou, lectures et
écritures du registre) et `ameesh.canon_sync` (journal, retard) — lot L1.
Les écritures du registre ne passent que par la transaction qui détient le
verrou (`under_registry_lock`) : `receipts._apply_authenticators` les fait
par `jeton.db`, après avoir revérifié le verrou dans `pg_locks`.

Verrou consultatif TRANSACTIONNEL de toute écriture du registre (revue L9b :
B1, codex3). Le registre est unique par schéma : la clé ne dépend que de lui
— ni du chemin du clone, ni de l'hôte, ni de `canon.root` —, deux clones ou
deux hôtes du même canon se sérialisent donc sur le même verrou. Une seule
définition de la clé (`_AUTH_LOCK_KEY`), pour la prise (`lock_registry`)
comme pour le contrôle (`registry_lock_held`).
"""
from __future__ import annotations

from .. import interface

AUTH_COLUMNS = """
    id, approver, facade, credential_id, public_key, key_fingerprint, aaguid, level,
    canon_ref,
    extract(epoch from enrolled_at)::float8 AS enrolled_ts,
    extract(epoch from updated_at)::float8  AS updated_ts,
    extract(epoch from revoked_at)::float8  AS revoked_ts,
    revoked_reason
"""

AUTH_LOCK_NAMESPACE = "ameesh_authenticators:"
_AUTH_LOCK_KEY = "hashtext(%s || current_schema())::bigint"
AUTH_LOCK_SQL = "SELECT pg_advisory_xact_lock(%s)" % _AUTH_LOCK_KEY
#: pg_locks présente une clé bigint en (classid = 32 bits hauts, objid = 32
#: bits bas, objsubid = 1) ; seule la session courante compte (pid)
_AUTH_LOCK_HELD_SQL = """
SELECT EXISTS (
    SELECT 1
      FROM pg_locks l, (SELECT %s AS k) AS key
     WHERE l.locktype = 'advisory' AND l.granted AND l.pid = pg_backend_pid()
       AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
       AND l.objsubid = 1
       AND l.classid::bigint = ((key.k >> 32) & 4294967295)
       AND l.objid::bigint = (key.k & 4294967295)
) AS held
""" % _AUTH_LOCK_KEY


class Authenticators(interface.Authenticators):

    # -- verrou du registre --------------------------------------------------
    def lock_registry(self) -> interface.RegistryLock:
        """À appeler dans une transaction ouverte : le verrou tient jusqu'au
        COMMIT. Hors transaction (autocommit), il serait relâché aussitôt et
        l'écriture refuserait le jeton. Réentrant dans la même session
        (`pg_advisory_xact_lock` déjà détenu : accordé)."""
        self.db.execute(AUTH_LOCK_SQL, (AUTH_LOCK_NAMESPACE,))
        return interface.RegistryLock(self.db, interface._LOCK_PROOF)

    def registry_lock_held(self) -> bool:
        rows = self.db.query(_AUTH_LOCK_HELD_SQL, (AUTH_LOCK_NAMESPACE,))
        return bool(rows and rows[0].get("held"))

    def under_registry_lock(self, work):
        """UNE transaction (`db.transaction()`, les deux pilotes) : le verrou
        du registre est la première instruction, AVANT tout contrôle ; `work`
        reçoit le jeton (`jeton.db` : la transaction) ; COMMIT à la sortie
        normale, ROLLBACK sur toute exception (qui est propagée)."""
        with self.db.transaction() as tx:
            lock = Authenticators(tx).lock_registry()
            return work(lock)

    # -- journal des synchronisations ------------------------------------------
    def canon_refs(self, revocations) -> list[dict]:
        return self.db.query(
            "SELECT DISTINCT canon_ref FROM authenticators "
            "WHERE revoked_at IS NULL OR revoked_reason IN (%s, %s)",
            revocations)

    def last_sync(self) -> dict | None:
        rows = self.db.query(
            "SELECT id, root_member, root_commit, commits, branch, trust, host, applied_by, "
            "extract(epoch from applied_at)::float8 AS applied_ts "
            "FROM authenticator_syncs ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def journal_head(self) -> int | None:
        rows = self.db.query("SELECT max(id) AS id FROM authenticator_syncs")
        if not rows or rows[0].get("id") is None:
            return None
        return int(rows[0]["id"])

    def append_sync(self, *, root_member, root_commit, commits_json, branch, trust, host,
                    summary_json, expected) -> bool:
        """Insertion conditionnée au journal lu sous le verrou : la ligne
        n'est écrite que si la dernière est encore `expected`."""
        rows = self.db.query(
            """
            INSERT INTO authenticator_syncs
                (root_member, root_commit, commits, branch, trust, host, summary)
            SELECT %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb
             WHERE (SELECT max(id) FROM authenticator_syncs) IS NOT DISTINCT FROM %s::bigint
            RETURNING id
            """,
            (root_member, root_commit, commits_json, branch, trust, host, summary_json,
             expected))
        return bool(rows)

    # -- registre de confiance (§8.2) ----------------------------------------
    def registered(self, *, approver, include_revoked) -> list[dict]:
        sql = "SELECT %s FROM authenticators WHERE true" % AUTH_COLUMNS
        params: list = []
        if approver:
            sql += " AND approver = %s"
            params.append(approver)
        if not include_revoked:
            sql += " AND revoked_at IS NULL"
        sql += " ORDER BY approver, facade, credential_id, id"
        return self.db.query(sql, tuple(params))

    def for_approver(self, approver) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM authenticators WHERE approver = %%s ORDER BY id" % AUTH_COLUMNS,
            (approver,))

    def active_holders(self, facade, credential_id) -> list[dict]:
        return self.db.query(
            "SELECT approver FROM authenticators WHERE facade = %s AND credential_id = %s "
            "AND revoked_at IS NULL", (facade, credential_id))

    def update_meta(self, authenticator_id, *, level, aaguid, canon_ref) -> bool:
        rows = self.db.query(
            "UPDATE authenticators SET level = %s, aaguid = %s, canon_ref = %s, "
            "updated_at = now() WHERE id = %s AND revoked_at IS NULL RETURNING id",
            (level, aaguid, canon_ref, int(authenticator_id)))
        return bool(rows)

    def revoke(self, authenticator_id, *, reason, canon_ref) -> bool:
        """Une révocation est un UPDATE de la ligne : elle se sérialise avec
        les lectures sous verrou partagé (`FOR SHARE`) des réservations, des
        enregistrements de grants et des consommations de nonces."""
        rows = self.db.query(
            "UPDATE authenticators SET revoked_at = now(), revoked_reason = %s, "
            "canon_ref = coalesce(%s::text, canon_ref), updated_at = now() "
            "WHERE id = %s AND revoked_at IS NULL RETURNING id",
            (reason, canon_ref, int(authenticator_id)))
        return bool(rows)

    def insert(self, record) -> bool:
        rows = self.db.query(
            """
            INSERT INTO authenticators
                (approver, facade, credential_id, public_key, key_fingerprint, aaguid,
                 level, canon_ref)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (facade, credential_id) WHERE revoked_at IS NULL DO NOTHING
            RETURNING id
            """,
            (record["approver"], record["facade"], record["credential_id"],
             record["public_key"], record["key_fingerprint"], record["aaguid"],
             record["level"], record["canon_ref"]))
        return bool(rows)
