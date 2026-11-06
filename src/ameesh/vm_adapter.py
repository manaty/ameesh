# SPDX-License-Identifier: AGPL-3.0-only
"""#5925: unprivileged queue hook and separately enrolled operator worker.

Importing this module makes no provider call. Only `worker` reads credentials.
The operator's private enrollment fixes one controller host, exact VM/project
identities and health/reconciliation commands. Runners only call `request`.

#5932: `request` answers 0 only when the worker already completed the state,
`ameesh.host_release.ADAPTER_WAIT_CODE` (75) when the request is queued and
pending, and 1 when the request is refused. A queued request is a wait: the
scanner keeps the previous acknowledged state and closed claims, and the
worker journals the completion under the same generation/epoch.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import urllib.request
import uuid
from zoneinfo import ZoneInfo

from . import config, db as db_mod, election, host_idle, host_lifecycle, host_release, storage
from .storage.postgres.vm_adapter import Queue


class Refused(ValueError):
    pass


def private_json(path):
    """Do not follow links, block on FIFOs or print enrollment/secret bodies."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 65536):
            raise Refused('private operator-owned file required')
        try:
            return json.loads(stream.read(65537))
        except ValueError:
            raise Refused('invalid private JSON') from None


def enrollment(value, *, local_host=None):
    if not isinstance(value, dict) or value.get('controller_host') != (local_host or socket.gethostname()):
        raise Refused('adapter controller host mismatch')
    if value.get('enabled') is not True:
        raise Refused('adapter not enabled by operator')
    for key in ('min_on_s', 'min_off_s', 'io_timeout_s', 'observation_gap_s'):
        n = value.get(key)
        if isinstance(n, bool) or not isinstance(n, (float, int)) or not math.isfinite(n) or n <= 0:
            raise Refused('positive adapter timing required')
    if value['io_timeout_s'] > 10:
        raise Refused('external call timeout exceeds 10 seconds')
    delay = value.setdefault('wake_delay_s', 900)  # study #5900 §7, 05/11/2026
    if isinstance(delay, bool) or not isinstance(delay, (float, int)) or not math.isfinite(delay) or delay <= 0:
        raise Refused('invalid wake delay')
    targets = value.get('targets')
    if not isinstance(targets, dict) or not targets:
        raise Refused('explicit VM allowlist required')
    seen = set()
    for host, target in targets.items():
        if not config.NAME_RE.fullmatch(host) or host == value['controller_host']:
            raise Refused('invalid target or adapter cannot stop itself')
        if not isinstance(target, dict):
            raise Refused('invalid target')
        for key in ('server_id', 'project_id'):
            try:
                if str(uuid.UUID(target[key])) != target[key]:
                    raise ValueError
            except (KeyError, ValueError, TypeError, AttributeError):
                raise Refused('exact VM and project UUIDs required') from None
        if not re.fullmatch(r'[a-z]{2}-[a-z]{3}-[1-9]', target.get('zone', '')):
            raise Refused('invalid target zone')
        # #6017: the one identity the health command runs on a closed return.
        probe = target.get('probe_agent')
        if not isinstance(probe, str) or not config.NAME_RE.fullmatch(probe):
            raise Refused('explicit probe agent required')
        identity = (target['zone'], target['server_id'])
        if identity in seen:
            raise Refused('duplicate VM mapping')
        seen.add(identity)
        for key in ('health_argv', 'reconcile_argv', 'quiesce_argv'):
            argv = target.get(key)
            if (not isinstance(argv, list) or not argv or not all(
                    isinstance(a, str) and a and '\x00' not in a for a in argv)
                    or not Path(argv[0]).is_absolute()):
                raise Refused('fixed absolute operator commands required')
        ZoneInfo(target.get('timezone', 'UTC'))
        for window in target.get('release_windows', []):
            if not isinstance(window, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', window):
                raise Refused('invalid release window')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refused('provider redirect refused')


class Scaleway:
    def __init__(self, token, *, timeout, opener=None):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', token):
            raise Refused('invalid cloud credential')
        self.token, self.timeout = token, timeout
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def _call(self, target, action=None):
        url = ('https://api.scaleway.com/instance/v1/zones/' + target['zone']
               + '/servers/' + target['server_id'])
        body = None if action is None else json.dumps({'action': action}).encode()
        request = urllib.request.Request(url + ('/action' if action else ''), data=body,
                                        headers={'X-Auth-Token': self.token,
                                                 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError
            return json.loads(raw)
        except Exception:
            # No URL, provider body, header, token or exception text is logged.
            raise Refused('provider unavailable; reconcile on next tick') from None

    def state(self, target):
        server = self._call(target).get('server', {})
        if (server.get('id') != target['server_id'] or server.get('project') != target['project_id']):
            raise Refused('provider identity mismatch')
        state = server.get('state')
        if state not in ('running', 'stopped', 'starting', 'stopping', 'stopped in place', 'locked'):
            raise Refused('unknown provider state')
        return state

    def action(self, target, action):
        if action not in ('poweron', 'poweroff'):
            raise Refused('provider action forbidden')
        self._call(target, action)


def run_probe(argv, timeout):
    try:
        return subprocess.run(argv, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def wake_due(host, control, listing, lots, controls, holds, *, now, horizon, target):
    """Only necessary capacity: immediate work, scheduled wake or enrolled release."""
    if host in holds:
        return False
    known = {h for a in listing for h in a.get('admitted_hosts', ())}
    if host_release.return_targets(control, listing, controls, known_hosts=known, holds=holds):
        return True
    available = host_release._open_hosts(controls, known, holds)
    eligible = {a['name'] for a in listing
                if host in a.get('admitted_hosts', ()) and a.get('releve') != 'election'
                and a.get('mode') in (None, 'execute') and a.get('status') != 'stopped'
                and not (set(a.get('admitted_hosts', ())) - {host}) & available}
    if any(lot.get('assignee') in eligible and lot.get('state') not in host_idle.DONE
           and isinstance(lot.get('wake_ts'), (float, int))
           and now <= lot['wake_ts'] <= now + horizon for lot in lots):
        return True
    if not eligible:
        return False
    local = datetime.fromtimestamp(now, ZoneInfo(target.get('timezone', 'UTC')))
    for window in target.get('release_windows', ()):
        hour, minute = map(int, window.split(':'))
        for offset in (0, 1):
            when = (local + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if 0 <= when.timestamp() - now <= horizon:
                return True
    deadline = control.get('return_ts')
    return isinstance(deadline, (float, int)) and deadline <= now + horizon


def stop_evidence(cfg, db, host, control, *, now, quiet_s=None):
    """Revalidate native release proof and elected leader/reserve at effect time."""
    canons, unknown = host_release._canon_read(cfg)
    if unknown or not canons:
        raise Refused('canon unavailable')
    store = storage.of(db)
    reading = store.hosts.latest(host)
    listing = store.operations.listing()
    fp = host_idle.snapshot(cfg, db, canons, {host: reading}, now=now,
                            listing=listing, history_s=quiet_s).get(host)
    if not fp or fp.get('capacity') != 'burst' or fp.get('unknown'):
        raise Refused('idle burst evidence unavailable')
    # Always read elections even when baseline declarations are complete.
    for name in host_lifecycle.critical_names(cfg, db, canons=canons):
        role = election.status(db, name)
        reserve = next((c['host'] for c in role['candidates']
                        if c.get('ready') and c['host'] != role.get('holder')), None)
        if host in (role.get('holder'), reserve):
            raise Refused('elected leader or reserve protected')
    reason = host_release.release_blocker(control, listing, reading,
                                          footprint=fp, cfg=cfg, now=now, adapter_wait=True)
    if reason or fp.get('state') != 'idle':
        raise Refused('host no longer idle')
    return fp


def current(control, request):
    return bool(control and not control['claims_open']
                and control['mode'] in ('draining', 'returning')
                and control['generation'] == request['generation']
                and control['release_epoch'] == request['release_epoch'])


def settle_obsolete(db, host):
    """Settle the queued requests the live control no longer owns (#5932).

    A return can reopen the gate before the worker reads the request it
    superseded. Left pending, that row would keep the stall alert alive
    forever while the VM serves again (auditeur B1). The single UPDATE is
    fenced on the request's own generation/epoch and on the live control:
    a request of the current closed wave is never touched, and a concurrent
    new drain cannot be caught (its request carries the newer epoch)."""
    db.execute(
        "UPDATE vm_adapter_requests v SET status='obsolete',"
        "result='superseded before the worker read it',"
        "updated_at=clock_timestamp() "
        "WHERE v.host=%s AND v.status='pending' AND NOT EXISTS ("
        " SELECT 1 FROM host_lifecycle c WHERE c.host=v.host"
        " AND c.generation=v.generation AND c.release_epoch=v.release_epoch"
        " AND c.mode IN ('draining','returning') AND NOT c.claims_open)",
        (host,))


def tick_host(cfg, db, policy, host, provider, *, probe=run_probe, evidence=stop_evidence):
    """One bounded operator tick; never opens native claims itself."""
    target = policy['targets'][host]
    store = storage.of(db)
    controls = {c['host']: c for c in store.host_lifecycle.listing()}
    control = controls.get(host)
    if not control or control['mode'] not in ('draining', 'returning') or control['claims_open']:
        settle_obsolete(db, host)
        return 'inactive'
    # Operator reconciliation may itself need the native gate. Run probes
    # outside our transaction, then revalidate generation, provider and fresh
    # reading under the gate before acknowledging. No probe opens claims.
    prepared = Queue(db).pending(host)
    health_ok = reconciliation_ok = probed = False
    # #6017: the probe runs only once the closed return admits its identity.
    if (prepared and prepared[0]['state'] == 'busy' and current(control, prepared[0])
            and control['mode'] == 'returning'
            and Queue(db).return_probe(host) == target['probe_agent']
            and not any(h['host'] == host for h in store.hosts.holds())
            and provider.state(target) == 'running'
            and host_release._fresh_reading(cfg, db, host, now=Queue(db).now(),
                                            after=control['requested_ts'])):
        probed = True
        health_ok = probe(target['health_argv'], policy['io_timeout_s'])
        if health_ok:
            reconciliation_ok = probe(target['reconcile_argv'], policy['io_timeout_s'])
    # A transaction-scoped adapter lock makes duplicate worker invocations
    # harmless; the native gate serializes effects with claims and drains.
    with db.transaction() as tx:
        q = Queue(tx)
        if not q.worker_lock(host):
            return 'worker busy'
        control = q.lock(host)
        if not control or control['claims_open'] or control['mode'] not in ('draining', 'returning'):
            # The gate opened while we were waiting for the lock: settle the
            # request this return superseded instead of leaving it pending.
            settle_obsolete(tx, host)
            return 'inactive'
        now = q.now()
        if any(h['host'] == host for h in store.hosts.holds()):
            return 'held'
        state = provider.state(target)
        observed = q.observe(host, state, policy['observation_gap_s'])
        delay = observed['wake_delay_s'] or policy['wake_delay_s']
        listing = store.operations.listing()
        lots = store.operations.open_lots_activity(None)
        holds = {h['host']: h for h in store.hosts.holds()}
        due = wake_due(host, control, listing, lots, controls, holds, now=now,
                       horizon=delay, target=target)
        if due or control.get('return_requested_ts') is not None or control['mode'] == 'returning':
            q.return_intent(host)
            q.request(host, control['generation'], control['release_epoch'], 'busy')
        pending = q.pending(host)
        if not pending:
            return 'no request'
        request = pending[0]
        if not current(control, request):
            q.finish(request, 'obsolete', 'generation changed')
            return 'obsolete'
        verb = request['state']
        if (control.get('hook_attempt_state') == verb
                and (verb != 'idle-drained' or request.get('result') != 'poweroff intent')
                and now - (control.get('hook_attempt_ts') or 0) < host_release._attempt_bound(cfg)):
            return 'native hook in flight'
        if state == 'stopping':
            q.refresh_stop_intent(host)
        if verb == 'draining':
            q.complete(request, 'claims closed; no cloud effect')
            return 'draining'
        if verb == 'idle-drained':
            if (due or control.get('return_requested_ts') is not None
                    or control['mode'] != 'draining' or control['hook_state'] != 'draining'):
                q.finish(request, 'obsolete', 'return supersedes stop')
                return 'return requested'
            if state == 'stopped':
                q.complete(request, 'compute stopped')
                return 'stopped'
            if state != 'running':
                return 'provider transition'
            quiet_s = max(cfg.idle_after_s, policy['min_on_s'], 2 * delay)
            fp = evidence(cfg, tx, host, control, now=now, quiet_s=quiet_s)
            quiet = fp.get('idle_since')
            if (quiet is None or now - quiet < quiet_s
                    or observed['on_ts'] is None or now - observed['on_ts'] < policy['min_on_s']):
                return 'on hysteresis'
            if request.get('result') != 'poweroff intent':
                q.stop_intent(request)
                return 'stop prepared'
            q.refresh_stop_intent(host)
            if not probe(target['quiesce_argv'], policy['io_timeout_s']):
                return 'quiesce pending'
            provider.action(target, 'poweroff')
            # Provider acceptance is not completion. Next tick must see stopped.
            q.finish(request, 'pending', 'poweroff intent')
            return 'stopping'
        if state == 'stopped':
            if observed['off_ts'] is None or now - observed['off_ts'] < policy['min_off_s']:
                return 'off hysteresis'
            q.started(host)
            provider.action(target, 'poweron')
            return 'starting'
        if state != 'running':
            return 'provider transition'
        after = max(observed['on_ts'] or now, observed['poweron_ts'] or 0, control['requested_ts'])
        if not host_release._fresh_reading(cfg, tx, host, now=now, after=after):
            return 'fresh reading pending'
        if control['mode'] != 'returning' or control['return_probe'] != target['probe_agent']:
            # Running and reporting: admit the probe identity alone, claims
            # stay closed; the health turn runs on the next tick (#6017).
            q.admit_probe(request, target['probe_agent'])
            return 'probe admitted'
        if (not prepared or prepared[0]['generation'] != request['generation']
                or prepared[0]['release_epoch'] != request['release_epoch'] or not probed):
            return 'health pending'
        if not health_ok:
            # Back to draining, probe cleared, never lost: the next tick
            # admits it again while the return is still due.
            q.refuse_probe(request)
            return 'health failed'
        if not reconciliation_ok:
            return 'reconciliation pending'
        # Probes cannot substitute for a database reading or reopen claims.
        if not host_release._fresh_reading(cfg, tx, host, now=q.now(), after=after):
            return 'fresh reading pending'
        q.healthy(host)
        q.complete(request, 'healthy and reconciled')
        return 'ready'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    hook = sub.add_parser('request')
    hook.add_argument('state', choices=host_release.STATES)
    hook.add_argument('host')
    worker = sub.add_parser('worker')
    worker.add_argument('--enrollment', required=True)
    args = parser.parse_args(argv)
    db = None
    try:
        cfg = config.load()
        db = db_mod.connect(cfg)
        if args.command == 'request':
            q = Queue(db)
            with db.transaction() as tx:
                q = Queue(tx)
                row = q.lock(args.host)
                generation = int(os.environ.get('AMEESH_HOST_GENERATION', '0'))
                if not row or row['generation'] != generation:
                    return 1
                if q.status(args.host, generation, row['release_epoch'], args.state) == 'done':
                    print('VM adapter: done')
                    return 0
                if (row.get('hook_attempt_generation') != generation
                        or row.get('hook_attempt_epoch') != row['release_epoch']
                        or row.get('hook_attempt_state') != args.state):
                    return 1
                status = q.request(args.host, generation, row['release_epoch'], args.state)
            print('VM adapter: ' + status)
            # #5932: a queued request is a normal asynchronous wait, not an
            # acknowledgement (0) and not a failure (1). The scanner records
            # `hook_wait_*` and the operator worker owns the completion.
            if status == 'done':
                return 0
            if status == 'pending':
                return host_release.ADAPTER_WAIT_CODE
            return 1
        policy = enrollment(private_json(args.enrollment))
        credentials = private_json(Path(os.environ['CREDENTIALS_DIRECTORY']) / 'scaleway.json')
        provider = Scaleway(credentials['secret_key'], timeout=policy['io_timeout_s'])
        for host in policy['targets']:
            try:
                result = tick_host(cfg, db, policy, host, provider)
                print(host + ': ' + result)
            except Exception:
                print(host + ': adapter unavailable; claims remain closed')
        return 0
    except Exception:
        print('VM adapter unavailable; no acknowledgement')
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == '__main__':
    raise SystemExit(main())
