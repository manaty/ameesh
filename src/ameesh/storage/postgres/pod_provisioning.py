# SPDX-License-Identifier: AGPL-3.0-only
"""#7697 (P4 de #7633, design 7633-hote-avatars-pod.md §8) : demandes et
templates d'enfants pod.

`pod_templates` est le template figé et le battement du pourvoyeur ;
`pod_requests` la demande écrite par le `spawn` du parent. Le pourvoyeur
réclame une demande (`claim`), crée le pod, puis `running` écrit dans UNE
transaction `pod_hosts`, `host_tenancy`, `host_tenants` et l'état `running`.
`cleanup` retire les liaisons après que l'appelant a supprimé le pod.

Aucun secret : le nom du Secret enfant, des noms d'outils et des digests.
"""
from __future__ import annotations

import json

from .. import interface

#: colonnes rendues par les lectures (dates en epoch, comme le reste du stockage).
def _columns(prefix: str = "") -> str:
    p = prefix
    return f"""
    {p}id::text AS id, {p}child, {p}parent, {p}tenant, {p}host,
    {p}template, {p}namespace, {p}pod_name, {p}harness, {p}credential_secret,
    {p}uid, {p}unix_user, {p}runner_id, {p}profile, {p}fingerprint,
    {p}state, {p}requested_by, {p}claimed_by,
    extract(epoch from {p}requested_at)::float8 AS requested_ts,
    extract(epoch from {p}claimed_at)::float8 AS claimed_ts,
    extract(epoch from {p}started_at)::float8 AS started_ts,
    extract(epoch from {p}expires_at)::float8 AS expires_ts,
    extract(epoch from {p}finished_at)::float8 AS finished_ts,
    {p}error
"""


_COLUMNS = _columns()
_COLUMNS_R = _columns("r.")

_TEMPLATE_COLUMNS = """
    name, namespace, image, fingerprint, spec, registered_by, active,
    extract(epoch from registered_at)::float8 AS registered_ts,
    extract(epoch from last_seen_at)::float8 AS seen_ts
"""


