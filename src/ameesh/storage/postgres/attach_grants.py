# SPDX-License-Identifier: AGPL-3.0-only
"""#7119 (avatar D1) : journal des grants d'attach, sur Postgres (migration 0574)."""
from __future__ import annotations

from ..attach_grants import AttachGrants as Contract

_COLUMNS = """
    id, extract(epoch FROM at)::float8 AS at_ts, outcome, code, reason, human, agent,
    host, tenant, device_key, attach_id, nonce, exp, checked_by
"""


class AttachGrants(Contract):

    def __init__(self, db):
        self.db = db

    def record(self, *, outcome, code, reason, human, agent, host, tenant, device_key,
               attach_id, nonce, exp, checked_by):
        # `ON CONFLICT` ne vise que l'index partiel des grants acceptés : un
        # second `granted` pour le même attach_id n'écrit rien (None).
        rows = self.db.query(
            "INSERT INTO attach_grant_events"
            "       (outcome, code, reason, human, agent, host, tenant, device_key,"
            "        attach_id, nonce, exp, checked_by)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (attach_id) WHERE outcome = 'granted' DO NOTHING"
            " RETURNING id",
            (outcome, code, reason, human, agent, host, tenant, device_key, attach_id,
             nonce, exp, checked_by))
        return int(rows[0]["id"]) if rows else None

    def record_granted(self, *, device_authenticator_id, human, agent, host, tenant,
                       device_key, attach_id, nonce, exp, reason, checked_by):
        with self.db.transaction() as tx:
            # 1. le verrou, sans condition d'état dans le WHERE (motif de 0011 :
            #    une révocation concurrente est attendue, puis relue) ;
            # 2. l'état relu sous ce verrou ; 3. l'écriture.
            locked = tx.query("SELECT id FROM authenticators WHERE id = %s FOR SHARE",
                              (int(device_authenticator_id),))
            live = locked and tx.query(
                "SELECT 1 AS live FROM authenticators WHERE id = %s AND revoked_at IS NULL",
                (int(device_authenticator_id),))
            if not live:
                return "device_revoked", None
            # ⏱ à l'heure réelle de la base, après le dernier verrou et après
            # l'écriture (l'INSERT peut attendre un grant concurrent) : un
            # dépassement lève et annule toute la transaction (0011)
            what = "grant d'attach %s" % attach_id
            tx.query("SELECT ameesh_receipt_deadlines(%s, %s, NULL, NULL, NULL) AS ok",
                     (what, int(exp)))
            rows = tx.query(
                "INSERT INTO attach_grant_events"
                "       (outcome, code, reason, human, agent, host, tenant, device_key,"
                "        attach_id, nonce, exp, checked_by)"
                " VALUES ('granted', 'ok', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (attach_id) WHERE outcome = 'granted' DO NOTHING"
                " RETURNING id",
                (reason, human, agent, host, tenant, device_key, attach_id, nonce, exp,
                 checked_by))
            if not rows:
                return "used", None
            tx.query("SELECT ameesh_receipt_deadlines(%s, %s, NULL, NULL, NULL) AS ok",
                     (what, int(exp)))
            return "ok", int(rows[0]["id"])

    def recent(self, *, agent=None, limit=50):
        limit = max(1, min(int(limit), 1000))
        if agent:
            return self.db.query(
                "SELECT " + _COLUMNS + " FROM attach_grant_events WHERE agent = %s"
                " ORDER BY at DESC, id DESC LIMIT %s", (agent, limit))
        return self.db.query(
            "SELECT " + _COLUMNS + " FROM attach_grant_events"
            " ORDER BY at DESC, id DESC LIMIT %s", (limit,))
