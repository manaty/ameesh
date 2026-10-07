# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : liaisons de session (L41, décision 0030, migration 0034).

Une liaison active au plus par (hôte, harnais, session) : l'index unique
partiel `session_bindings_active_uq` le garantit, et `bind` n'écrit rien si
une liaison active existe déjà (`on conflict … do nothing`) — c'est
l'appelant qui décide quoi dire (même agent, autre agent).
"""
from __future__ import annotations

from .. import interface

_COLUMNS = ("id, session_id, harness, host, agent, pid, pid_start, created_by,"
            " extract(epoch from created_at)::float8 as created_ts,"
            " extract(epoch from revoked_at)::float8 as revoked_ts")


class SessionBindings(interface.SessionBindings):

    def active(self, host, harness, session_id) -> dict | None:
        rows = self.db.query(
            "select %s from session_bindings where host = %%s and harness = %%s"
            " and session_id = %%s and revoked_at is null" % _COLUMNS,
            (host, harness, session_id))
        return rows[0] if rows else None

    def bind(self, *, host, harness, session_id, agent, pid, created_by,
             pid_start=None) -> dict | None:
        rows = self.db.query(
            "insert into session_bindings (session_id, harness, host, agent, pid, pid_start,"
            " created_by) values (%%s, %%s, %%s, %%s, %%s, %%s, %%s)"
            " on conflict (host, harness, session_id) where revoked_at is null do nothing"
            " returning %s" % _COLUMNS,
            (session_id, harness, host, agent, int(pid) if pid else None,
             int(pid_start) if pid and pid_start is not None else None, created_by))
        return rows[0] if rows else None

    def set_pid(self, binding_id, pid, pid_start=None) -> dict | None:
        rows = self.db.query(
            "update session_bindings set pid = %%s, pid_start = %%s"
            " where id = %%s and revoked_at is null returning %s" % _COLUMNS,
            (int(pid) if pid else None,
             int(pid_start) if pid and pid_start is not None else None, int(binding_id)))
        return rows[0] if rows else None

    def revoke(self, host, harness, session_id) -> dict | None:
        rows = self.db.query(
            "update session_bindings set revoked_at = now() where host = %%s"
            " and harness = %%s and session_id = %%s and revoked_at is null"
            " returning %s" % _COLUMNS,
            (host, harness, session_id))
        return rows[0] if rows else None

    def listing(self, *, host=None, agent=None, include_revoked=False) -> list[dict]:
        where, params = [], []
        if not include_revoked:
            where.append("revoked_at is null")
        if host:
            where.append("host = %s")
            params.append(host)
        if agent:
            where.append("agent = %s")
            params.append(agent)
        sql = "select %s from session_bindings%s order by created_at desc, id desc" % (
            _COLUMNS, (" where " + " and ".join(where)) if where else "")
        return self.db.query(sql, tuple(params))

    def with_pids(self, host, pids) -> list[dict]:
        clean = sorted({int(p) for p in pids if int(p) > 1})
        if not clean:
            return []
        return self.db.query(
            "select %s from session_bindings where host = %%s and revoked_at is null"
            " and pid in (%s) order by id" % (_COLUMNS, ", ".join(str(p) for p in clean)),
            (host,))
