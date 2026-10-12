# SPDX-License-Identifier: AGPL-3.0-only
"""L232e: withdrawal writes serialize with claims and enable publication."""
import json

from .. import interface
from .hosts import Hosts


_ROW = ("host, mode, generation, actor, "
        "extract(epoch from requested_at)::float8 AS requested_ts, "
        "extract(epoch from return_at)::float8 AS return_ts")


class HostLifecycle(interface.HostLifecycle):
    def detect_losses(self, *, actor, older_than, critical=()):
        if isinstance(older_than, bool) or not isinstance(older_than, int) or older_than < 60:
            raise ValueError("loss threshold must be at least 60 seconds")
        # Only hosts with an observed heartbeat qualify. Missing inventory
        # and a fresh runner lease are not evidence of a lost machine.
        candidates = self.db.query(
            "SELECT host FROM host_resources GROUP BY host "
            "HAVING max(sampled_at)<clock_timestamp()-make_interval(secs=>%s) "
            "ORDER BY host", (older_than,))
        changed = []
        for candidate in candidates:
            host = candidate["host"]
            with self.db.transaction() as tx:
                Hosts._lock_gate(tx, host)
                agents = tx.query("SELECT name FROM agent_registry WHERE host=%s "
                                  "AND mode='execute' ORDER BY name FOR UPDATE", (host,))
                old = HostLifecycle(tx).get(host)
                if not agents or (old and old["mode"] == "lost"):
                    continue
                # Re-read after the gate/agent locks: a heartbeat or renewal
                # may have committed while this detector was waiting.
                stale = tx.query(
                    "SELECT host FROM host_resources WHERE host=%s GROUP BY host "
                    "HAVING max(sampled_at)<clock_timestamp()-make_interval(secs=>%s) "
                    "AND NOT EXISTS (SELECT 1 FROM agent_registry r WHERE r.host=%s "
                    "AND r.mode='execute' AND r.lease_expires_at>clock_timestamp())",
                    (host, older_than, host))
                if not stale:
                    continue
                if old and old["mode"] == "draining":
                    row = tx.query("UPDATE host_lifecycle SET mode='lost',actor=%s,return_at=NULL, "
                                   "updated_at=clock_timestamp() WHERE host=%s RETURNING " + _ROW,
                                   (actor, host))[0]
                else:
                    row = tx.query(
                        "INSERT INTO host_lifecycle(host,mode,actor) VALUES (%s,'lost',%s) "
                        "ON CONFLICT(host) DO UPDATE SET mode='lost',actor=excluded.actor, "
                        "generation=host_lifecycle.generation+1,requested_at=clock_timestamp(), "
                        "return_at=NULL,updated_at=clock_timestamp() RETURNING " + _ROW,
                        (host, actor))[0]
                for agent in agents:
                    tx.execute("INSERT INTO host_lifecycle_agents(host,generation,agent,critical) "
                               "VALUES (%s,%s,%s,%s) ON CONFLICT(host,generation,agent) "
                               "DO UPDATE SET critical=host_lifecycle_agents.critical OR excluded.critical",
                               (host, row["generation"], agent["name"], agent["name"] in critical))
                changed.append(row)
        return changed

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
            old = HostLifecycle(tx).get(host)
            if old and old["mode"] == "lost":
                raise ValueError("lost host requires verified recovery before return")
            rows = tx.query("UPDATE host_lifecycle SET mode='returning',actor=%s,return_at=NULL, "
                            "updated_at=clock_timestamp() WHERE host=%s "
                            "AND mode='draining' RETURNING " + _ROW, (actor, host))
            return rows[0] if rows else HostLifecycle(tx).get(host)

    def agents(self, host, generation):
        return self.db.query("SELECT agent, critical, operation, missing "
                             "FROM host_lifecycle_agents WHERE host=%s AND generation=%s "
                             "ORDER BY critical DESC,agent", (host, generation))

    def demand(self, agent, current_host):
        source = self.get(current_host)
        if source and source["mode"] == "draining":
            return dict(source, reason="host_drain", target=None)
        if source and source["mode"] == "lost":
            origins = self.agents(current_host, source["generation"])
            origin = next((row for row in origins if row["agent"] == agent), None)
            if origin:
                return dict(source, reason="host_loss", target=None,
                            loss_after=180 if origin["critical"] else 900)
        rows = self.db.query(
            "SELECT c.host,c.generation FROM host_lifecycle c "
            "JOIN host_lifecycle_agents a ON a.host=c.host AND a.generation=c.generation "
            "WHERE a.agent=%s AND c.mode='returning' AND c.host<>%s "
            "ORDER BY c.requested_at DESC,c.host LIMIT 1", (agent, current_host))
        return dict(rows[0], reason="host_return", target=rows[0]["host"]) if rows else None

    def progress(self, host, generation, agent, *, operation, missing):
        rows = self.db.query(
            "UPDATE host_lifecycle_agents a SET operation=%s,missing=%s::jsonb "
            "FROM host_lifecycle c WHERE a.host=c.host AND a.generation=c.generation "
            "AND c.host=%s AND c.generation=%s AND a.agent=%s RETURNING a.agent",
            (operation, json.dumps(list(missing)), host, generation, agent))
        return bool(rows)

    def expire_returns(self):
        rows = self.db.query("SELECT host,generation FROM host_lifecycle "
                             "WHERE mode='draining' AND return_at<=clock_timestamp()", ())
        changed = []
        for row in rows:
            with self.db.transaction() as tx:
                Hosts._lock_gate(tx, row["host"])
                updated = tx.query(
                    "UPDATE host_lifecycle SET mode='returning',return_at=NULL, "
                    "updated_at=clock_timestamp() WHERE host=%s AND generation=%s "
                    "AND mode='draining' AND return_at<=clock_timestamp() RETURNING " + _ROW,
                    (row["host"], row["generation"]))
                changed.extend(updated)
        return changed

    def complete_return(self, host, generation):
        with self.db.transaction() as tx:
            Hosts._lock_gate(tx, host)
            tx.query("SELECT r.name FROM agent_registry r JOIN host_lifecycle_agents a "
                     "ON a.agent=r.name WHERE a.host=%s AND a.generation=%s ORDER BY r.name "
                     "FOR UPDATE OF r", (host, generation))
            rows = tx.query(
                "UPDATE host_lifecycle c SET mode='available',updated_at=clock_timestamp() "
                "WHERE c.host=%s AND c.generation=%s AND c.mode='returning' "
                "AND NOT EXISTS (SELECT 1 FROM host_lifecycle_agents a "
                "JOIN agent_registry r ON r.name=a.agent WHERE a.host=c.host AND a.generation=c.generation "
                "AND (r.host<>c.host OR r.handoff_fenced OR EXISTS "
                "(SELECT 1 FROM host_handoffs h WHERE h.agent=r.name AND h.state NOT IN ('verified','cancelled')))) "
                "RETURNING c.host", (host, generation))
            return bool(rows)
