# SPDX-License-Identifier: AGPL-3.0-only
"""#7106 (avatar B3) : hôte partagé multi-tenant (migration 0575).

`host_tenancy` porte ce que dit le canon, `host_tenants` la liaison écrite
par le superviseur d'hôte, `runner_presence` le battement du runner d'un
tenant. La garde de réclamation est `ameesh_tenant_claim_ok` (SQL), appelée
par `Leases.claim` et `Leases.attach_claim`.
"""
from __future__ import annotations

import json

from .. import interface


class Tenancy(interface.Tenancy):
    def write_policy(self, host, canon, *, shared, tenants, canon_ref):
        tenants = [str(t) for t in tenants or () if t]
        if not shared and not tenants:
            self.db.execute("DELETE FROM host_tenancy WHERE host = %s AND canon = %s",
                            (host, canon or ""))
            return
        self.db.execute(
            """
            INSERT INTO host_tenancy (host, canon, shared, tenants, canon_ref, synced_at)
            VALUES (%s, %s, %s, ARRAY(SELECT jsonb_array_elements_text(%s::jsonb)), %s, now())
            ON CONFLICT (host, canon) DO UPDATE
               SET shared = excluded.shared, tenants = excluded.tenants,
                   canon_ref = excluded.canon_ref, synced_at = excluded.synced_at
            """,
            (host, canon or "", bool(shared), json.dumps(tenants), canon_ref))

    def policy(self, host):
        rows = self.db.query(
            "SELECT shared, tenants FROM host_tenancy WHERE host = %s ORDER BY canon",
            (host,))
        tenants: list[str] = []
        for row in rows:
            if row["shared"]:
                tenants.extend(t for t in row["tenants"] or () if t not in tenants)
        # comme `ameesh_host_shared` : un tenant encore lié garde l'hôte
        # partagé quand le canon ne le déclare plus (fail closed)
        shared = any(row["shared"] for row in rows) or bool(self.db.query(
            "SELECT 1 FROM host_tenants WHERE host = %s LIMIT 1", (host,)))
        return {"shared": shared, "tenants": tenants}

    def bind(self, host, tenant, *, unix_user, uid, runner_id, actor):
        rows = self.db.query(
            """
            INSERT INTO host_tenants (host, tenant, unix_user, uid, runner_id,
                                      registered_by, registered_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (host, tenant) DO UPDATE
               SET unix_user = excluded.unix_user, uid = excluded.uid,
                   runner_id = excluded.runner_id,
                   registered_by = excluded.registered_by,
                   registered_at = excluded.registered_at
            RETURNING host, tenant, unix_user, uid, runner_id, registered_by,
                      extract(epoch from registered_at)::float8 AS registered_ts
            """,
            (host, tenant, unix_user, int(uid), runner_id, actor))
        return dict(rows[0])

    def unbind(self, host, tenant):
        # Un seul énoncé : la liste des agents qui retiennent le tenant et la
        # suppression voient le même instantané ; une ligne verrouillée par
        # une réclamation concurrente est attendue (FOR UPDATE).
        with self.db.transaction() as tx:
            blocking = tx.query(
                """
                SELECT name FROM agent_registry
                 WHERE responsible = %s AND host = %s AND retired_at IS NULL
                 ORDER BY name FOR UPDATE
                """,
                (tenant, host))
            names = [row["name"] for row in blocking]
            if names:
                return {"removed": False, "blocking": names}
            rows = tx.query(
                "DELETE FROM host_tenants WHERE host = %s AND tenant = %s RETURNING tenant",
                (host, tenant))
        return {"removed": bool(rows), "blocking": []}

    def bindings(self, host=None):
        rows = self.db.query(
            """
            SELECT t.host, t.tenant, t.unix_user, t.uid, t.runner_id, t.registered_by,
                   extract(epoch from t.registered_at)::float8 AS registered_ts,
                   p.version, extract(epoch from p.seen_at)::float8 AS seen_ts,
                   extract(epoch from clock_timestamp())::float8 AS now_ts
              FROM host_tenants t
              LEFT JOIN runner_presence p ON p.host = t.host AND p.tenant = t.tenant
             WHERE %s::text IS NULL OR t.host = %s
             ORDER BY t.host, t.tenant
            """,
            (host, host))
        return [dict(row) for row in rows]

    def beat(self, host, tenant, runner_id, *, uid, version):
        # La présence n'existe que sous une liaison qui nomme CE runner : un
        # runner mal configuré ne se déclare pas présent pour un autre tenant.
        rows = self.db.query(
            """
            INSERT INTO runner_presence (host, tenant, runner_id, uid, version, seen_at)
            SELECT t.host, t.tenant, t.runner_id, %s, %s, clock_timestamp()
              FROM host_tenants t
             WHERE t.host = %s AND t.tenant = %s AND t.runner_id = %s
            ON CONFLICT (host, tenant) DO UPDATE
               SET runner_id = excluded.runner_id, uid = excluded.uid,
                   version = excluded.version, seen_at = excluded.seen_at
            RETURNING tenant
            """,
            (uid, version, host, tenant, runner_id))
        return bool(rows)

    def presence(self, host, tenant):
        rows = self.db.query(
            """
            SELECT p.runner_id, p.version,
                   extract(epoch from p.seen_at)::float8 AS seen_ts,
                   extract(epoch from clock_timestamp())::float8 AS now_ts
              FROM runner_presence p
              JOIN host_tenants t ON t.host = p.host AND t.tenant = p.tenant
                                 AND t.runner_id = p.runner_id
             WHERE p.host = %s AND p.tenant = %s
            """,
            (host, tenant))
        return dict(rows[0]) if rows else None
