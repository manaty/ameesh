# SPDX-License-Identifier: AGPL-3.0-only
"""#7694 (design 7633 §8.3) : identité des hôtes enfants portés par un pod.

`pod_hosts` (migration 0597) est un état de provisionnement, pas un contenu
de compte : nom logique stable, pod Kubernetes (`namespace`, `pod_uid`),
template, parent, tenant, UID, dates. Le pourvoyeur d'enfants (P4) l'écrit ;
le lecteur d'une réclamation d'enfant doit refuser quand la ligne manque, a
expiré ou ne porte pas le `pod_uid` observé.
"""
from __future__ import annotations

from .. import interface

#: états acceptés par la contrainte `pod_hosts_state_valid` (0597).
STATES = ("requested", "running", "deleted", "failed")

#: colonnes rendues par les lectures (dates en epoch, comme le reste du stockage).
_COLUMNS = """
    host, namespace, template, parent, tenant, uid, runner_id, pod_uid, state,
    extract(epoch from requested_at)::float8 AS requested_ts,
    extract(epoch from started_at)::float8 AS started_ts,
    extract(epoch from expires_at)::float8 AS expires_ts,
    extract(epoch from deleted_at)::float8 AS deleted_ts
"""


class PodHosts(interface.PodHosts):
    def record(self, host, *, namespace, template, parent, tenant, uid,
               runner_id, pod_uid, expires_ts, state="running"):
        if state not in STATES:
            raise ValueError("pod_hosts state %r refused" % (state,))
        rows = self.db.query(
            """
            INSERT INTO pod_hosts
                (host, namespace, template, parent, tenant, uid, runner_id,
                 pod_uid, state, requested_at, started_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(),
                    CASE WHEN %s = 'running' THEN now() ELSE NULL END,
                    to_timestamp(%s))
            ON CONFLICT (host) DO UPDATE
               SET namespace = excluded.namespace, template = excluded.template,
                   parent = excluded.parent, tenant = excluded.tenant,
                   uid = excluded.uid, runner_id = excluded.runner_id,
                   pod_uid = excluded.pod_uid, state = excluded.state,
                   started_at = COALESCE(pod_hosts.started_at,
                                         excluded.started_at),
                   expires_at = excluded.expires_at, deleted_at = NULL
            RETURNING """ + _COLUMNS,
            (host, namespace, template, parent, tenant, int(uid), runner_id,
             pod_uid, state, state, float(expires_ts)))
        return dict(rows[0])

    def get(self, host):
        rows = self.db.query(
            "SELECT " + _COLUMNS + " FROM pod_hosts WHERE host = %s", (host,))
        return dict(rows[0]) if rows else None

    def active(self, host, pod_uid):
        rows = self.db.query(
            """
            SELECT 1 FROM pod_hosts
             WHERE host = %s AND pod_uid = %s AND state = 'running'
               AND expires_at > now()
            """, (host, pod_uid))
        return bool(rows)

    def remove(self, host, *, state="deleted"):
        if state not in STATES:
            raise ValueError("pod_hosts state %r refused" % (state,))
        rows = self.db.query(
            """
            UPDATE pod_hosts
               SET state = %s, deleted_at = now()
             WHERE host = %s
            RETURNING host
            """, (state, host))
        return bool(rows)
