# SPDX-License-Identifier: AGPL-3.0-only
"""#5925: operator adapter journal, fenced by native lifecycle generations."""
from .hosts import Hosts
from .host_lifecycle import HostLifecycle


class Queue:
    def __init__(self, db):
        self.db = db

    def control(self, host):
        return HostLifecycle(self.db).get(host)

    def now(self):
        return float(self.db.query(
            'SELECT extract(epoch FROM clock_timestamp())::float8 AS now_ts')[0]['now_ts'])

    def worker_lock(self, host):
        return self.db.query(
            'SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS ok',
            ('vm-adapter:' + host,))[0]['ok']

    def lock(self, host):
        # Bounded external I/O is performed under this gate: a new generation
        # cannot reopen claims between the final check and a provider action.
        self.db.execute("SET LOCAL idle_in_transaction_session_timeout='120s'")
        Hosts._lock_gate(self.db, host)
        # Native transition attempts and return intents also update this row
        # directly. Fence them through the bounded provider action as well.
        self.db.query('SELECT host FROM host_lifecycle WHERE host=%s FOR UPDATE', (host,))
        return self.control(host)

    def request(self, host, generation, epoch, state):
        rows = self.db.query("""
            INSERT INTO vm_adapter_requests(host,generation,release_epoch,state)
            SELECT host,generation,release_epoch,%s FROM host_lifecycle
             WHERE host=%s AND generation=%s AND release_epoch=%s
               AND mode IN ('draining','returning') AND NOT claims_open
            ON CONFLICT(host,generation,release_epoch,state) DO UPDATE
               SET host=excluded.host
            RETURNING status
        """, (state, host, generation, epoch))
        return rows[0]['status'] if rows else 'obsolete'

    def status(self, host, generation, epoch, state):
        rows = self.db.query('SELECT status FROM vm_adapter_requests WHERE host=%s '
                             'AND generation=%s AND release_epoch=%s AND state=%s',
                             (host, generation, epoch, state))
        return rows[0]['status'] if rows else None

    def stop_intent(self, request):
        # Durable BEFORE the external shutdown: native loss detection already
        # recognizes an idle-drained attempt as intentionally unavailable.
        self.db.execute("UPDATE host_lifecycle SET hook_attempt_at=clock_timestamp(),"
                        "hook_attempt_state='idle-drained',hook_attempt_generation=generation,"
                        "hook_attempt_epoch=release_epoch WHERE host=%s AND generation=%s "
                        "AND release_epoch=%s AND NOT claims_open",
                        (request['host'], request['generation'], request['release_epoch']))
        self.finish(request, 'pending', 'poweroff intent')

    def refresh_stop_intent(self, host):
        self.db.execute("UPDATE host_lifecycle SET hook_attempt_at=clock_timestamp() "
                        "WHERE host=%s AND hook_attempt_state='idle-drained'", (host,))

    def complete(self, request, result):
        self.finish(request, 'done', result)
        # Async completion must not depend on a scanner retry inside the
        # drain timeout (billing hysteresis can be longer than that timeout).
        HostLifecycle(self.db).finish_transition(
            request['host'], request['generation'], request['state'],
            release_epoch=request['release_epoch'], actor='vm-adapter',
            detail={'adapter': 'scaleway', 'result': result})

    def pending(self, host):
        return self.db.query("SELECT * FROM vm_adapter_requests WHERE host=%s "
                             "AND status='pending' ORDER BY "
                             "CASE state WHEN 'busy' THEN 0 WHEN 'draining' THEN 1 ELSE 2 END, "
                             "created_at LIMIT 1", (host,))

    def pending_all(self, host):
        """#5932: every queued request of a host, for the wait/stall alerts.

        Epochs are extracted so the alert pass can age a durable poweroff
        intent and spot an intent whose generation/epoch no longer matches
        the live control (the worker has not reconciled it)."""
        return self.db.query(
            "SELECT host,generation,release_epoch,state,status,result,"
            "extract(epoch from created_at)::float8 AS created_ts,"
            "extract(epoch from updated_at)::float8 AS updated_ts "
            "FROM vm_adapter_requests WHERE host=%s AND status='pending' "
            "ORDER BY created_at,state", (host,))

    def worker_seen(self, host):
        """#5932: epoch of the last worker tick for a host, or None.

        `observe` updates this row on every operator tick, so a stale value is
        the evidence that the worker stopped progressing."""
        rows = self.db.query(
            "SELECT extract(epoch from updated_at)::float8 AS seen_ts "
            "FROM vm_adapter_observations WHERE host=%s", (host,))
        return rows[0]["seen_ts"] if rows else None

    def finish(self, request, status, result):
        self.db.execute("UPDATE vm_adapter_requests SET status=%s,result=%s,"
                        "updated_at=clock_timestamp() WHERE host=%s AND generation=%s "
                        "AND release_epoch=%s AND state=%s",
                        (status, result, request['host'], request['generation'],
                         request['release_epoch'], request['state']))

    def observe(self, host, state, max_gap_s):
        return self.db.query("""
            INSERT INTO vm_adapter_observations(host,provider_state,on_since,off_since)
            VALUES (%s,%s,CASE WHEN %s='running' THEN clock_timestamp() END,
                          CASE WHEN %s='stopped' THEN clock_timestamp() END)
            ON CONFLICT(host) DO UPDATE SET
              on_since=CASE WHEN excluded.provider_state='running' AND
                  (vm_adapter_observations.provider_state <> 'running' OR
                  vm_adapter_observations.updated_at < clock_timestamp()-make_interval(secs=>%s))
                  THEN clock_timestamp() ELSE vm_adapter_observations.on_since END,
              off_since=CASE WHEN excluded.provider_state='stopped' AND
                  (vm_adapter_observations.provider_state <> 'stopped' OR
                  vm_adapter_observations.updated_at < clock_timestamp()-make_interval(secs=>%s))
                  THEN clock_timestamp() ELSE vm_adapter_observations.off_since END,
              provider_state=excluded.provider_state,updated_at=clock_timestamp()
            RETURNING extract(epoch FROM on_since)::float8 AS on_ts,
                      extract(epoch FROM off_since)::float8 AS off_ts,
                      extract(epoch FROM poweron_at)::float8 AS poweron_ts,wake_delay_s
        """, (host, state, state, state, float(max_gap_s), float(max_gap_s)))[0]

    def started(self, host):
        self.db.execute("UPDATE vm_adapter_observations SET "
                        "poweron_at=coalesce(poweron_at,clock_timestamp()) WHERE host=%s", (host,))

    def healthy(self, host):
        self.db.execute("UPDATE vm_adapter_observations SET wake_delay_s=CASE "
                        "WHEN poweron_at IS NOT NULL THEN greatest(1,extract(epoch FROM "
                        "clock_timestamp()-poweron_at)) ELSE wake_delay_s END,"
                        "poweron_at=NULL WHERE host=%s", (host,))

    def return_intent(self, host):
        return HostLifecycle(self.db).note_return_request(host, actor='vm-adapter')
