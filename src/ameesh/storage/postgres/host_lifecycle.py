# SPDX-License-Identifier: AGPL-3.0-only
"""L232e: withdrawal writes serialize with claims and enable publication."""
import json

from .. import interface
from .hosts import Hosts


_ROW = ("host, mode, generation, actor, "
        "extract(epoch from requested_at)::float8 AS requested_ts, "
        "extract(epoch from return_at)::float8 AS return_ts, "
        "release_state, hook_state, claims_open, release_generation, automatic, "
        "extract(epoch from released_at)::float8 AS released_ts, "
        "release_error, release_error_state, "
        "extract(epoch from release_error_at)::float8 AS release_error_ts, "
        "extract(epoch from release_error_since)::float8 AS release_error_since_ts, "
        "extract(epoch from hook_attempt_at)::float8 AS hook_attempt_ts, "
        "hook_attempt_state, hook_attempt_generation, "
        "extract(epoch from updated_at)::float8 AS updated_ts")

#: #4148: states an adapter can acknowledge, in the strict order of the spec.
RELEASE_STATES = ("busy", "draining", "idle-drained")


def _check_state(state):
    if state not in RELEASE_STATES:
        raise ValueError("release state: %s expected" % " | ".join(RELEASE_STATES))
    return state


def _positive_seconds(value, name, *, minimum=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not value >= minimum:
        raise ValueError("%s: a number of seconds >= %g expected" % (name, minimum))
    return float(value)


def _release_elected(tx, host):
    """#3029 : un drain planifié rend le bail d'un rôle élu tout de suite.

    Le drain verrouille toutes les lignes d'agents de l'hôte (rôles élus
    compris), donc cette écriture sérialise avec un `renew` ou une
    réclamation concurrente. L'epoch avance : l'ancien détenteur est fencé
    (`renew`, `begin_turn`, `end_turn` refusés) et le bail libre est
    réclamable par un autre candidat sans attendre le TTL. La ligne garde son
    hôte jusqu'à cette réclamation ; l'hôte drainé, lui, ne peut plus
    réclamer. Mêmes colonnes que `release` : rien d'autre n'est effacé.
    """
    return tx.query(
        "UPDATE agent_registry SET lease_owner = NULL, lease_expires_at = NULL, "
        "lease_epoch = lease_epoch + 1, "
        "status = CASE WHEN status = 'running' THEN 'idle' ELSE status END, "
        "updated_at = now() "
        "WHERE host = %s AND mode = 'execute' AND releve = 'election' "
        "AND lease_owner IS NOT NULL "
        "RETURNING name", (host,))


class HostLifecycle(interface.HostLifecycle):
    def detect_losses(self, *, actor, older_than, critical=()):
        if isinstance(older_than, bool) or not isinstance(older_than, int) or older_than < 60:
            raise ValueError("loss threshold must be at least 60 seconds")
        # Only hosts with an observed heartbeat qualify. Missing inventory
        # and a fresh runner lease are not evidence of a lost machine.
        # #3631 : the candidate hosts are the registry's (the pass skips a
        # host without an executor agent anyway) and the last reading is one
        # index lookup per host — the old GROUP BY over host_resources read
        # the whole history on every pass of every executor.
        candidates = self.db.query(
            "SELECT h.host FROM (SELECT DISTINCT host FROM agent_registry"
            "                    WHERE mode = 'execute' AND host IS NOT NULL) h"
            " WHERE (SELECT max(sampled_at) FROM host_resources r"
            "         WHERE r.host = h.host)"
            "       < clock_timestamp()-make_interval(secs=>%s)"
            # #4148: a host released by the lifecycle (VM off, pod evicted)
            # stops publishing on purpose. It is idle-drained, not lost; a
            # hook still in flight for that state says the same.
            "   AND NOT EXISTS (SELECT 1 FROM host_lifecycle c"
            "                    WHERE c.host = h.host"
            "                      AND (c.release_state = 'idle-drained'"
            "                           OR c.hook_attempt_state = 'idle-drained'))"
            " ORDER BY h.host", (older_than,))
        changed = []
        for candidate in candidates:
            host = candidate["host"]
            with self.db.transaction() as tx:
                Hosts._lock_gate(tx, host)
                # L165 (EP1) : un rôle élu n'est pas déplacé par L232e ; son
                # bail expiré est repris par un autre candidat, sans preuve de
                # source. Il n'entre donc pas dans le journal de l'hôte perdu.
                agents = tx.query("SELECT name FROM agent_registry WHERE host=%s "
                                  "AND mode='execute' AND releve <> 'election' "
                                  "ORDER BY name FOR UPDATE", (host,))
                old = HostLifecycle(tx).get(host)
                if not agents or (old and old["mode"] == "lost"):
                    continue
                if old and (old.get("release_state") == "idle-drained"
                            or old.get("hook_attempt_state") == "idle-drained"):
                    # #4148: off by decision, between two readings.
                    continue
                # Re-read after the gate/agent locks: a heartbeat or renewal
                # may have committed while this detector was waiting.
                stale = tx.query(
                    "SELECT host FROM (VALUES (%s::text)) v(host)"
                    " WHERE (SELECT max(sampled_at) FROM host_resources r"
                    "         WHERE r.host = v.host)"
                    "       < clock_timestamp()-make_interval(secs=>%s)"
                    "   AND NOT EXISTS (SELECT 1 FROM agent_registry r WHERE r.host=%s "
                    "   AND r.mode='execute' AND r.lease_expires_at>clock_timestamp())",
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

    def drain(self, host, *, actor, duration=None, critical=(), automatic=False):
        if not isinstance(automatic, bool):
            raise ValueError("automatic: a boolean expected")
        with self.db.transaction() as tx:
            Hosts._lock_gate(tx, host)
            # A claim or turn that wins this lock finishes before withdrawal;
            # a waiter reevaluates the volatile claim predicate afterwards.
            # L165 (EP1) : un rôle élu n'entre pas dans le journal de L232e
            # (rien à déplacer), mais sa ligne est verrouillée ici pour que le
            # drain rende son bail sans course avec un renew ou une
            # réclamation (#3029).
            agents = tx.query("SELECT name, releve FROM agent_registry WHERE host=%s "
                              "AND mode='execute' ORDER BY name FOR UPDATE", (host,))
            old = HostLifecycle(tx).get(host)
            if old and old["mode"] in ("draining", "lost"):
                # Un drain déjà posé reste idempotent ; s'il est toujours
                # planifié, un bail élu réapparu entre-temps est rendu lui
                # aussi. Un hôte perdu garde la règle du TTL.
                if old["mode"] == "draining":
                    _release_elected(tx, host)
                return old
            rows = tx.query(
                "INSERT INTO host_lifecycle(host,mode,actor,return_at,release_state,"
                "release_generation,claims_open,automatic) "
                "VALUES (%s,'draining',%s,CASE WHEN %s::int IS NULL THEN NULL "
                "ELSE clock_timestamp()+make_interval(secs=>%s) END,"
                "'draining',1,false,%s) "
                "ON CONFLICT(host) DO UPDATE SET mode='draining',actor=excluded.actor, "
                "generation=host_lifecycle.generation+1,requested_at=clock_timestamp(), "
                "return_at=excluded.return_at, "
                # #4148: a new withdrawal closes claims before any adapter
                # transition; the acknowledged hook state is kept (busy after a
                # return, idle-drained if the host was released without a busy
                # acknowledgement, which the release scanner restores first).
                "release_state='draining',claims_open=false, "
                "release_generation=host_lifecycle.generation+1, "
                "automatic=excluded.automatic,released_at=NULL, "
                "release_error=NULL,release_error_state=NULL,release_error_at=NULL, "
                "release_error_since=NULL,hook_attempt_at=NULL,"
                "hook_attempt_state=NULL,hook_attempt_generation=NULL, "
                "updated_at=clock_timestamp() RETURNING " + _ROW,
                (host, actor, duration, duration, automatic))
            row = rows[0]
            for agent in agents:
                if agent["releve"] == "election":
                    continue
                tx.execute("INSERT INTO host_lifecycle_agents(host,generation,agent,critical) "
                           "VALUES (%s,%s,%s,%s)",
                           (host, row["generation"], agent["name"], agent["name"] in critical))
            _release_elected(tx, host)
            return row

    def undrain(self, host, *, actor, claims_open=True):
        if not isinstance(claims_open, bool):
            raise ValueError("claims_open: a boolean expected")
        with self.db.transaction() as tx:
            Hosts._lock_gate(tx, host)
            tx.query("SELECT name FROM agent_registry WHERE host=%s ORDER BY name FOR UPDATE", (host,))
            old = HostLifecycle(tx).get(host)
            if old and old["mode"] == "lost":
                raise ValueError("lost host requires verified recovery before return "
                                 "(`ameesh host reconcile`); a heartbeat does not reopen it")
            rows = tx.query(
                "UPDATE host_lifecycle SET mode='returning',actor=%s,return_at=NULL, "
                "claims_open=%s, "
                # the release state follows the opened gate; a closed return
                # keeps saying `draining` until the busy hook answers.
                "release_state=CASE WHEN %s THEN 'busy' ELSE release_state END, "
                "updated_at=clock_timestamp() WHERE host=%s "
                "AND mode='draining' RETURNING " + _ROW,
                (actor, claims_open, claims_open, host))
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

    def expire_returns(self, *, open_claims=True):
        if not isinstance(open_claims, bool):
            raise ValueError("open_claims: a boolean expected")
        rows = self.db.query("SELECT host,generation FROM host_lifecycle "
                             "WHERE mode='draining' AND return_at<=clock_timestamp()", ())
        changed = []
        for row in rows:
            with self.db.transaction() as tx:
                Hosts._lock_gate(tx, row["host"])
                updated = tx.query(
                    "UPDATE host_lifecycle SET mode='returning',return_at=NULL, "
                    # #4148: the busy hook must answer before claims reopen.
                    "claims_open=CASE WHEN %s THEN true ELSE claims_open END, "
                    "release_state=CASE WHEN %s THEN 'busy' ELSE release_state END, "
                    "updated_at=clock_timestamp() WHERE host=%s AND generation=%s "
                    "AND mode='draining' AND return_at<=clock_timestamp() RETURNING " + _ROW,
                    (open_claims, open_claims, row["host"], row["generation"]))
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
                # #4148: a return is complete only once the busy hook has
                # reopened claims; otherwise the host would be `available`
                # with no lease possible, and the next drain would look idle.
                "AND c.claims_open "
                "AND NOT EXISTS (SELECT 1 FROM host_lifecycle_agents a "
                "JOIN agent_registry r ON r.name=a.agent WHERE a.host=c.host AND a.generation=c.generation "
                "AND (r.host<>c.host OR r.handoff_fenced OR EXISTS "
                "(SELECT 1 FROM host_handoffs h WHERE h.agent=r.name AND h.state NOT IN ('verified','cancelled')))) "
                "RETURNING c.host", (host, generation))
            return bool(rows)

    # -- #4148: adapter transitions, journal and scanner lock ---------------
    def begin_transition(self, host, generation, state, *, actor, stale_after,
                         retry_after):
        """Atomically claim one adapter call; see the interface contract."""
        _check_state(state)
        stale_after = _positive_seconds(stale_after, "stale_after", minimum=1.0)
        retry_after = _positive_seconds(retry_after, "retry_after")
        if not isinstance(actor, str) or not actor.strip() \
                or any(ord(c) < 32 for c in actor):
            raise ValueError("transition actor required")
        rows = self.db.query(
            "UPDATE host_lifecycle c SET hook_attempt_at=clock_timestamp(), "
            "hook_attempt_state=%s,hook_attempt_generation=c.generation, "
            "updated_at=clock_timestamp() "
            "WHERE c.host=%s AND c.generation=%s "
            "AND c.hook_state IS DISTINCT FROM %s "
            "AND NOT EXISTS (SELECT 1 FROM host_lifecycle_transitions t "
            "                WHERE t.host=c.host AND t.generation=c.generation "
            "                  AND t.state=%s) "
            "AND (c.hook_attempt_at IS NULL OR c.hook_attempt_at <= "
            "     clock_timestamp()-make_interval(secs=>%s)) "
            # A failed transition is retried after its own delay; a failure of
            # another state (an idle-drained timeout then an operator's
            # undrain) must not block the safe `busy` answer.
            "AND (c.release_error_at IS NULL OR c.release_error_state IS DISTINCT FROM %s "
            "     OR c.release_error_at <= clock_timestamp()-make_interval(secs=>%s)) "
            "RETURNING " + _ROW,
            (state, host, generation, state, state, stale_after, state, retry_after))
        return rows[0] if rows else None

    def finish_transition(self, host, generation, state, *, actor, detail,
                          error=None):
        """Journal a successful transition, or keep the previous state on failure."""
        _check_state(state)
        if not isinstance(actor, str) or not actor.strip() \
                or any(ord(c) < 32 for c in actor):
            raise ValueError("transition actor required")
        if not isinstance(detail, dict):
            raise ValueError("transition detail: an object expected")
        if error is not None and not isinstance(error, str):
            raise ValueError("transition error: text expected")
        with self.db.transaction() as tx:
            Hosts._lock_gate(tx, host)
            control = HostLifecycle(tx).get(host)
            if not control or control["generation"] != generation:
                return None
            if error is not None:
                text = " ".join(str(error).split())[:500]
                tx.execute(
                    "UPDATE host_lifecycle SET hook_attempt_at=NULL,"
                    "hook_attempt_state=NULL,hook_attempt_generation=NULL,"
                    "release_error=%s,release_error_state=%s,"
                    "release_error_at=clock_timestamp(),"
                    "release_error_since=coalesce(release_error_since,clock_timestamp()),"
                    "updated_at=clock_timestamp() WHERE host=%s AND generation=%s",
                    (text, state, host, generation))
                tx.execute(
                    "INSERT INTO ameesh_events(type,object,project,data) "
                    "VALUES ('host.release_failed',%s,NULL,%s::jsonb)",
                    ("host:" + host, json.dumps({"generation": generation, "state": state,
                                                 "actor": actor, "error": text},
                                                sort_keys=True)))
            else:
                tx.execute(
                    "INSERT INTO host_lifecycle_transitions(host,generation,state,actor,detail) "
                    "VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING",
                    (host, generation, state, actor, json.dumps(detail, sort_keys=True)))
                tx.execute(
                    "UPDATE host_lifecycle SET hook_state=%s,hook_attempt_at=NULL,"
                    "hook_attempt_state=NULL,hook_attempt_generation=NULL,"
                    "release_error=NULL,release_error_state=NULL,release_error_at=NULL,"
                    "release_error_since=NULL,"
                    "release_state=CASE WHEN %s='busy' THEN 'busy' "
                    "                  WHEN %s='idle-drained' THEN 'idle-drained' "
                    "                  ELSE release_state END,"
                    "released_at=CASE WHEN %s='idle-drained' THEN clock_timestamp() "
                    "                  ELSE released_at END,"
                    "updated_at=clock_timestamp() WHERE host=%s AND generation=%s",
                    (state, state, state, state, host, generation))
            return HostLifecycle(tx).get(host)

    def open_claims(self, host, generation, *, require_busy=True):
        rows = self.db.query(
            "UPDATE host_lifecycle SET claims_open=true,updated_at=clock_timestamp() "
            "WHERE host=%s AND generation=%s AND (NOT %s OR hook_state='busy') "
            "AND NOT claims_open RETURNING host", (host, generation, bool(require_busy)))
        return bool(rows)

    def transition_journal(self, host):
        rows = self.db.query(
            "SELECT host,generation,state,actor,detail,"
            "extract(epoch from created_at)::float8 AS created_ts "
            "FROM host_lifecycle_transitions WHERE host=%s "
            "ORDER BY created_at,state", (host,))
        for row in rows:
            if isinstance(row.get("detail"), str):
                try:
                    row["detail"] = json.loads(row["detail"])
                except ValueError:
                    row["detail"] = {}
            elif row.get("detail") is None:
                row["detail"] = {}
        return rows

    def claim_release_scan(self, host, *, owner, lock_seconds, scan_interval):
        if not isinstance(owner, str) or not owner.strip() \
                or any(ord(c) < 32 for c in owner):
            raise ValueError("release scan owner required")
        lock_seconds = _positive_seconds(lock_seconds, "lock_seconds", minimum=1.0)
        scan_interval = _positive_seconds(scan_interval, "scan_interval")
        rows = self.db.query(
            "INSERT INTO host_release_locks(host,owner,expires_at,next_scan_at) "
            "VALUES (%s,%s,clock_timestamp()+make_interval(secs=>%s),clock_timestamp()) "
            "ON CONFLICT(host) DO UPDATE SET owner=excluded.owner,"
            "expires_at=excluded.expires_at,"
            "next_scan_at=coalesce(host_release_locks.next_scan_at,excluded.next_scan_at),"
            "updated_at=clock_timestamp() "
            "WHERE host_release_locks.expires_at <= clock_timestamp() "
            "   OR host_release_locks.owner = excluded.owner "
            "RETURNING host,owner,"
            "extract(epoch from expires_at)::float8 AS expires_ts,"
            "extract(epoch from next_scan_at)::float8 AS next_scan_ts,"
            "(next_scan_at IS NULL OR next_scan_at <= clock_timestamp()) AS scan_due",
            (host, owner, lock_seconds))
        if not rows:
            return None
        row = rows[0]
        if row["scan_due"]:
            self.db.execute(
                "UPDATE host_release_locks SET next_scan_at="
                "clock_timestamp()+make_interval(secs=>%s),updated_at=clock_timestamp() "
                "WHERE host=%s AND owner=%s", (scan_interval, host, owner))
            row["next_scan_ts"] = None
        return row

    def finish_release_scan(self, host, *, owner, next_scan_at=None):
        if next_scan_at is not None:
            next_scan_at = float(next_scan_at)
        rows = self.db.query(
            "UPDATE host_release_locks SET expires_at=clock_timestamp()-interval '1 second',"
            "next_scan_at=coalesce(%s,next_scan_at),updated_at=clock_timestamp() "
            "WHERE host=%s AND owner=%s RETURNING host",
            (next_scan_at, host, owner))
        return bool(rows)

    def note_release_blocked(self, host, *, owner, reason=None, source=None):
        if reason is None:
            rows = self.db.query(
                "UPDATE host_release_locks SET blocked_reason=NULL,"
                "blocked_source=NULL,blocked_since=NULL,updated_at=clock_timestamp() "
                "WHERE host=%s AND owner=%s RETURNING host", (host, owner))
        else:
            text = " ".join(str(reason).split())[:300]
            src = None if source is None else " ".join(str(source).split())[:300]
            rows = self.db.query(
                "UPDATE host_release_locks SET blocked_reason=%s,blocked_source=%s,"
                "blocked_since=coalesce(blocked_since,clock_timestamp()),"
                "updated_at=clock_timestamp() "
                "WHERE host=%s AND owner=%s RETURNING host", (text, src, host, owner))
        return bool(rows)

    def release_locks(self):
        return self.db.query(
            "SELECT host,owner,blocked_reason,blocked_source,"
            "extract(epoch from blocked_since)::float8 AS blocked_since_ts,"
            "extract(epoch from expires_at)::float8 AS expires_ts,"
            "extract(epoch from next_scan_at)::float8 AS next_scan_ts,"
            "extract(epoch from updated_at)::float8 AS updated_ts "
            "FROM host_release_locks ORDER BY host")

    def reconcile_loss(self, host, *, actor, fresh_within):
        """#3035: reopen a lost host only after a proof that no second executor exists.

        The gate lock is the same one loss publication, reads and
        `promote_loss`/`recover_source` take: a reading or a recovery effect
        cannot interleave between the checks and the `lost -> returning ->
        available` transition. Recovery evidence is database state (readings in
        database time, origins home, no fence, no active handoff, no
        `recover_source` since the loss), never the returning machine's own
        claim. A live lease of a home agent does not block the return (#3180):
        it is the returning host's own runner, and a move away is refused by
        the origin check. One fresh reading is not enough: the host must have
        published continuously for at least one detection window, without a
        hole that large, and a reading dated in the future is refused.
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
            # #3180: a returning host's own runners reclaim their leases as
            # soon as they boot, before the sustained-reading window closes.
            # That lease is the host's own executor, not a second one; a move
            # away from the host is refused below (`agent_moved`), as are a
            # fence and an active handoff. Detection already required no live
            # lease at the instant of the loss.
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
                "(r.lease_expires_at > clock_timestamp()) AS live, "
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
                "UPDATE host_lifecycle c SET mode='available',"
                "claims_open=true,release_state='busy',hook_state='busy',"
                "updated_at=clock_timestamp() "
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
                # The issue measures the outage from the first reading after
                # the loss; the stable suffix is the proof, not that start.
                "first_fresh_ts": times[0],
                "suffix_start_ts": times[start],
                "fresh_ts": now["latest_ts"],
                "readings": len(times) - start,
                "readings_since_loss": len(times),
                "coverage_s": covered,
                "agents": [row["name"] for row in origins],
                # Informational: the returning machine's own runners had
                # already taken their leases back; they are not a refusal.
                "live_lease_agents": [row["name"] for row in origins if row["live"]],
                "checks": ["fresh_reading", "sustained_readings", "no_reading_gap",
                           "agents_home", "no_active_handoff", "no_recovery_attempt"],
            }
            tx.execute(
                "INSERT INTO ameesh_events(type,object,project,data) "
                "VALUES ('host.reconciled',%s,NULL,%s::jsonb)",
                ("host:" + host, json.dumps(evidence, sort_keys=True)))
            return {"host": host, "mode": "available", "reconciled": True,
                    "missing": [], "generation": control["generation"],
                    "evidence": evidence}
