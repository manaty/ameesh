# SPDX-License-Identifier: AGPL-3.0-only
"""#3007 : battement partagé du notificateur et réclamation des briefs.

Le notificateur à canaux bat en base à chaque passage, par hôte et par humain
qu'il joint ; les autres hôtes lisent ces battements au lieu de conclure de
leur propre configuration (une VM sans route n'est pas une panne). Le brief
`notify_undeliverable` est porté par une passe hors du poste (le ménage du
runner) : `claim_brief` sérialise les hôtes et l'espace les relances de six
heures, sans dépendre de l'état local du notificateur.
"""
from __future__ import annotations

from .. import interface


class NotifyLiveness(interface.NotifyLiveness):
    def beat(self, host, humans, *, now=None):
        humans = [str(human) for human in humans if human]
        if not humans:
            return 0
        with self.db.transaction() as tx:
            for human in humans:
                tx.execute(
                    "insert into notify_liveness (host, human, seen_at)"
                    " values (%s, %s, coalesce(to_timestamp(%s::float8),"
                    " clock_timestamp()))"
                    " on conflict (host, human) do update"
                    " set seen_at = excluded.seen_at",
                    (host, human, now))
        return len(humans)

    def latest(self):
        rows = self.db.query(
            "select host, human, extract(epoch from seen_at)::float8 as seen_ts"
            " from notify_liveness")
        return [dict(row) for row in rows]

    def claim_brief(self, recipient, human, *, since, now=None):
        with self.db.transaction() as tx:
            # Same (recipient, human) from two hosts: one claims, the other
            # sees the fresh claim and skips. The lock is held only for the
            # read-and-write of the claim, never across the mail deposit.
            tx.query("select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                     ("notify-brief:" + recipient + ":" + human,))
            rows = tx.query(
                "select extract(epoch from sent_at)::float8 as sent_ts"
                " from notify_briefs where recipient = %s and human = %s",
                (recipient, human))
            previous = float(rows[0]["sent_ts"]) if rows else None
            if previous is not None and previous >= float(since):
                return {"claimed": False, "previous_ts": previous}
            tx.execute(
                "insert into notify_briefs (recipient, human, sent_at)"
                " values (%s, %s, coalesce(to_timestamp(%s::float8),"
                " clock_timestamp()))"
                " on conflict (recipient, human) do update"
                " set sent_at = excluded.sent_at",
                (recipient, human, now))
            return {"claimed": True, "previous_ts": previous}

    def release_brief(self, recipient, human):
        self.db.execute(
            "delete from notify_briefs where recipient = %s and human = %s",
            (recipient, human))
