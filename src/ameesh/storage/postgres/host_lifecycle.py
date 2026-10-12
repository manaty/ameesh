# SPDX-License-Identifier: AGPL-3.0-only
"""L232e: withdrawal writes serialize with claims and enable publication."""
from .. import interface
from .hosts import Hosts


_ROW = ("host, mode, generation, actor, "
        "extract(epoch from requested_at)::float8 AS requested_ts, "
        "extract(epoch from return_at)::float8 AS return_ts")


class HostLifecycle(interface.HostLifecycle):
    def get(self, host):
        rows = self.db.query("SELECT " + _ROW + " FROM host_lifecycle WHERE host=%s", (host,))
        return rows[0] if rows else None

    def listing(self):
        return self.db.query("SELECT " + _ROW + " FROM host_lifecycle ORDER BY host", ())

    def drain(self, host, *, actor, duration=None, critical=()):
        with self.db.transaction() as tx:
            Hosts._lock_gate(tx, host)
            # A claim or turn that wins this lock finishes before withdrawal;
            # a waiter reevaluates the volatile claim predicate afterwards.
            agents = tx.query("SELECT name FROM agent_registry WHERE host=%s "
                              "AND mode='execute' ORDER BY name FOR UPDATE", (host,))
            old = HostLifecycle(tx).get(host)
            if old and old["mode"] in ("draining", "lost"):
                return old
            rows = tx.query(
                "INSERT INTO host_lifecycle(host,mode,actor,return_at) "
                "VALUES (%s,'draining',%s,CASE WHEN %s::int IS NULL THEN NULL "
                "ELSE clock_timestamp()+make_interval(secs=>%s) END) "
                "ON CONFLICT(host) DO UPDATE SET mode='draining',actor=excluded.actor, "
                "generation=host_lifecycle.generation+1,requested_at=clock_timestamp(), "
                "return_at=excluded.return_at,updated_at=clock_timestamp() RETURNING " + _ROW,
                (host, actor, duration, duration))
            row = rows[0]
            for agent in agents:
                tx.execute("INSERT INTO host_lifecycle_agents(host,generation,agent,critical) "
                           "VALUES (%s,%s,%s,%s)",
                           (host, row["generation"], agent["name"], agent["name"] in critical))
            return row

    def undrain(self, host, *, actor):
        with self.db.transaction() as tx:
            Hosts._lock_gate(tx, host)
            tx.query("SELECT name FROM agent_registry WHERE host=%s ORDER BY name FOR UPDATE", (host,))
            rows = tx.query("UPDATE host_lifecycle SET mode='returning',actor=%s,return_at=NULL, "
                            "updated_at=clock_timestamp() WHERE host=%s "
                            "AND mode IN ('draining','lost') RETURNING " + _ROW, (actor, host))
            return rows[0] if rows else HostLifecycle(tx).get(host)

    def agents(self, host, generation):
        return self.db.query("SELECT agent, critical, operation, missing "
                             "FROM host_lifecycle_agents WHERE host=%s AND generation=%s "
                             "ORDER BY critical DESC,agent", (host, generation))
