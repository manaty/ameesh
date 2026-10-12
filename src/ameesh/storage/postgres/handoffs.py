# SPDX-License-Identifier: AGPL-3.0-only
"""L232a storage: reservations, lot-boundary fence and journaled CAS steps."""
import json
from contextlib import contextmanager

from .. import interface
from .registry import placement_admitted_sql
from ... import db as db_mod

COLUMNS = "*, extract(epoch from reserved_until)::float8 AS reserved_until_ts"
TERMINAL = ("verified", "cancelled")


class Handoffs(interface.Handoffs):
    @contextmanager
    def controller(self, operation):
        # Independent lock connection: each journal/fence change must commit
        # before the next external effect, especially before enabling a turn.
        lock_db = db_mod.connect(self.db.cfg)
        try:
            with lock_db.transaction() as tx:
                rows = tx.query("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired",
                                ("handoff-operation:" + operation,))
                yield self.db if rows[0]["acquired"] else None
        finally:
            lock_db.close()

    def get(self, operation):
        rows = self.db.query("SELECT " + COLUMNS + " FROM host_handoffs WHERE id = %s", (operation,))
        return rows[0] if rows else None

    def active(self, agent):
        rows = self.db.query("SELECT " + COLUMNS + " FROM host_handoffs WHERE agent = %s"
                             " AND state NOT IN ('verified','cancelled')", (agent,))
        return rows[0] if rows else None

    def occupancy(self, host):
        return self.db.query("SELECT count(*)::int AS n FROM agent_registry WHERE host=%s"
                             " AND mode='execute' AND status <> 'stopped'", (host,))[0]["n"]

    def cooling(self, agent, seconds):
        return bool(self.db.query("SELECT id FROM host_handoffs WHERE agent=%s AND state='verified'"
                                  " AND updated_at > clock_timestamp()-make_interval(secs=>%s) LIMIT 1",
                                  (agent, seconds)))

    def hold(self, operation, expected):
        with self.db.transaction() as tx:
            op = Handoffs(tx).get(operation)
            if not op or op["state"] != expected:
                return None
            tx.query("SELECT name FROM agent_registry WHERE name=%s FOR UPDATE", (op["agent"],))
            op = tx.query("SELECT " + COLUMNS + " FROM host_handoffs WHERE id=%s FOR UPDATE",
                          (operation,))[0]
            if op["state"] != expected:
                return None
            if expected not in ("planned", "prepared"):
                tx.execute("UPDATE agent_registry SET handoff_fenced=true,updated_at=now() WHERE name=%s",
                           (op["agent"],))
                tx.execute("UPDATE host_handoffs SET fenced=true WHERE id=%s AND state=%s",
                           (operation, expected))
            return Handoffs(tx).transition(operation, expected, "held", evidence={"held_at": expected},
                                           detail="prerequisite failed; operation held")

    def events(self, operation):
        return self.db.query("SELECT state, detail FROM host_handoff_events WHERE operation = %s"
                             " ORDER BY id", (operation,))

    def _event(self, operation, state, detail):
        self.db.execute("INSERT INTO host_handoff_events(operation,state,detail) VALUES (%s,%s,%s)",
                        (operation, state, detail))

    def reserve(self, operation, plan, *, ttl):
        with self.db.transaction() as tx:
            # Physical host lock, shared by all canons and competing planners.
            tx.query("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                     ("handoff-host:" + plan["target"],))
            rows = tx.query("SELECT name, host, admitted_hosts, lease_owner, lease_epoch,"
                            " status, mode, canon, (lease_expires_at > clock_timestamp()) AS live"
                            " FROM agent_registry WHERE name = %s FOR UPDATE",
                            (plan["agent"],))
            if not rows:
                return None
            agent = rows[0]
            api = Handoffs(tx)
            if api.active(plan["agent"]):
                return api.active(plan["agent"])
            if (not agent["live"] or (agent["canon"] or "") != plan["canon"]
                    or agent["host"] != plan["source"] or plan["target"] not in (agent["admitted_hosts"] or [])
                    or agent["status"] == "stopped" or agent["mode"] != "execute"
                    or agent["lease_owner"] != plan["owner"] or agent["lease_epoch"] != plan["epoch"]):
                return None
            states = tx.query("SELECT host FROM canon_state WHERE host IN (%s,%s) AND canon=%s"
                              " AND status='ok' AND last_good_commit=%s",
                              (plan["source"], plan["target"], plan["canon"], plan["canon_revision"]))
            if {r["host"] for r in states} != {plan["source"], plan["target"]}:
                return None
            reading = tx.query("SELECT extract(epoch from sampled_at)::float8 AS sampled_ts"
                               " FROM host_resources WHERE host=%s AND sampled_at <= clock_timestamp()"
                               " AND sampled_at > clock_timestamp()-interval '60 seconds'"
                               " ORDER BY sampled_at DESC,id DESC LIMIT 1", (plan["target"],))
            if not reading or reading[0]["sampled_ts"] != plan["sampled_ts"]:
                return None
            used = tx.query("SELECT count(*)::int AS slots, coalesce(sum(memory_bytes),0)::bigint AS memory,"
                            " coalesce(sum(disk_bytes),0)::bigint AS disk FROM host_handoffs"
                            " WHERE target = %s AND state NOT IN ('verified','cancelled')",
                            (plan["target"],))[0]
            occupants = tx.query("SELECT count(*)::int AS n FROM agent_registry WHERE host = %s"
                                 " AND mode = 'execute' AND status <> 'stopped'", (plan["target"],))[0]["n"]
            if (occupants + used["slots"] >= plan["max_agents"]
                    or used["memory"] + plan["memory_bytes"] > plan["available_memory"]
                    or used["disk"] + plan["disk_bytes"] > plan["available_disk"]):
                return None
            tx.execute("INSERT INTO host_handoffs(id,agent,source,target,plan,memory_bytes,disk_bytes,"
                       " reserved_until) VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,"
                       " clock_timestamp()+make_interval(secs=>%s))",
                       (operation, plan["agent"], plan["source"], plan["target"], json.dumps(plan),
                        plan["memory_bytes"], plan["disk_bytes"], ttl))
            api._event(operation, "planned", "destination capacity reserved")
            return api.get(operation)

    def renew(self, operation, *, ttl):
        with self.db.transaction() as tx:
            op = Handoffs(tx).get(operation)
            if not op or op["state"] != "held":
                return None
            tx.query("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                     ("handoff-host:" + op["target"],))
            agent = tx.query("SELECT * FROM agent_registry WHERE name=%s FOR UPDATE", (op["agent"],))[0]
            destination = op["evidence"].get("held_at") in ("switched", "activated")
            if (agent["host"] != (op["target"] if destination else op["source"])
                    or (agent["canon"] or "") != op["plan"]["canon"]
                    or agent["harness"] != op["plan"]["harness"] or agent["mode"] != "execute"
                    or agent["status"] == "stopped" or op["target"] not in (agent["admitted_hosts"] or [])):
                return None
            fresh = tx.query("SELECT host FROM host_resources WHERE host=%s"
                             " AND sampled_at <= clock_timestamp()"
                             " AND sampled_at > clock_timestamp()-interval '60 seconds' LIMIT 1", (op["target"],))
            states = tx.query("SELECT host FROM canon_state WHERE host IN (%s,%s) AND canon=%s"
                              " AND status='ok' AND last_good_commit=%s",
                              (op["source"],op["target"],op["plan"]["canon"],op["plan"]["canon_revision"]))
            if not fresh or {r["host"] for r in states} != {op["source"],op["target"]}:
                return None
            row = tx.query("UPDATE host_handoffs SET reserved_until=clock_timestamp()+make_interval(secs=>%s),"
                           " updated_at=clock_timestamp() WHERE id=%s AND state='held' RETURNING id",
                           (ttl,operation))
            if not row:
                return None
            Handoffs(tx)._event(operation, "held", "reservation renewed after explicit host reconciliation")
            return Handoffs(tx).get(operation)

    def transition(self, operation, expected, state, *, evidence=None, detail=""):
        with self.db.transaction() as tx:
            rows = tx.query("UPDATE host_handoffs SET state = %s, evidence=evidence || %s::jsonb,"
                            " updated_at=clock_timestamp() WHERE id=%s AND state=%s RETURNING id",
                            (state, json.dumps(evidence or {}), operation, expected))
            if not rows:
                return None
            api = Handoffs(tx)
            api._event(operation, state, detail)
            return api.get(operation)

    def drain(self, operation):
        with self.db.transaction() as tx:
            op = Handoffs(tx).get(operation)
            if not op or op["state"] != "prepared":
                return None
            plan = op["plan"]
            agent = tx.query("SELECT *, (lease_expires_at > clock_timestamp()) AS live"
                             " FROM agent_registry WHERE name=%s FOR UPDATE", (op["agent"],))[0]
            op = tx.query("SELECT " + COLUMNS + " FROM host_handoffs WHERE id=%s FOR UPDATE",
                          (operation,))[0]
            if op["state"] != "prepared":
                return None
            if (not agent["live"] or agent["host"] != op["source"]
                    or agent["status"] in ("running", "stopped", "paused")
                    or agent["lease_owner"] != plan["owner"] or agent["lease_epoch"] != plan["epoch"]):
                return None
            active_lot = tx.query("SELECT id FROM work_items WHERE assignee=%s"
                                  " AND state NOT IN ('intake','merged','promoted','closed') LIMIT 1",
                                  (op["agent"],))
            if active_lot:
                return None
            rows = tx.query("UPDATE host_handoffs SET state='drained', fenced=true,"
                            " updated_at=clock_timestamp() WHERE id=%s AND state='prepared'"
                            " AND reserved_until > clock_timestamp() RETURNING id", (operation,))
            if not rows:
                return None
            tx.execute("UPDATE agent_registry SET handoff_fenced=true,updated_at=now() WHERE name=%s",
                       (op["agent"],))
            Handoffs(tx)._event(operation, "drained", "lot boundary fenced; source cannot start a turn")
            return Handoffs(tx).get(operation)

    def switch(self, operation, *, cwd, brief):
        with self.db.transaction() as tx:
            op = Handoffs(tx).get(operation)
            if not op or op["state"] != "session_ready" or not op["fenced"]:
                return None
            plan = op["plan"]
            tx.query("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                     ("handoff-host:" + op["target"],))
            rows = tx.query("SELECT * FROM agent_registry WHERE name=%s FOR UPDATE", (op["agent"],))
            if not rows:
                return None
            agent = rows[0]
            if ((agent["canon"] or "") != plan["canon"] or agent["mode"] != "execute"
                    or agent["harness"] != plan["harness"]
                    or agent["host"] != op["source"] or agent["status"] in ("running", "stopped", "paused")
                    or agent["lease_epoch"] not in (plan["epoch"], plan["epoch"]+1)
                    or agent["lease_owner"] not in (None, plan["owner"])
                    or op["target"] not in (agent["admitted_hosts"] or [])):
                return None
            fresh = tx.query("SELECT id FROM host_handoffs WHERE id=%s"
                             " AND reserved_until > clock_timestamp() FOR UPDATE", (operation,))
            if not fresh:
                return None
            locked = Handoffs(tx).get(operation)
            if locked["state"] != "session_ready" or not locked["fenced"]:
                return None
            states = tx.query("SELECT host FROM canon_state WHERE host IN (%s,%s) AND canon=%s"
                              " AND status='ok' AND last_good_commit=%s",
                              (op["source"],op["target"],plan["canon"],plan["canon_revision"]))
            if {r["host"] for r in states} != {op["source"],op["target"]}:
                return None
            tx.execute("UPDATE agent_registry SET host=%s,cwd=%s,lease_owner=NULL,lease_expires_at=NULL,"
                       " lease_epoch=lease_epoch+1,session_id=NULL,session_account=NULL,session_work_item=NULL,"
                       " session_reset_at=clock_timestamp(),placement_ok=NULL,placement_profile=NULL,"
                       " pending_prompt=concat_ws(E'\\n\\n',%s::text,pending_prompt),"
                       " status='queued',status_text='host handoff',updated_at=now() WHERE name=%s",
                       (op["target"], cwd, brief, op["agent"]))
            return Handoffs(tx).transition(operation, "session_ready", "switched",
                                           detail="source lease fenced; placement and session switched")

    def unfence(self, operation, expected, state):
        with self.db.transaction() as tx:
            op = Handoffs(tx).get(operation)
            if not op or op["state"] != expected:
                return None
            tx.query("SELECT name FROM agent_registry WHERE name=%s FOR UPDATE", (op["agent"],))
            op = tx.query("SELECT " + COLUMNS + " FROM host_handoffs WHERE id=%s FOR UPDATE",
                          (operation,))[0]
            if op["state"] != expected:
                return None
            if state in ("activated", "verified"):
                allowed = tx.query("SELECT name FROM agent_registry r WHERE name=%s AND host=%s"
                                   " AND mode='execute' AND status NOT IN ('paused','stopped')"
                                   " AND coalesce(canon,'')=%s AND harness=%s"
                                   " AND %s=ANY(admitted_hosts) AND " + placement_admitted_sql("r")
                                   + " AND EXISTS (SELECT 1 FROM canon_state s"
                                   " WHERE s.host=r.host AND s.canon=coalesce(r.canon,'') AND s.status='ok'"
                                   " AND s.last_good_commit=%s)",
                                   (op["agent"],op["target"],op["plan"]["canon"],op["plan"]["harness"],
                                    op["target"],op["plan"]["canon_revision"]))
                if not allowed:
                    return None
            if state == "cancelled":
                agent = tx.query("SELECT host,lease_owner,lease_epoch FROM agent_registry WHERE name=%s",
                                 (op["agent"],))[0]
                if (agent["host"] != op["source"]
                        or agent["lease_owner"] not in (None, op["plan"]["owner"])
                        or agent["lease_epoch"] not in (op["plan"]["epoch"], op["plan"]["epoch"]+1)):
                    return None
            tx.execute("UPDATE agent_registry SET handoff_fenced=false,updated_at=now() WHERE name=%s",
                       (op["agent"],))
            tx.execute("UPDATE host_handoffs SET fenced=false WHERE id=%s", (operation,))
            return Handoffs(tx).transition(operation, expected, state, detail="claim fence released")
