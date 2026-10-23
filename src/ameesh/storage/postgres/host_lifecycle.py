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
                raise ValueError("lost host requires verified recovery before return "
                                 "(`ameesh host reconcile`); a heartbeat does not reopen it")
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

    def reconcile_loss(self, host, *, actor, fresh_within):
        """#3035: reopen a lost host only after a proof that no second executor exists.

        The gate lock is the same one loss publication, reads and
        `promote_loss`/`recover_source` take: a reading or a recovery effect
        cannot interleave between the checks and the `lost -> returning ->
        available` transition. Recovery evidence is database state (readings in
        database time, origins home, no live lease, no active handoff, no
        `recover_source` since the loss), never the returning machine's own
        claim. One fresh reading is not enough: the host must have published
        continuously for at least one detection window, without a hole that
        large, and a reading dated in the future is refused.
        """
        if not isinstance(host, str) or not host:
            raise ValueError("host required")
        if (isinstance(fresh_within, bool) or not isinstance(fresh_within, int)
                or fresh_within < 60):
            raise ValueError("freshness threshold must be at least 60 seconds")
        if (not isinstance(actor, str) or not actor.strip()
                or any(ord(c) < 32 for c in actor)):
            raise ValueError("reconciliation actor required")
        with self.db.transaction() as tx:
            Hosts._lock_gate(tx, host)
            control = HostLifecycle(tx).get(host)
            if not control or control["mode"] != "lost":
                return {"host": host, "mode": control["mode"] if control else None,
                        "reconciled": False, "missing": ["not_lost"]}
            lost_ts = tx.query(
                "SELECT extract(epoch from updated_at)::float8 AS lost_ts "
                "FROM host_lifecycle WHERE host=%s", (host,))[0]["lost_ts"]
            # Read again under the lock: `hosts.record` takes it too, so a
            # heartbeat observed here stays fresh through the transition. All
            # comparisons use database time; a host clock never proves return,
            # and a reading dated in the future is refused outright.
            now = tx.query(
                "SELECT extract(epoch from clock_timestamp())::float8 AS now_ts, "
                "extract(epoch from max(sampled_at))::float8 AS latest_ts, "
                "coalesce(max(sampled_at) <= clock_timestamp(), true) AS not_future "
                "FROM host_resources WHERE host=%s", (host,))[0]
            if now["not_future"] is not True:
                return {"host": host, "mode": "lost", "reconciled": False,
                        "missing": ["future_reading"], "lost_ts": lost_ts}
            if (now["latest_ts"] is None
                    or now["now_ts"] - float(now["latest_ts"]) > fresh_within):
                return {"host": host, "mode": "lost", "reconciled": False,
                        "missing": ["fresh_reading"], "lost_ts": lost_ts}
            # Hysteresis: a single isolated reading must not reopen a host that
            # oscillates. The readings after the loss must cover a full
            # detection window, with no hole as large as that window.
            readings = tx.query(
                "SELECT extract(epoch from sampled_at)::float8 AS at_ts "
                "FROM host_resources WHERE host=%s AND sampled_at > to_timestamp(%s) "
                "AND sampled_at <= clock_timestamp() ORDER BY sampled_at", (host, lost_ts))
            times = [float(row["at_ts"]) for row in readings]
            # Measure the recent continuous suffix, not every reading since the
            # loss: an old hole before a host stabilised must not block a
            # return that has since been continuous (review B1). Coverage is
            # the span between the suffix's readings, never the silence after
            # the last one (review B2).
            start = len(times) - 1
            while start > 0 and times[start] - times[start - 1] <= fresh_within:
                start -= 1
            covered = (times[-1] - times[start]) if times else 0.0
            if len(times) - start < 2 or covered < fresh_within:
                return {"host": host, "mode": "lost", "reconciled": False,
                        "missing": ["sustained_readings"], "lost_ts": lost_ts}

            missing = []
            # Every execute agent of the host, not only the recorded origins:
            # a canon sync may have added one after the loss. Claims are closed,
            # so a live lease here would mean a renewal slipped through.
            live = tx.query(
                "SELECT name FROM agent_registry r WHERE r.host=%s AND r.mode='execute' "
                "AND r.lease_expires_at > clock_timestamp() ORDER BY name FOR UPDATE", (host,))
            missing.extend("live_lease:" + row["name"] for row in live)
            # An active handoff of any age still owns a destination reservation
            # and may be about to switch; reopening would leave it held. This
            # read is under the same gate lock as `promote_loss` and
            # `recover_source`, so a recovery commits before or after us, never
            # in the middle of the proof.
            active = tx.query(
                "SELECT id, agent FROM host_handoffs WHERE source=%s "
                "AND state NOT IN ('verified','cancelled') ORDER BY id", (host,))
            missing.extend("active_handoff:" + row["agent"] for row in active)
            # A recovery attempt since this loss is the explicit refusal: the
            # source was (or may have been) quiesced and fenced for a move.
            recovered = tx.query(
                "SELECT id, agent FROM host_handoffs WHERE source=%s "
                "AND evidence ? 'source_recovered' AND updated_at >= to_timestamp(%s) "
                "ORDER BY id", (host, lost_ts))
            missing.extend("recovery_attempted:" + row["agent"] for row in recovered)
            origins = tx.query(
                "SELECT r.name, r.host, r.handoff_fenced, "
                "EXISTS (SELECT 1 FROM host_handoffs h WHERE h.agent=r.name "
                "        AND h.state NOT IN ('verified','cancelled')) AS active_handoff "
                "FROM host_lifecycle_agents a JOIN agent_registry r ON r.name=a.agent "
                "WHERE a.host=%s AND a.generation=%s ORDER BY r.name FOR UPDATE OF r",
                (host, control["generation"]))
            for row in origins:
                if row["host"] != host:
                    missing.append("agent_moved:" + row["name"])
                if row["handoff_fenced"]:
                    missing.append("handoff_fenced:" + row["name"])
                if row["active_handoff"]:
                    missing.append("active_handoff:" + row["name"])
            if missing:
                return {"host": host, "mode": "lost", "reconciled": False,
                        "missing": sorted(set(missing)), "lost_ts": lost_ts}

            # The origin rows are home and unfenced; `complete_return`'s own
            # predicate is therefore satisfied. Keep the two recorded steps in
            # one transaction so no reader observes `returning` and starts a
            # move back before the host is open.
            returned = tx.query(
                "UPDATE host_lifecycle SET mode='returning',actor=%s,return_at=NULL, "
                "updated_at=clock_timestamp() WHERE host=%s AND mode='lost' "
                "RETURNING host", (actor, host))
            if not returned:
                return {"host": host, "mode": "lost", "reconciled": False,
                        "missing": ["not_lost"], "lost_ts": lost_ts}
            completed = tx.query(
                "UPDATE host_lifecycle c SET mode='available',updated_at=clock_timestamp() "
                "WHERE c.host=%s AND c.mode='returning' AND NOT EXISTS ("
                " SELECT 1 FROM host_lifecycle_agents a JOIN agent_registry r ON r.name=a.agent "
                " WHERE a.host=c.host AND a.generation=c.generation AND (r.host<>c.host "
                " OR r.handoff_fenced OR EXISTS (SELECT 1 FROM host_handoffs h WHERE h.agent=r.name "
                " AND h.state NOT IN ('verified','cancelled')))) RETURNING c.host", (host,))
            if not completed:
                raise ValueError("lost host return proof changed during reconciliation")
            # #3035: the first reading of the stable suffix is the start of
            # the measured recovery (fresh reading -> first lease taken).
            evidence = {
                "generation": control["generation"],
                "actor": actor,
                "from": "lost",
                "via": "returning",
                "to": "available",
                "lost_ts": lost_ts,
                "first_fresh_ts": times[start],
                "fresh_ts": now["latest_ts"],
                "readings": len(times) - start,
                "coverage_s": covered,
                "agents": [row["name"] for row in origins],
                "checks": ["fresh_reading", "sustained_readings", "no_reading_gap",
                           "agents_home", "no_live_lease", "no_active_handoff",
                           "no_recovery_attempt"],
            }
            tx.execute(
                "INSERT INTO ameesh_events(type,object,project,data) "
                "VALUES ('host.reconciled',%s,NULL,%s::jsonb)",
                ("host:" + host, json.dumps(evidence, sort_keys=True)))
            return {"host": host, "mode": "available", "reconciled": True,
                    "missing": [], "generation": control["generation"],
                    "evidence": evidence}