class PodProvisioning(interface.PodProvisioning):
    def register_template(self, name, *, namespace, image, fingerprint, spec,
                          actor, active=True):
        rows = self.db.query(
            """
            INSERT INTO pod_templates
                (name, namespace, image, fingerprint, spec, registered_by,
                 last_seen_at, active)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, clock_timestamp(), %s)
            ON CONFLICT (name) DO UPDATE
               SET namespace = excluded.namespace,
                   image = excluded.image,
                   fingerprint = excluded.fingerprint,
                   spec = excluded.spec,
                   registered_by = excluded.registered_by,
                   last_seen_at = clock_timestamp(),
                   active = excluded.active
            RETURNING """ + _TEMPLATE_COLUMNS,
            (name, namespace, image, fingerprint, json.dumps(spec, sort_keys=True),
             actor, bool(active)))
        return dict(rows[0])

    def ready(self, *, max_age_s=120.0):
        rows = self.db.query(
            """
            SELECT """ + _TEMPLATE_COLUMNS + """
              FROM pod_templates
             WHERE active
               AND last_seen_at > clock_timestamp() - make_interval(secs => %s)
             ORDER BY last_seen_at DESC, name
             LIMIT 1
            """,
            (float(max_age_s),))
        return dict(rows[0]) if rows else None

    def get(self, child):
        rows = self.db.query(
            "SELECT " + _COLUMNS + " FROM pod_requests WHERE child = %s", (child,))
        return dict(rows[0]) if rows else None

    def get_by_id(self, request_id):
        rows = self.db.query(
            "SELECT " + _COLUMNS + " FROM pod_requests WHERE id = %s::uuid",
            (str(request_id),))
        return dict(rows[0]) if rows else None

    def claim(self, worker):
        rows = self.db.query(
            """
            UPDATE pod_requests
               SET state = 'claimed',
                   claimed_at = clock_timestamp(),
                   claimed_by = %s
             WHERE id = (
                 SELECT id FROM pod_requests
                  WHERE state = 'requested'
                  ORDER BY requested_at
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1)
            RETURNING """ + _COLUMNS,
            (worker,))
        return dict(rows[0]) if rows else None

    def running(self, request_id, *, pod_uid, pod_name, actor):
        if not pod_uid or not pod_name:
            return None
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT " + _COLUMNS + " FROM pod_requests WHERE id = %s FOR UPDATE",
                (request_id,))
            if not rows:
                return None
            request = dict(rows[0])
            if request["state"] not in ("claimed", "running"):
                return None
            if request["state"] == "running":
                # Idempotent retry: only the same pod may be adopted.
                current = tx.query(
                    "SELECT pod_uid FROM pod_hosts WHERE host = %s",
                    (request["host"],))
                if not current or current[0]["pod_uid"] != pod_uid:
                    return None
                request["pod_uid"] = pod_uid
                return request
            # The spawn wrote the child row before the request. If it timed
            # out and discarded the row, the pod must not become a host.
            child = tx.query(
                "SELECT host FROM agent_registry WHERE name = %s",
                (request["child"],))
            if (not child or child[0]["host"] != request["host"]):
                return None
            tx.execute(
                """
                INSERT INTO pod_hosts
                    (host, namespace, template, parent, tenant, uid, runner_id,
                     pod_uid, state, requested_at, started_at, expires_at)
                SELECT r.host, r.namespace, r.template, r.parent, r.tenant, r.uid,
                       r.runner_id, %s, 'running', r.requested_at,
                       clock_timestamp(), r.expires_at
                  FROM pod_requests r
                 WHERE r.id = %s
                ON CONFLICT (host) DO UPDATE
                   SET namespace = excluded.namespace,
                       template = excluded.template,
                       parent = excluded.parent,
                       tenant = excluded.tenant,
                       uid = excluded.uid,
                       runner_id = excluded.runner_id,
                       pod_uid = excluded.pod_uid,
                       state = 'running',
                       started_at = COALESCE(pod_hosts.started_at,
                                             excluded.started_at),
                       expires_at = excluded.expires_at,
                       deleted_at = NULL
                """,
                (pod_uid, request_id))
            tx.execute(
                """
                INSERT INTO host_tenancy
                    (host, canon, shared, tenants, canon_ref, synced_at)
                SELECT r.host, 'pod:' || r.template, true, ARRAY[r.tenant],
                       'pod-request:' || r.id::text, clock_timestamp()
                  FROM pod_requests r
                 WHERE r.id = %s
                ON CONFLICT (host, canon) DO UPDATE
                   SET shared = true,
                       tenants = excluded.tenants,
                       canon_ref = excluded.canon_ref,
                       synced_at = clock_timestamp()
                """,
                (request_id,))
            tx.execute(
                """
                INSERT INTO host_tenants
                    (host, tenant, unix_user, uid, runner_id, registered_by,
                     registered_at)
                SELECT r.host, r.tenant, r.unix_user, r.uid, r.runner_id, %s,
                       clock_timestamp()
                  FROM pod_requests r
                 WHERE r.id = %s
                ON CONFLICT (host, tenant) DO UPDATE
                   SET unix_user = excluded.unix_user,
                       uid = excluded.uid,
                       runner_id = excluded.runner_id,
                       registered_by = excluded.registered_by,
                       registered_at = clock_timestamp()
                """,
                (actor, request_id))
            updated = tx.query(
                """
                UPDATE pod_requests
                   SET state = 'running',
                       pod_name = %s,
                       started_at = COALESCE(started_at, clock_timestamp()),
                       claimed_at = COALESCE(claimed_at, clock_timestamp())
                 WHERE id = %s AND state IN ('claimed', 'running')
                RETURNING """ + _COLUMNS,
                (pod_name, request_id))
            if not updated:
                raise RuntimeError("pod request %s changed during start" % request_id)
            result = dict(updated[0])
            result["pod_uid"] = pod_uid
            return result

    def fail(self, request_id, error):
        rows = self.db.query(
            """
            UPDATE pod_requests
               SET state = 'failed',
                   error = left(%s, 1000),
                   finished_at = clock_timestamp()
             WHERE id = %s
               AND state IN ('requested', 'claimed', 'running')
            RETURNING id
            """,
            (str(error or "unknown provisioner error"), request_id))
        return bool(rows)

    def cancel(self, child, reason):
        rows = self.db.query(
            """
            UPDATE pod_requests
               SET state = 'cancelled',
                   error = left(%s, 1000),
                   finished_at = clock_timestamp()
             WHERE child = %s
               AND state IN ('requested', 'claimed', 'running')
            RETURNING id
            """,
            (str(reason or "spawn cancelled"), child))
        return bool(rows)

    def reclaim_stale(self, *, older_than_s=300.0):
        rows = self.db.query(
            """
            UPDATE pod_requests
               SET state = 'requested',
                   claimed_at = NULL,
                   claimed_by = NULL
             WHERE state = 'claimed'
               AND claimed_at < clock_timestamp() - make_interval(secs => %s)
            RETURNING id
            """,
            (float(older_than_s),))
        return len(rows)

    def cleanup_candidates(self, *, limit=32):
        rows = self.db.query(
            """
            SELECT """ + _COLUMNS_R + """, p.pod_uid
              FROM pod_requests r
              LEFT JOIN pod_hosts p ON p.host = r.host
             WHERE r.state IN ('cancelled', 'failed', 'expired')
                OR (r.state IN ('requested', 'claimed', 'running')
                    AND r.expires_at <= clock_timestamp())
                OR r.host IN (
                    SELECT host FROM pod_hosts
                     WHERE state = 'running' AND expires_at <= clock_timestamp())
             ORDER BY r.expires_at
             LIMIT %s
            """,
            (int(limit),))
        return [dict(row) for row in rows]

    def cleanup(self, host):
        # `pod_hosts.state` (0597) n'admet que requested/running/deleted/failed :
        # un hôte échu ou échoué reste un TOMBEAU `deleted` (fail closed), la
        # cause de fin vivant dans `pod_requests.error`.
        with self.db.transaction() as tx:
            rows = tx.query(
                """
                UPDATE pod_hosts
                   SET state = 'deleted', deleted_at = clock_timestamp()
                 WHERE host = %s
                RETURNING host
                """,
                (host,))
            tx.execute("DELETE FROM host_tenants WHERE host = %s", (host,))
            tx.execute(
                "DELETE FROM host_tenancy WHERE host = %s AND canon LIKE 'pod:%%'",
                (host,))
            request = tx.query(
                """
                UPDATE pod_requests
                   SET state = 'deleted', finished_at = clock_timestamp()
                 WHERE host = %s AND state <> 'deleted'
                RETURNING id
                """,
                (host,))
        return bool(rows or request)

    def finish(self, request_id, state):
        if state not in ("deleted", "expired"):
            raise ValueError("pod request finish state %r refused" % (state,))
        rows = self.db.query(
            """
            UPDATE pod_requests
               SET state = %s, finished_at = clock_timestamp()
             WHERE id = %s AND state <> %s
            RETURNING id
            """,
            (state, request_id, state))
        return bool(rows)
