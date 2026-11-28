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

#6073: the health command answers the same three ways. 0 is healthy, 75 is
a probe turn still in flight (the host stays `returning`, same probe), any
other code is a failure. The wait is bounded by `health_deadline_s` counted
from the probe admission; past it the return goes back to `draining`.

#6074: the native stop has two phases under one generation/epoch, each effect
preceded by a durable intent (the request `result`). The OS is halted first
(`halt_argv`, Scaleway power-off-instance.mdx: "Before powering off your
Instance, shut down the OS [...] halt"); an in-guest poweroff leaves the VM
`stopped in place` (use-standby-mode.mdx, still billed). Only that provider
state observed after our halt intent, plus a muted SSH witness
(`halted_argv`), proves the halt; the provider `poweroff` (archive) then runs
only if the server GET lists it in `allowed_actions`. A running VM is never
powered off, whatever SSH says; past `halt_deadline_s` the worker alerts.

#8420: a target enrolled `replica: true` carries a synchronous standby of the
hub's database. Its `standby_enter_argv` (deploy/serveur/replication/
mode-veille.sh entrer) runs after each quiesce of its halt phase, before the
halt intent and again before the halt: no halt while it fails. Its
`standby_exit_argv` (mode-veille.sh sortir) runs once its return is healthy
and every replica target is back; a failure is journalled and alerted
(`vm.standby_exit_failed`), the return itself stands.

#8459: the standby debt is durable on the replica's control (`standby_*`
columns, 0615). The enter intent is written before the external call, the
first failed exit is journalled once and raises
`host_release_standby_exit_failed` through `host_release.alerts`, and every
idle tick replays `sortir` while the debt is recorded: a transient failure no
longer waits for the next replica return or an operator. A successful exit
clears the debt of every replica; `standby_timeout_s` stays strictly under
the tick transaction's 120 s idle timeout.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import selectors
import socket
import stat
import struct
import subprocess
import time
import urllib.request
import uuid
from zoneinfo import ZoneInfo

from . import config, db as db_mod, election, host_idle, host_lifecycle, host_release, storage
from .storage.postgres.vm_adapter import IDLE_IN_TRANSACTION_S, Queue


class Refused(ValueError):
    pass


# #6074: `vm_adapter_requests.result` markers of the idle-drained request, in
# order. Each one is committed before the effect that follows it.
STOP_PREPARED = 'poweroff intent'   # hysteresis passed, native attempt durable
HALT_INTENT = 'halt intent'         # quiesce ok; halt_argv may run next tick
HALT_SENT = 'halt sent'             # halt_argv ran once; its return is no proof
HALT_LATE = 'halt deadline passed'  # OS still up at the deadline: alert only
ARCHIVE_INTENT = 'archive intent'   # halt proven; provider poweroff next tick
ARCHIVE_REFUSED = 'archive refused'  # halt proven, poweroff not allowed: alert
POWEROFF_SENT = 'poweroff sent'     # provider accepted; `stopped` still required
HALTING = (HALT_INTENT, HALT_SENT, HALT_LATE, ARCHIVE_INTENT, ARCHIVE_REFUSED)
PHASES = (STOP_PREPARED, POWEROFF_SENT) + HALTING
# #6080: marker of the busy request. A return found the VM in standby and the
# server GET did not allow `poweron`; committed once, so the row ages from the
# first refusal and host_release names the host past the stall bound.
POWERON_REFUSED = 'poweron refused'
POWERON_SENT = 'poweron sent'
# Leave time for a candidate to start before also requesting its successor.
# Election itself remains the authority that chooses the holder.
ROLE_WAKE_FAILOVER_S = 150
#: #8459: the standby commands run within the tick's transaction, which
#: `Queue.lock` puts under `IDLE_IN_TRANSACTION_S`. The enrolled bound keeps a
#: 30 s margin so a slow `entrer` is never cut mid-call by the database.
STANDBY_TIMEOUT_MAX_S = IDLE_IN_TRANSACTION_S - 30


