# SPDX-License-Identifier: AGPL-3.0-only
"""Postgres driver: the watches ameesh manages (L121, migration 0362)."""
from __future__ import annotations

from .. import interface

_COLUMNS = ("agent, every_s, host, enabled_by,"
            " extract(epoch from enabled_at)::float8 as enabled_ts,"
            " extract(epoch from last_sent_at)::float8 as last_sent_ts")


class Watches(interface.Watches):

    def put(self, agent, every_s, host, *, actor) -> dict:
        return self.db.query(
            "insert into agent_watches (agent, every_s, host, enabled_by)"
            " values (%s, %s, %s, %s)"
            " on conflict (agent) do update set every_s = excluded.every_s,"
            " host = excluded.host, enabled_by = excluded.enabled_by, enabled_at = now()"
            " returning " + _COLUMNS, (agent, int(every_s), host, actor))[0]

    def delete(self, agent, host=None) -> dict | None:
        if host is None:
            rows = self.db.query(
                "delete from agent_watches where agent = %s returning " + _COLUMNS, (agent,))
        else:
            rows = self.db.query(
                "delete from agent_watches where agent = %s and host = %s returning "
                + _COLUMNS, (agent, host))
        return rows[0] if rows else None

    def list(self, agent=None) -> list:
        if agent is None:
            return self.db.query("select " + _COLUMNS + " from agent_watches order by agent")
        return self.db.query(
            "select " + _COLUMNS + " from agent_watches where agent = %s", (agent,))

    def deliver(self, agent, host, send) -> dict | None:
        with self.db.transaction() as tx:
            rows = tx.query("select " + _COLUMNS + " from agent_watches where agent = %s"
                            " for update", (agent,))
            if not rows:
                return None
            row = dict(rows[0], sent=False)
            if row["host"] != host:
                return row
            send()
            tx.execute("update agent_watches set last_sent_at = now() where agent = %s",
                       (agent,))
            row["sent"] = True
            return row
