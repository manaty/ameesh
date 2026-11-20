# SPDX-License-Identifier: AGPL-3.0-only
"""#7695 (P2 de #7633, design 7633-hote-avatars-pod.md §3.3) : bail d'UN
superviseur par hôte pod.

Un hôte pod a un seul conteneur superviseur, mais un rollout qui chevauche,
un pod dupliqué ou une copie du StatefulSet peuvent en créer un second : les
deux écriraient `host_resources` (et la porte d'hôte) et `host_lifecycle`.
La base est l'arbitre : `pod_supervisor_leases` (migration 0599) porte le
détenteur, son époque et son expiration ; le trigger
`host_resources_pod_supervisor_guard` refuse tout relevé dont l'auteur
(`host_resources.runner_id`) n'est pas le détenteur vivant.

Le bail est pris à la première passe de ressources, renouvelé à chaque passe
et remis à l'arrêt propre. Une époque incrémentée à chaque prise interdit à
un ancien détenteur de renouveler ou de remettre le bail du suivant. Un hôte
non pod n'a aucune ligne : rien ne change pour lui.
"""
from __future__ import annotations

from .. import interface


class PodSupervisor(interface.PodSupervisor):
    def acquire(self, host, runner_id, ttl_s):
        rows = self.db.query(
            "SELECT ameesh_pod_supervisor_acquire(%s, %s, %s) AS epoch",
            (host, runner_id, float(ttl_s)))
        value = rows[0].get("epoch") if rows else None
        return int(value) if value is not None else None

    def renew(self, host, runner_id, epoch, ttl_s):
        rows = self.db.query(
            "SELECT ameesh_pod_supervisor_renew(%s, %s, %s, %s) AS renewed",
            (host, runner_id, int(epoch), float(ttl_s)))
        return bool(rows and rows[0].get("renewed"))

    def release(self, host, runner_id, epoch):
        rows = self.db.query(
            "SELECT ameesh_pod_supervisor_release(%s, %s, %s) AS released",
            (host, runner_id, int(epoch)))
        return bool(rows and rows[0].get("released"))

    def holder(self, host):
        rows = self.db.query(
            "SELECT ameesh_pod_supervisor_holder(%s) AS holder", (host,))
        return rows[0].get("holder") if rows else None

    def lease(self, host):
        rows = self.db.query(
            """
            SELECT host, runner_id, epoch,
                   extract(epoch from acquired_at)::float8 AS acquired_ts,
                   extract(epoch from renewed_at)::float8 AS renewed_ts,
                   extract(epoch from expires_at)::float8 AS expires_ts,
                   extract(epoch from released_at)::float8 AS released_ts,
                   released_at is null
                     and expires_at > clock_timestamp() AS live
              FROM pod_supervisor_leases
             WHERE host = %s
            """, (host,))
        return dict(rows[0]) if rows else None
