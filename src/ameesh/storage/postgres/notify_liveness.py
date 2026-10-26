# SPDX-License-Identifier: AGPL-3.0-only
"""#3007 : battement partagé du notificateur et réclamation des briefs.

Le notificateur à canaux bat en base à chaque passage, par hôte et par humain
qu'il joint ; les autres hôtes lisent ces battements au lieu de conclure de
leur propre configuration (une VM sans route n'est pas une panne). Le brief
`notify_undeliverable` est porté par une passe hors du poste (le ménage du
runner) : `claim_brief` sérialise les hôtes et l'espace les relances de six
heures, sans dépendre de l'état local du notificateur.

#3576 : le document de livraison de la passe `alerts` vit ici aussi
(`notify_state`), avec son bail d'exécution. Le détenteur de la passe prend le
bail, lit le document partagé, livre puis l'écrit sous son epoch : un hôte qui
reprend le rôle ne re-pousse pas les alertes déjà livrées et n'annonce pas des
résolutions fantômes ; un détenteur périmé ne peut plus écraser son état.
"""
from __future__ import annotations

import json

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

    # -- #3576 : état de livraison partagé et son bail d'exécution ------------
    def acquire_state(self, scope, owner, ttl_seconds):
        # Same shape as `merge_locks` (0058): the row is created by the first
        # holder with an empty document, then taken by INSERT..ON CONFLICT.
        # The clock is the database's; a drifted host clock cannot make an
        # expired lease look alive (or a live one expired).
        rows = self.db.query(
            "insert into notify_state (scope, body, owner, epoch, expires_at)"
            " values (%s, '{}'::jsonb, %s, 1,"
            "         clock_timestamp() + make_interval(secs => %s))"
            " on conflict (scope) do update"
            "   set owner = excluded.owner,"
            "       epoch = notify_state.epoch + 1,"
            "       expires_at = excluded.expires_at,"
            "       updated_at = clock_timestamp()"
            " where notify_state.owner is null"
            "    or notify_state.expires_at is null"
            "    or notify_state.expires_at < clock_timestamp()"
            "    or notify_state.owner = excluded.owner"
            " returning epoch",
            (scope, owner, float(ttl_seconds)))
        return int(rows[0]["epoch"]) if rows else None

    def read_state(self, scope):
        rows = self.db.query(
            "select body from notify_state where scope = %s", (scope,))
        if not rows:
            return None
        body = rows[0]["body"]
        if isinstance(body, str):
            body = json.loads(body)
        return body if isinstance(body, dict) and body else None

    def save_state(self, scope, body, *, owner, epoch):
        with self.db.transaction() as tx:
            held = tx.query(
                "select owner, epoch from notify_state where scope = %s for update",
                (scope,))
            if not held or held[0]["owner"] != owner \
                    or int(held[0]["epoch"]) != int(epoch):
                return False
            # Recheck after the row lock, never with a pre-lock clock.
            if not tx.query(
                    "select 1 from notify_state where scope = %s"
                    " and expires_at > clock_timestamp()", (scope,)):
                return False
            tx.execute(
                "update notify_state set body = %s::jsonb,"
                " updated_at = clock_timestamp() where scope = %s",
                (json.dumps(body, ensure_ascii=False, sort_keys=True), scope))
            return True

    def release_state(self, scope, owner, epoch):
        rows = self.db.query(
            "update notify_state set owner = null, expires_at = null,"
            " updated_at = clock_timestamp()"
            " where scope = %s and owner = %s and epoch = %s"
            " returning scope",
            (scope, owner, int(epoch)))
        return bool(rows)
