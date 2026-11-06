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
        rows = self.db.query('SELECT return_probe FROM host_lifecycle WHERE host=%s FOR UPDATE',
                             (host,))
        control = self.control(host)
        if control and rows:
            control['return_probe'] = rows[0]['return_probe']
        return control

    def return_probe(self, host):
        rows = self.db.query('SELECT return_probe FROM host_lifecycle WHERE host=%s', (host,))
        return rows[0]['return_probe'] if rows else None

    def admit_probe(self, request, probe):
        """#6017: closed return naming the one identity the health turn runs.

        draining -> returning without opening claims: the claim predicate then
        admits `probe` alone (0552). Same generation/epoch, so the queued busy
        request stays current. #6073: the request journals the admission, the
        start of the health deadline."""
        admitted = bool(self.db.query(
            "UPDATE host_lifecycle SET mode='returning',return_probe=%s,"
            "updated_at=clock_timestamp() WHERE host=%s AND generation=%s "
            "AND release_epoch=%s AND mode IN ('draining','returning') "
            "AND NOT claims_open RETURNING host",
            (probe, request['host'], request['generation'], request['release_epoch'])))
        if admitted:
            self.finish(request, 'pending', 'probe admitted')
        return admitted

    def result_since(self, request, result):
        """#6073: epoch at which this pending request entered `result`, or None.

        A phase deadline (`<phase>_deadline_s` in the enrollment) counts from
        the `finish(request, 'pending', <phase marker>)` that opened it; any
        later marker, a completion or a new generation/epoch (a new row)
        ends it. Health uses 'probe admitted'."""
        rows = self.db.query(
            "SELECT extract(epoch FROM updated_at)::float8 AS ts FROM vm_adapter_requests "
            "WHERE host=%s AND generation=%s AND release_epoch=%s AND state=%s "
            "AND status='pending' AND result=%s",
            (request['host'], request['generation'], request['release_epoch'],
             request['state'], result))
        return rows[0]['ts'] if rows else None

    def refuse_probe(self, request):
        """#6017: failed health: back to draining, probe cleared, never lost."""
        refused = bool(self.db.query(
            "UPDATE host_lifecycle SET mode='draining',return_probe=NULL,"
            "updated_at=clock_timestamp() WHERE host=%s AND generation=%s "
            "AND release_epoch=%s AND mode='returning' AND NOT claims_open "
            "AND return_probe IS NOT NULL RETURNING host",
            (request['host'], request['generation'], request['release_epoch'])))
        if refused:
            self.finish(request, 'pending', 'health refused')
        return refused

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

    def touch(self, request):
        """#6074: a provider transition still progressing (archive of a local
        volume): refresh the row without changing its marker."""
        self.db.execute("UPDATE vm_adapter_requests SET updated_at=clock_timestamp() "
                        "WHERE host=%s AND generation=%s AND release_epoch=%s AND state=%s "
                        "AND status='pending'",
                        (request['host'], request['generation'], request['release_epoch'],
                         request['state']))

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

    def provider_state(self, host):
        """#6074: provider state of the last worker tick, or None."""
        rows = self.db.query("SELECT provider_state FROM vm_adapter_observations "
                             "WHERE host=%s", (host,))
        return rows[0]['provider_state'] if rows else None

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