def private_json(path):
    """Do not follow links, block on FIFOs or print enrollment/secret bodies."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise Refused('private file must be regular')
        if info.st_uid != os.geteuid():
            raise Refused('private file uid must match effective uid')
        if info.st_mode & 0o077:
            raise Refused('private file group or other permission bits forbidden')
        if info.st_nlink != 1:
            raise Refused('private file link count must be one')
        if info.st_size > 65536:
            raise Refused('private file size exceeds limit')
        try:
            return json.loads(stream.read(65537))
        except ValueError:
            raise Refused('invalid private JSON') from None


def credential_json(path):
    """Read only the systemd scaleway credential, including tmpfs ACL copies."""
    directory = Path(os.environ['CREDENTIALS_DIRECTORY'])
    path = Path(path)
    if (not directory.is_absolute() or path != directory / 'scaleway.json'
            or '..' in directory.parts):
        raise Refused('credential must be directly under CREDENTIALS_DIRECTORY')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        uid = os.geteuid()
        if not stat.S_ISREG(info.st_mode):
            raise Refused('credential file must be regular')
        if info.st_uid not in (uid, 0):
            raise Refused('credential file owner must be root or effective uid')
        if info.st_mode & 0o037:
            raise Refused('credential file unsafe group or other permission bits')
        if info.st_nlink != 1:
            raise Refused('credential file link count must be one')
        if info.st_size > 65536:
            raise Refused('credential file size exceeds limit')
        if info.st_uid == uid:
            if info.st_mode & 0o077:
                raise Refused('private file group or other permission bits forbidden')
        else:
            # Linux POSIX ACL v2: only root and this service uid may read.
            # st_mode group bits are the ACL mask, not the owning group's rights.
            try:
                acl = os.getxattr(stream.fileno(), 'system.posix_acl_access')
            except OSError:
                raise Refused('credential ACL required') from None
            expected = {(1, 0xffffffff): 4, (2, uid): 4,
                        (4, 0xffffffff): 0, (16, 0xffffffff): 4,
                        (32, 0xffffffff): 0}
            if len(acl) != 44 or struct.unpack_from('<I', acl)[0] != 2:
                raise Refused('credential ACL must grant read only to effective uid')
            entries = [struct.unpack_from('<HHI', acl, offset)
                       for offset in range(4, len(acl), 8)]
            if {(tag, ident): perm for tag, perm, ident in entries} != expected:
                raise Refused('credential ACL must grant read only to effective uid')
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
    # #8420: the standby commands wait for turns or for a replica to catch up,
    # longer than an I/O call; their own bound is explicit once a replica is
    # enrolled. The operator's argv bounds its waits below it (README).
    # #8459: strictly under the tick transaction's idle timeout, with a margin
    # for the queries that already ran in it (STANDBY_TIMEOUT_MAX_S).
    targets = value.get('targets')
    if isinstance(targets, dict) and any(
            isinstance(t, dict) and t.get('replica') is True for t in targets.values()):
        bound = value.get('standby_timeout_s')
        if (isinstance(bound, bool) or not isinstance(bound, (float, int)) or not math.isfinite(bound)
                or not value['io_timeout_s'] <= bound <= STANDBY_TIMEOUT_MAX_S):
            raise Refused('explicit standby timeout required (at most %d seconds, '
                          'strictly under the %d s idle-in-transaction timeout)'
                          % (STANDBY_TIMEOUT_MAX_S, IDLE_IN_TRANSACTION_S))
    # #6074: explicit bound on the halt phase, counted from the halt intent.
    bound = value.get('halt_deadline_s')
    if (isinstance(bound, bool) or not isinstance(bound, (float, int)) or not math.isfinite(bound)
            or not value['io_timeout_s'] < bound <= 3600):
        raise Refused('explicit halt deadline required (at most 3600 seconds)')
    # #6073: explicit bound on a health turn still in flight (75), counted
    # from the probe admission; a probe turn waits up to 300 s for its mail.
    bound = value.get('health_deadline_s')
    if (isinstance(bound, bool) or not isinstance(bound, (float, int)) or not math.isfinite(bound)
            or not value['io_timeout_s'] < bound <= 3600):
        raise Refused('explicit health deadline required (at most 3600 seconds)')
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
        replica = target.get('replica', False)
        if not isinstance(replica, bool):
            raise Refused('replica must be true or false')
        standby = ('standby_enter_argv', 'standby_exit_argv')
        if not replica and any(key in target for key in standby):
            raise Refused('standby commands require replica: true')
        for key in ('health_argv', 'reconcile_argv', 'quiesce_argv', 'halt_argv', 'halted_argv') + (
                standby if replica else ()):
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

    def _server(self, target):
        server = self._call(target).get('server', {})
        if (server.get('id') != target['server_id'] or server.get('project') != target['project_id']):
            raise Refused('provider identity mismatch')
        return server

    def state(self, target):
        # The SDK enum spells it `stopped_in_place`; one name in the journal.
        state = str(self._server(target).get('state')).replace('_', ' ')
        if state not in ('running', 'stopped', 'starting', 'stopping', 'stopped in place', 'locked'):
            raise Refused('unknown provider state')
        return state

    def allowed(self, target):
        """#6074: `allowed_actions` of the server GET (ServerAction enum), or an
        empty set when absent: no action is then deduced as allowed."""
        actions = self._server(target).get('allowed_actions')
        return set(actions) if isinstance(actions, list) else set()

    def action(self, target, action):
        if action not in ('poweron', 'poweroff'):
            raise Refused('provider action forbidden')
        self._call(target, action)


#: #8083: the only fields and values of the health line, the same table as
#: `REPORT` in deploy/vm-adapter/commandes/vm-commande.py (`Commande.said`);
#: a test keeps both equal. `step` is the only number: the rank (1-99) of the
#: first remote health command that did not answer 0.
HEALTH_REPORT = {
    'stage': ('control', 'admission', 'launched', 'receipt', 'checks',
              'previous-boot', 'done'),
    'answer': ('ok', 'transport', 'failed'),
    'verdict': ('ok', 'ko', 'pending'),
    'delivered': ('true', 'false', 'absent'),
    'acknowledged': ('true', 'false', 'absent'),
    'turn_ended': ('true', 'false', 'absent'),
    'checks': ('ok', 'transport', 'failed'),
    'previous_boot': ('ok', 'transport', 'failed'),
}
HEALTH_HOST = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')
HEALTH_NONCE = re.compile(r'[0-9a-f]{8}\Z')
HEALTH_STEP = re.compile(r'[1-9][0-9]?\Z')
#: #8083: `run_probe` reads at most this many bytes of the health command's
#: stdout; one byte past the accepted line proves it is over the bound.
HEALTH_OUTPUT_MAX = 4096


def health_line(out):
    """#8072/#8083: the validated health line of a probe's stdout, or None.

    Only the fixed fields of `HEALTH_REPORT` are accepted, each at most once,
    with `stage` required: never a free `key=value` (#8083). Every other
    output, including one over 4096 bytes, is dropped."""
    if not isinstance(out, bytes) or len(out) > HEALTH_OUTPUT_MAX:
        return None
    lines = out.decode('ascii', 'replace').splitlines()
    line = lines[-1].strip() if lines else ''
    parts = line.split()
    if len(parts) < 3 or parts[0] != 'health':
        return None
    fields = {}
    for part in parts[1:]:
        key, sep, value = part.partition('=')
        if not sep or not key or key in fields:
            return None
        fields[key] = value
    if not HEALTH_HOST.fullmatch(fields.pop('host', '')):
        return None
    nonce = fields.pop('nonce', None)
    if nonce is not None and not HEALTH_NONCE.fullmatch(nonce):
        return None
    if 'stage' not in fields:
        return None
    for key, value in fields.items():
        if key == 'step':
            if not HEALTH_STEP.fullmatch(value):
                return None
        elif value not in HEALTH_REPORT.get(key, ()):
            return None
    return line


def _read_bounded(stream, limit, deadline):
    """At most `limit` bytes of `stream`, before `deadline` (monotonic).

    #8083: the bound is on the read itself, not on the accepted line: a
    failing local command must not inflate the worker's memory. Reaching the
    bound stops the read; the caller closes the pipe and reaps the child."""
    chunks, total = [], 0
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while total < limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError('probe stdout read timed out')
            chunk = os.read(stream.fileno(), limit - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    return b''.join(chunks)


def _probe_bounded(argv, timeout):
    """(code, stdout) of one health probe; stdout is read at most 4097 bytes.

    #8083: past that bound the pipe is closed (the writer gets EPIPE) and the
    child is reaped within the same deadline, killed if it outlives it."""
    proc = subprocess.Popen(argv, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    raw = b''
    try:
        raw = _read_bounded(proc.stdout, HEALTH_OUTPUT_MAX + 1, deadline)
        proc.stdout.close()
        proc.stdout = None
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
            proc.stdout = None
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return proc.returncode, raw


def run_probe(argv, timeout, *, pending=False):
    """True on 0; with `pending`, 'pending' on 75 (#6073); False otherwise.

    #8072: for health (`pending`), the command's bounded line (stage reached,
    receipt verdict and booleans, failing check) is journalled, so a return
    that does not complete reads from the worker log alone.
    #8083: that stdout is read at most 4097 bytes; the other verbs keep it on
    /dev/null."""
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
    try:
        if pending:
            code, raw = _probe_bounded(argv, timeout)
        else:
            done = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=timeout)
            code, raw = done.returncode, None
    except (OSError, subprocess.SubprocessError):
        if pending:
            print('health probe: not run or not ended within its bound', flush=True)
        return False
    if pending:
        line = health_line(raw)
        print('%s (exit %s)' % (line or 'health probe: no valid line', code), flush=True)
    if pending and code == host_release.ADAPTER_WAIT_CODE:
        return 'pending'
    return code == 0


def wake_due(host, control, listing, lots, controls, holds, *, now, horizon, target,
             ci=None, startable=()):
    """Only necessary capacity: immediate work, scheduled wake, CI queue,
    enrolled release, or a startable unassigned lot (#8419)."""
    if host in holds:
        return False
    # #8031: a job the measured CI queue holds on labels this host's runners
    # serve is work for a stopped CI VM. `ci` is the evidence read by
    # `host_release.ci_wake_evidence`; `unknown` never wakes (it only forbids a
    # stop, see `stop_evidence`).
    if ci and ci.get('state') == 'waiting':
        return True
    # #7769: `admitted_hosts` is declarative and nullable (0029). A row outside
    # `canon sync` carries NULL, which `get` returns as-is: NULL means no
    # declared admission, as every other reader already treats it.
    known = {h for a in listing for h in (a.get('admitted_hosts') or ())}
    if host_release.return_targets(control, listing, controls, known_hosts=known,
                                   holds=holds, startable=startable):
        return True
    available = host_release._open_hosts(controls, known, holds)
    eligible = {a['name'] for a in listing
                if host in (a.get('admitted_hosts') or ()) and a.get('releve') not in ('election', 'preference')
                and a.get('mode') in (None, 'execute') and a.get('status') != 'stopped'
                and not (set(a.get('admitted_hosts') or ()) - {host}) & available}
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


def elected_wake_due(role, lots, *, now, horizon):
    """A role needs its executor for active mail or a scheduled lot."""
    if (role.get('retired_ts') is not None
            or role.get('releve') not in ('election', 'preference')
            or role.get('lease_live') or role.get('status') == 'stopped'
            or role.get('mode') not in (None, 'execute')):
        return False
    scheduled = any(lot.get('assignee') == role.get('name')
                    and lot.get('state') not in host_idle.DONE
                    and isinstance(lot.get('wake_ts'), (int, float))
                    and lot['wake_ts'] <= now + horizon for lot in lots)
    return bool(role.get('vm_wake_mail_id') or role.get('has_pending_prompt') or scheduled)


def elected_wake_target(role, candidates, lots, targets, holds, *, now, horizon,
                        stalled=(), open_hosts=()):
    """Choose one enrolled candidate for pending work on a lease-free role.

    A sleeping candidate has no fresh standing; its declared placement, not
    election readiness, is the evidence available before the machine starts.
    #8121: a declared, admitted and ready candidate already standing on an
    open host (the hub is never an adapter target) will take the role by
    election; waking a drained VM for it would pay for capacity the mesh
    already has.
    """
    if not elected_wake_due(role, lots, now=now, horizon=horizon):
        return None
    if any(c.get('declared') and c.get('admitted') and c.get('ready')
           and c.get('host') in open_hosts for c in candidates):
        return None
    admitted = set(role.get('admitted_hosts') or ())
    eligible = [c for c in candidates if c.get('declared') and c.get('admitted')
                and c.get('host') in admitted and c.get('host') in targets
                and c.get('host') not in holds and c.get('host') not in stalled]
    eligible.sort(key=lambda c: (c.get('preference_rank') is None,
                                 c.get('preference_rank') or 0, c['host']))
    return eligible[0]['host'] if eligible else None


def stalled_role_wakes(queue, targets, controls, *, now):
    """A bounded, still-pending return cannot indefinitely bar the reserve."""
    stalled = set()
    for candidate in targets:
        for request in queue.pending_all(candidate):
            created = request.get('created_ts')
            if (request.get('state') == 'busy' and current(controls.get(candidate), request)
                    and isinstance(created, (int, float))
                    and now - created >= ROLE_WAKE_FAILOVER_S):
                stalled.add(candidate)
    return stalled


def stop_evidence(cfg, db, host, control, *, now, quiet_s=None):
    """Revalidate native release proof and elected leader/reserve at effect time."""
    canons, unknown = host_release._canon_read(cfg)
    if unknown:
        raise Refused('canon unavailable')
    if not canons:
        # #8126: an absent `canons` key is a configuration to write, not a
        # canon to synchronize; the operator must see which one is missing.
        raise Refused('no canon configured')
    store = storage.of(db)
    reading = store.hosts.latest(host)
    listing = store.operations.listing()
    # Match the return decision for a dated manual drain (#8129).  Only the
    # idle projection ignores ordinary active and passive mail; keep the original listing
    # for the remaining safety checks. Missing urgent evidence stays unknown.
    idle_listing = listing
    if listing is not None and host_release._dated_manual_drain(control):
        idle_listing = [dict(row, unread=(None if row.get('unread') is None
                                          or row.get('urgent_unread') is None
                                          else bool(row['urgent_unread']
                                                    or row.get('restart_requested_ts'))),
                             passive_unread=0)
                        if row.get('host') == host else row for row in listing]
    fp = host_idle.snapshot(cfg, db, canons, {host: reading}, now=now,
                            listing=idle_listing, history_s=quiet_s).get(host)
    if not fp or fp.get('capacity') != 'burst' or fp.get('unknown'):
        raise Refused('idle burst evidence unavailable')
    # Always read elections even when baseline declarations are complete.
    for name in host_lifecycle.critical_names(cfg, db, canons=canons):
        role = election.status(db, name)
        reserve = next((c['host'] for c in role['candidates']
                        if c.get('ready') and c['host'] != role.get('holder')), None)
        if host in (role.get('holder'), reserve):
            raise Refused('elected leader or reserve protected')
    # #8031: a CI host is stopped only on a readable, fresh queue. An
    # unreadable reading is doubt: it never wakes, but it forbids the halt
    # (`release_blocker` names it), because a job could be waiting on labels
    # this VM's runners serve. A named lease or turn is reported first.
    ci = host_release.ci_wake_evidence(db, host, now=now,
                                       released_ts=control.get('released_ts'))
    reason = host_release.release_blocker(control, listing, reading,
                                          footprint=fp, cfg=cfg, now=now,
                                          adapter_wait=True, ci=ci)
    if reason:
        # #7799: the R1 entry guard. `release_blocker` names the placed agents
        # whose lease is still live ("a lease is still held: <agents>"), so the
        # adapter refuses the halt at effect time and the journal says who
        # keeps the host up instead of an anonymous "no longer idle".
        raise Refused('host no longer idle: ' + reason)
    if fp.get('state') != 'idle':
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


def prepare_elected_wake(db, policy, host, listing, lots, controls, holds):
    """Persist a role return before contacting the provider, even if it fails."""
    if host in holds:
        return
    with db.transaction() as tx:
        q = Queue(tx)
        if not q.worker_lock(host):
            return
        control = q.lock(host)
        if not control or control['claims_open'] or control['mode'] not in ('draining', 'returning'):
            return
        now = q.now()
        # #8121: same open-host evidence as wake_due. A candidate ready on a
        # host whose claims are open (the hub, never an adapter target) takes
        # the role by election; no drained VM is woken for it.
        known = {h for row in listing for h in (row.get('admitted_hosts') or ())}
        open_hosts = host_release._open_hosts(controls, known, holds)
        stalled = None
        for role in listing:
            if not elected_wake_due(role, lots, now=now, horizon=policy['wake_delay_s']):
                continue
            status = election.status(tx, role['name'])
            if status.get('holder'):
                continue
            if stalled is None:
                stalled = stalled_role_wakes(q, policy['targets'], controls, now=now)
            if elected_wake_target(role, status['candidates'], lots, policy['targets'], holds,
                                   now=now, horizon=policy['wake_delay_s'], stalled=stalled,
                                   open_hosts=open_hosts) == host:
                q.return_intent(host)
                q.request(host, control['generation'], control['release_epoch'], 'busy')
                scheduled = next((lot for lot in lots if lot.get('assignee') == role['name']
                                  and lot.get('state') not in host_idle.DONE
                                  and isinstance(lot.get('wake_ts'), (int, float))
                                  and lot['wake_ts'] <= now + policy['wake_delay_s']), None)
                cause = ({'mail_id': role['vm_wake_mail_id']} if role.get('vm_wake_mail_id')
                         else {'prompt': True} if role.get('has_pending_prompt')
                         else {'lot_id': scheduled.get('id')} if scheduled else {})
                tx.execute("INSERT INTO ameesh_events(type,object,project,data) "
                           "VALUES ('vm.elected_wake',%s,%s,%s::jsonb)",
                           (host, role.get('project'), json.dumps(
                               dict(role=role['name'], generation=control['generation'],
                                    release_epoch=control['release_epoch'], **cause))))
                return


def tick_host(cfg, db, policy, host, provider, *, probe=run_probe, evidence=stop_evidence):
    """One bounded operator tick; never opens native claims itself.

    #8382: the successful provider read of a tick stays durable even when the
    tick aborts (an idle guard refusal is the normal case). Its transaction
    rolls back, so `_tick_host` hands the state back through `tick_state` and
    it is replayed in its own transaction before the failure leaves. Only a
    real observation gap (unreadable provider, stopped worker) then breaks the
    90 s continuity and resets `on_since`.
    """
    tick_state = []
    try:
        result = _tick_host(cfg, db, policy, host, provider, probe=probe,
                            evidence=evidence, tick_state=tick_state)
    except Exception:
        if tick_state:
            try:
                # Same adapter lock as the tick: a duplicate worker in flight
                # keeps its own observation rather than being overwritten by
                # this older read.
                with db.transaction() as tx:
                    q = Queue(tx)
                    if q.worker_lock(host):
                        q.observe(host, tick_state[0], policy['observation_gap_s'])
            except Exception:
                # Never replace the tick's own failure: the next tick (or the
                # worker stall alert) carries the database side.
                pass
        raise
    if policy['targets'][host].get('replica'):
        if result == 'ready':
            # After the commit: the return stands whatever the exit answers.
            return standby_exit(db, policy, host, probe)
        # #8459: an exit owed by an earlier return is replayed at every tick,
        # even once the control left the release (`inactive`): the hub's
        # synchronous list stays empty until `sortir` succeeds. Never during a
        # release: `entrer` owns the standby state then.
        control = Queue(db).control(host) or {}
        if (result == 'inactive'
                and control.get('mode') not in ('draining', 'returning')
                and (control.get('standby_entered_ts') is not None
                     or control.get('standby_exit_failed_ts') is not None)):
            return standby_exit(db, policy, host, probe, prefix='inactive')
    return result


def replicas_back(db, policy):
    """#8420: every replica target is back: no release, or its return done.

    Its `busy` request done for the live generation/epoch is the healthy
    return (`healthy and reconciled`), claims still closed or already
    reopened by the native hook (the mode stays `returning` until
    `complete_return`)."""
    controls = {c['host']: c for c in storage.of(db).host_lifecycle.listing()}
    q = Queue(db)
    for name, target in policy['targets'].items():
        if not target.get('replica'):
            continue
        control = controls.get(name)
        if not control or (control['claims_open']
                           and control['mode'] not in ('draining', 'returning')):
            continue
        if (control['mode'] == 'returning'
                and q.status(name, control['generation'], control['release_epoch'],
                             'busy') == 'done'):
            continue
        return False
    return True


def standby_exit(db, policy, host, probe, *, prefix='ready'):
    """#8420/#8459: `mode-veille.sh sortir` once every replica is back, else wait.

    Idempotent on the hub (no standby mode: nothing to restore), so each
    completed replica return calls it, and #8459 replays it at every idle tick
    while the debt is recorded. A failure never undoes the return: it is
    journalled once per episode and alerted
    (`host_release_standby_exit_failed`), and the list stays empty (safe,
    degraded durability) until a later tick or an operator restores it. On
    success every replica's debt is cleared: the standby list belongs to the
    hub, not to one VM."""
    if not replicas_back(db, policy):
        return prefix + '; standby exit waits for the other replica'
    replicas = [name for name, target in policy['targets'].items()
                if target.get('replica')]
    if probe(policy['targets'][host]['standby_exit_argv'], policy['standby_timeout_s']):
        with db.transaction() as tx:
            Queue(tx).note_standby_exit_ok(replicas)
        return prefix + '; standby exited'
    with db.transaction() as tx:
        q = Queue(tx)
        controls = {name: q.control(name) for name in replicas}
        # One event per episode: a replay every 30 s must not fill the journal.
        first = not any(control and control.get('standby_exit_failed_ts') is not None
                        for control in controls.values())
        control = controls.get(host) or {}
        if first:
            tx.execute("INSERT INTO ameesh_events(type,object,project,data) "
                       "VALUES ('vm.standby_exit_failed',%s,NULL,%s::jsonb)",
                       (host, json.dumps(dict(generation=control.get('generation'),
                                              release_epoch=control.get('release_epoch')))))
        q.note_standby_exit_failed(host, replicas)
    return prefix + '; standby exit failed (synchronous list left empty)'


def _tick_host(cfg, db, policy, host, provider, *, probe, evidence, tick_state):
    target = policy['targets'][host]
    store = storage.of(db)
    controls = {c['host']: c for c in store.host_lifecycle.listing()}
    control = controls.get(host)
    if not control or control['mode'] not in ('draining', 'returning') or control['claims_open']:
        settle_obsolete(db, host)
        return 'inactive'
    prepare_elected_wake(db, policy, host, store.operations.listing(),
                         store.operations.open_lots_activity(None), controls,
                         {h['host']: h for h in store.hosts.holds()})
    # #8419: the unassigned startable lots are a wake cause; read them before
    # the lock (the native gate holds no work view open) and once for both
    # `wake_due` calls. Unreadable evidence is no wake (the next tick decides).
    try:
        startable = host_release.startable_unassigned_lots(
            db, store.operations.open_lots_activity(None), now=time.time())
    except Exception:  # noqa: BLE001 - a wake is never worth a failed tick
        startable = ()
    # Operator reconciliation may itself need the native gate. Run probes
    # outside our transaction, then revalidate generation, provider and fresh
    # reading under the gate before acknowledging. No probe opens claims.
    prepared = Queue(db).pending(host)
    health = None
    reconciliation_ok = probed = False
    # #6017: the probe runs only once the closed return admits its identity.
    if (prepared and prepared[0]['state'] == 'busy' and current(control, prepared[0])
            and control['mode'] == 'returning'
            and Queue(db).return_probe(host) == target['probe_agent']
            and not any(h['host'] == host for h in store.hosts.holds())
            and provider.state(target) == 'running'
            and host_release._fresh_reading(cfg, db, host, now=Queue(db).now(),
                                            after=control['requested_ts'])):
        probed = True
        health = probe(target['health_argv'], policy['io_timeout_s'], pending=True)
        if health is True:
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
        tick_state.append(state)
        observed = q.observe(host, state, policy['observation_gap_s'])
        delay = observed['wake_delay_s'] or policy['wake_delay_s']
        # Assess the first worker tick of each drain, not the dwindling time
        # on every subsequent tick. The draining request is the durable marker.
        drain_status = q.status(host, control['generation'], control['release_epoch'], 'draining')
        if (control['mode'] == 'draining' and control.get('return_ts') is not None
                and drain_status is None):
            q.request(host, control['generation'], control['release_epoch'], 'draining')
            required = max(cfg.idle_after_s, policy['min_on_s'], 2 * delay) + delay
            deadline = control.get('return_ts')
            result = ('drain too short for halt: needs %d s' % math.ceil(required)
                      if deadline is not None and deadline - now < required
                      else 'drain timing checked')
            # This state has no cloud effect. The native hook acknowledges it
            # when it next observes the completed request.
            row = next(row for row in q.pending_all(host)
                       if row['state'] == 'draining' and current(control, row))
            q.finish(row, 'done', result)
        elif (control['mode'] == 'draining' and control.get('return_ts') is not None
              and drain_status == 'pending'):
            row = next((row for row in q.pending_all(host)
                        if row['state'] == 'draining' and current(control, row)), None)
            if row is not None and not row.get('result'):
                required = max(cfg.idle_after_s, policy['min_on_s'], 2 * delay) + delay
                deadline = control.get('return_ts')
                if deadline is not None and deadline - now < required:
                    q.finish(row, 'pending', 'drain too short for halt: needs %d s' % math.ceil(required))
        listing = store.operations.listing()
        lots = store.operations.open_lots_activity(None)
        holds = {h['host']: h for h in store.hosts.holds()}
        # #8031: the same measured queue the scanner reads decides the wake of
        # a stopped CI VM; `unknown` is no wake (the halt refuses it later).
        ci = host_release.ci_wake_evidence(tx, host, now=now,
                                           released_ts=control.get('released_ts'))
        due = wake_due(host, control, listing, lots, controls, holds, now=now,
                       horizon=delay, target=target, ci=ci, startable=startable)
        if due or control.get('return_requested_ts') is not None or control['mode'] == 'returning':
            deadline = control.get('return_ts')
            deadline_only = (due and isinstance(deadline, (float, int))
                             and deadline <= now + delay
                             and not wake_due(host, dict(control, return_ts=None), listing,
                                              lots, controls, holds, now=now,
                                              horizon=delay, target=target, ci=ci,
                                              startable=startable))
            if deadline_only:
                for row in q.pending_all(host):
                    if row['state'] == 'idle-drained' and current(control, row):
                        q.finish(row, 'obsolete', 'return deadline within wake horizon')
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
                and (verb != 'idle-drained' or request.get('result') not in PHASES)
                and now - (control.get('hook_attempt_ts') or 0) < host_release._attempt_bound(cfg)):
            return 'native hook in flight'
        if state == 'stopping':
            q.refresh_stop_intent(host)
        if state in ('stopping', 'starting'):
            # #6074: archiving a local volume takes long; progress is not
            # a stall (host_release_adapter_stalled ages these rows). #6079:
            # a return queues a busy request ahead of the archiving row, which
            # is refreshed too while the provider transits.
            for row in q.pending_all(host):
                if current(control, row) and row.get('result') in (ARCHIVE_INTENT, POWEROFF_SENT):
                    q.touch(row)
        if verb == 'draining':
            result = request.get('result') or 'claims closed; no cloud effect'
            q.complete(request, result)
            return 'draining'
        if verb == 'idle-drained':
            if (due or control.get('return_requested_ts') is not None
                    or control['mode'] != 'draining' or control['hook_state'] != 'draining'):
                q.finish(request, 'obsolete', 'return supersedes stop')
                return 'return requested'
            if state == 'stopped':
                q.complete(request, 'compute stopped')
                return 'stopped'
            result = request.get('result')
            if result in HALTING:
                q.refresh_stop_intent(host)
            if result in (HALT_INTENT, HALT_SENT):
                since = q.result_since(request, result)
                if since is not None and now - since > policy['halt_deadline_s']:
                    # The full proof (provider state AND muted witness) is
                    # still missing: no poweroff, never lost; host_release
                    # names the host. A later complete proof stays valid.
                    q.finish(request, 'pending', HALT_LATE)
                    result = HALT_LATE
                    request = dict(request, result=HALT_LATE)
                    if state != 'stopped in place':
                        return 'halt deadline passed'
            if result in HALTING and not (result == HALT_INTENT and state == 'running'):
                return halted(q, request, target, policy, provider, state, probe)
            if result == POWEROFF_SENT and state != 'running':
                return 'provider transition'
            if state == 'stopped in place':
                # Not after our halt intent: anomaly alerted by host_release
                # (still billed), never archived automatically.
                return 'stopped in place without halt'
            if state != 'running':
                return 'provider transition'
            quiet_s = max(cfg.idle_after_s, policy['min_on_s'], 2 * delay)
            try:
                fp = evidence(cfg, tx, host, control, now=now, quiet_s=quiet_s)
            except Refused as exc:
                # #8126: each canon refusal keeps its own durable marker, so
                # the alert names the right cause; any other refusal still
                # escapes this tick.
                refusal = str(exc)
                if refusal not in host_release.ADAPTER_CANON:
                    raise
                if request.get('result') != refusal:
                    q.finish(request, 'pending', refusal)
                return refusal
            if request.get('result') in host_release.ADAPTER_CANON:
                q.finish(request, 'pending', 'canon available')
            quiet = fp.get('idle_since')
            if (quiet is None or now - quiet < quiet_s
                    or observed['on_ts'] is None or now - observed['on_ts'] < policy['min_on_s']):
                return 'on hysteresis'
            if result not in (STOP_PREPARED, HALT_INTENT):
                q.stop_intent(request)
                # #8459 B1 (cx2): the standby debt is committed with the stop
                # intent, a tick before `entrer`; a worker killed while the
                # probe runs can no longer roll it back. Conservative: if the
                # host never enters standby, the replay of `sortir` is a no-op.
                if target.get('replica'):
                    q.note_standby_enter(host)
                return 'stop prepared'
            q.refresh_stop_intent(host)
            if not probe(target['quiesce_argv'], policy['io_timeout_s']):
                return 'quiesce pending'
            # #8420: fail closed. No halt intent, and no halt, while the hub
            # cannot enter standby mode (idempotent: a replay verifies it).
            # #8459 B1 (cx2): the debt was committed with the stop intent (or
            # by this branch for a request prepared before this version); a
            # failed or killed `entrer` cannot erase it.
            if target.get('replica'):
                if control.get('standby_entered_ts') is None:
                    q.note_standby_enter(host)
                    return 'standby enter prepared'
                if not probe(target['standby_enter_argv'], policy['standby_timeout_s']):
                    return 'standby enter refused'
            if result == STOP_PREPARED:
                q.finish(request, 'pending', HALT_INTENT)
                return 'halt prepared'
            # The halt intent was committed by an earlier tick and the idle,
            # election and quiesce proofs hold again now. The command cannot
            # attest its own end: only the provider state proves the halt.
            probe(target['halt_argv'], policy['io_timeout_s'])
            q.finish(request, 'pending', HALT_SENT)
            return 'halt sent'
        if state == 'stopped in place':
            # #6074: a return between halt and archive. Standby keeps the
            # local volume (use-standby-mode.mdx): power on when the server
            # GET allows it; claims still wait for running, health, reconcile.
            if 'poweron' not in provider.allowed(target):
                if request.get('result') != POWERON_REFUSED:
                    q.finish(request, 'pending', POWERON_REFUSED)
                return 'poweron not allowed'
            q.started(host)
            provider.action(target, 'poweron')
            if request.get('result') == POWERON_REFUSED:
                q.finish(request, 'pending', POWERON_SENT)
            return 'starting'
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
        if health == 'pending':
            # #6073: the probe turn is still running. Same closed return,
            # same probe, no refusal, until the enrolled deadline passes.
            admitted = q.result_since(request, 'probe admitted')
            if admitted is not None and now - admitted <= policy['health_deadline_s']:
                return 'health pending'
        if health is not True:
            # Back to draining, probe cleared, never lost: the next tick
            # admits it again while the return is still due.
            q.refuse_probe(request)
            return 'health deadline passed' if health == 'pending' else 'health failed'
        if not reconciliation_ok:
            return 'reconciliation pending'
        # Probes cannot substitute for a database reading or reopen claims.
        if not host_release._fresh_reading(cfg, tx, host, now=q.now(), after=after):
            return 'fresh reading pending'
        q.healthy(host)
        q.complete(request, 'healthy and reconciled')
        return 'ready'


def halted(q, request, target, policy, provider, state, probe):
    """#6074: second phase, after the committed halt intent of this request.

    `request` is current (same generation/epoch as the control), so a
    `stopped in place` seen here comes after our own halt intent. Proof =
    that state AND a muted operator SSH witness; then the archive intent is
    committed, and the provider poweroff runs on the next tick only if the
    server GET still allows it. Running is never powered off."""
    result = request['result']
    if state != 'stopped in place':
        return 'halt deadline passed' if result == HALT_LATE else 'halt pending'
    if result == ARCHIVE_INTENT:
        if 'poweroff' not in provider.allowed(target):
            q.finish(request, 'pending', ARCHIVE_REFUSED)
            return 'archive not allowed'
        provider.action(target, 'poweroff')
        # Provider acceptance is not completion. Next tick must see stopped.
        q.finish(request, 'pending', POWEROFF_SENT)
        return 'archiving'
    if not probe(target['halted_argv'], policy['io_timeout_s']):
        return 'halt deadline passed' if result == HALT_LATE else 'halt witness pending'
    if 'poweroff' not in provider.allowed(target):
        if result != ARCHIVE_REFUSED:
            q.finish(request, 'pending', ARCHIVE_REFUSED)
        return 'archive not allowed'
    q.finish(request, 'pending', ARCHIVE_INTENT)
    return 'halt proven'


def failure_cause(error):
    """Keep exception values, filenames, URLs and credentials out of logs."""
    if isinstance(error, Refused):
        # Fixed literals, except #7799's bounded list of placed agent names
        # added by `release_blocker` (registry names, no secret).
        return str(error)
    cause = type(error).__name__
    if isinstance(error, KeyError) and len(error.args) == 1:
        key = error.args[0]
        # KeyError accepts arbitrary payloads; only known schema keys are safe.
        if isinstance(key, str) and key in {
                'CREDENTIALS_DIRECTORY', 'secret_key', 'io_timeout_s', 'targets',
                'zone', 'server_id', 'project_id', 'generation', 'release_epoch',
                'state', 'result', 'host', 'timezone', 'probe_agent'}:
            cause += ' (missing key: ' + key + ')'
    return cause


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
    stage = 'config'
    try:
        cfg = config.load()
        stage = 'base'
        db = db_mod.connect(cfg)
        if args.command == 'request':
            stage = 'request'
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
        stage = 'enrollment'
        policy = enrollment(private_json(args.enrollment))
        stage = 'credentials'
        credentials = credential_json(Path(os.environ['CREDENTIALS_DIRECTORY']) / 'scaleway.json')
        stage = 'provider'
        provider = Scaleway(credentials['secret_key'], timeout=policy['io_timeout_s'])
        for host in policy['targets']:
            try:
                result = tick_host(cfg, db, policy, host, provider)
                print(host + ': ' + result)
            except Exception as error:
                print(host + ': adapter unavailable at tick: ' + failure_cause(error)
                      + '; claims remain closed')
        return 0
    except Exception as error:
        print('VM adapter unavailable at ' + stage + ': ' + failure_cause(error)
              + '; no acknowledgement')
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == '__main__':
    raise SystemExit(main())
