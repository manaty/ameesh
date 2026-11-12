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

    def recent(self, *, agent=None, limit=50):
        limit = max(1, min(int(limit), 1000))
        if agent:
            return self.db.query(
                "SELECT " + _COLUMNS + " FROM attach_grant_events WHERE agent = %s"
                " ORDER BY at DESC, id DESC LIMIT %s", (agent, limit))
        return self.db.query(
            "SELECT " + _COLUMNS + " FROM attach_grant_events"
            " ORDER BY at DESC, id DESC LIMIT %s", (limit,))
