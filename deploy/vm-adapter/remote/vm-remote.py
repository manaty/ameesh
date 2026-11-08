#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#6276: non-root, forced-command VM endpoint. No shell or caller paths.

Installed invocation: <private-venv>/bin/python -I <this-file> dispatch.
Only SSH_ORIGINAL_COMMAND is parsed; it is never executed. All commands are
selected from an exact allowlist. The enrollment is installed by the bureau,
not supplied over SSH. No cloud credential, privilege escalation for Python,
claim reopening or lost-host recovery belongs here.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys

CONFIG = '/etc/ameesh-vm-adapter/remote.json'
PREFIX = '/opt/ameesh-vm-adapter/remote/'
WAIT = 75
LIMIT = 65536
ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')
NONCE = re.compile(r'[0-9a-f]{32}\Z')
HALT = ['/usr/bin/sudo', '-n', '/usr/bin/systemctl', 'poweroff', '--no-block']
JOURNAL = ['/usr/bin/journalctl', '-b', '-1', '-n', '1', '-o', 'cat', '--no-pager']
VERBS = ('probe-start', 'probe-receipt', 'reconcile', 'quiesce', 'runners', 'mail',
         'version-mode', 'doctor')


class Refus(ValueError):
    pass


def private_json(path, *, root=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != (0 if root else os.geteuid())
                or info.st_mode & (0o022 if root else 0o077)
                or info.st_size > LIMIT):
            raise Refus('unsafe private document')
        return json.load(stream)


def absolute(value):
    if (not isinstance(value, str) or not value.startswith('/')
            or '..' in Path(value).parts or '\x00' in value):
        raise Refus('absolute enrolled path required')
    return value


def enrollment(value):
    if (not isinstance(value, dict) or set(value) - {'health'} != {
            'host', 'probe_agent', 'ameesh_config', 'state_dir', 'fresh_seconds',
            'reconcile_deadline_s', 'checks'}):
        raise Refus('invalid remote enrollment')
    for key in ('host', 'probe_agent'):
        if not isinstance(value[key], str) or not NAME.fullmatch(value[key]):
            raise Refus('invalid identity')
    for key in ('ameesh_config', 'state_dir'):
        absolute(value[key])
    age = value['fresh_seconds']
    if type(age) is not int or not 1 <= age <= 300:
        raise Refus('invalid freshness bound')
    deadline = value['reconcile_deadline_s']
    if type(deadline) is not int or not 1 <= deadline <= 3600:
        raise Refus('invalid reconciliation deadline')
    if not isinstance(value['checks'], dict) or set(value['checks']) != {'doctor', 'version-mode'}:
        raise Refus('fixed health checks required')
    for argv in value['checks'].values():
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(a, str) or not a or '\x00' in a for a in argv)):
            raise Refus('fixed health argv required')
        absolute(argv[0])
    return dict(value)


def dispatch(text):
    """Parse the one forced-key language; argv is validated, never evaluated."""
    if not isinstance(text, str) or len(text) > 2048 or '\n' in text or '\x00' in text:
        raise Refus('invalid SSH command')
    try:
        args = shlex.split(text)
    except ValueError:
        raise Refus('invalid SSH command') from None
    if args in (HALT, JOURNAL, ['/usr/bin/true']):
        return args[0], args[1:]
    if not args or not args[0].startswith(PREFIX):
        raise Refus('command not enrolled')
    verb = args[0][len(PREFIX):]
    tail = args[1:]
    if verb == 'probe-start' and len(tail) == 5:
        host, agent, generation, epoch, nonce = tail
        if (NAME.fullmatch(host) and NAME.fullmatch(agent) and NONCE.fullmatch(nonce)
                and re.fullmatch(r'[1-9][0-9]{0,17}', generation)
                and re.fullmatch(r'[0-9]{1,18}', epoch)):
            return verb, [host, agent, int(generation), int(epoch), nonce]
    elif verb == 'probe-receipt' and len(tail) == 1 and NONCE.fullmatch(tail[0]):
        return verb, tail
    elif verb in VERBS and verb not in ('probe-start', 'probe-receipt') and not tail:
        return verb, []
    raise Refus('command not enrolled')


class Store:
    def __init__(self, path):
        self.path = Path(path)
        info = self.path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise Refus('private state directory required')

    @contextmanager
    def locked(self):
        fd = os.open(self.path / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or info.st_mode & 0o077):
                raise Refus('unsafe lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def load(self, nonce):
        try:
            return private_json(self.path / (nonce + '.json'))
        except FileNotFoundError:
            return None

    def save(self, nonce, value):
        # A single immutable record per nonce. O_EXCL prevents replacement by
        # another request; a truncated crash record causes a closed refusal.
        fd = os.open(self.path / (nonce + '.json'),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class Native:
    """Uses the native mailbox and the runner's durable facts, never a runner
    in the SSH session. The existing executor performs startup/periodic orphan
    settlement; reconcile stays pending until that native work is complete."""

    def __init__(self, db, cfg):
        self.db, self.cfg = db, cfg

    @contextmanager
    def control(self, mode, *, lock=True):
        from ameesh.storage.postgres.hosts import Hosts
        with self.db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout = '4000ms'")
            tx.execute("SET LOCAL lock_timeout = '1000ms'")
            if lock:
                Hosts._lock_gate(tx, self.cfg['host'])
            rows = tx.query('SELECT host, mode, claims_open, generation, release_epoch, '
                            'return_probe FROM host_lifecycle WHERE host=%s' +
                            (' FOR SHARE' if lock else ''),
                            (self.cfg['host'],))
            if (len(rows) != 1 or rows[0]['mode'] != mode or rows[0]['claims_open']
                    or (mode == 'returning' and rows[0]['return_probe'] != self.cfg['probe_agent'])):
                raise Refus('native gate not closed for operation')
            yield tx, rows[0]

    def admission(self, tx, key):
        from ameesh.storage.postgres.vm_adapter import Queue
        return Queue(tx).result_since(dict(key, state='busy'), 'probe admitted')

    def start(self, key):
        from ameesh import mail
        with self.control('returning') as (tx, control):
            if any(key[k] != control[k] for k in ('generation', 'release_epoch')):
                raise Refus('stale return')
            rows = tx.query('SELECT name FROM agent_registry WHERE name=%s AND host=%s '
                            "AND mode='execute' AND retired_at IS NULL AND NOT handoff_fenced",
                            (key['probe_agent'], key['host']))
            if len(rows) != 1:
                raise Refus('probe identity not enrolled here')
            if self.admission(tx, key) is None:
                raise Refus('probe not admitted')
            nonce = key['nonce']
            body = ('VM return health probe. Acknowledge this nonce with '
                    'agent-mail send ' + key['probe_agent'] + ' --ack "VM-PROBE-ACK ' + nonce +
                    '" then end this turn. No other work is requested.')
            request_id = mail.send(tx, 'vm-adapter', key['probe_agent'], body,
                                   host=key['host'], kind='request', thread=False,
                                   payload={'vm_probe': key}, idempotency_key='vm-probe-' + nonce)
            created = tx.query('SELECT extract(epoch from created_at) AS ts '
                               'FROM agent_mailbox WHERE id=%s', (request_id,))[0]['ts']
            return request_id, float(created)

    def receipt(self, launch):
        key = {k: launch[k] for k in ('host', 'probe_agent', 'generation', 'release_epoch', 'nonce')}
        result = dict(key, verdict='pending', delivered=False, acknowledged=False, turn_ended=False)
        with self.control('returning') as (tx, control):
            if any(key[k] != control[k] for k in ('generation', 'release_epoch')):
                return result
            admitted = self.admission(tx, key)
            if admitted is None or launch['started_ts'] < admitted:
                return result
            rows = tx.query('SELECT m.delivered_at, m.meta, m.payload, m.recipient, '
                            't.agent, t.host, t.status, t.ended_at, '
                            'extract(epoch from t.started_at) AS started_ts, '
                            'r.status AS agent_status, r.last_error '
                            'FROM agent_mailbox m '
                            "LEFT JOIN turn_resources t ON t.turn_id=m.meta->'consigne'->>'turn' "
                            'LEFT JOIN agent_registry r ON r.name=m.recipient WHERE m.id=%s',
                            (launch['request_id'],))
            if len(rows) != 1:
                return result
            row = rows[0]
            if (row['recipient'] != key['probe_agent'] or row['payload'].get('vm_probe') != key
                    or 'retire' in row['meta']):
                return result
            result['delivered'] = row['delivered_at'] is not None
            if (not result['delivered'] or row['agent'] != key['probe_agent']
                    or row['host'] != key['host'] or row['started_ts'] is None
                    or float(row['started_ts']) < launch['started_ts']):
                return result
            ack = tx.query('SELECT id FROM agent_mailbox WHERE sender=%s AND recipient=%s '
                           "AND payload @> '{\"ack\":true}'::jsonb AND body=%s "
                           'AND created_at >= %s AND created_at <= coalesce(%s, clock_timestamp()) '
                           'AND host=%s LIMIT 1',
                           (key['probe_agent'], key['probe_agent'], 'VM-PROBE-ACK ' + key['nonce'],
                            row['delivered_at'], row['ended_at'], key['host']))
            result['acknowledged'] = bool(ack)
            result['turn_ended'] = row['status'] == 'done' and row['ended_at'] is not None
            if result['turn_ended']:
                result['verdict'] = ('ok' if result['acknowledged'] and row['agent_status'] == 'idle'
                                     and not row['last_error'] else 'ko')
            return result

    def quiet(self, *, returning):
        # Quiesce/halt are called while the worker holds the native gate on
        # another connection. Never try to acquire that same lock over SSH.
        with self.control('returning' if returning else 'draining', lock=False) as (tx, control):
            # A native runner must have returned since this wave. An old
            # heartbeat or a second identity elsewhere cannot complete it.
            if returning:
                admitted = self.admission(tx, control)
                if admitted is None:
                    return False
                now = float(tx.query('SELECT extract(epoch from clock_timestamp()) AS ts')[0]['ts'])
                if now - admitted >= self.cfg['reconcile_deadline_s']:
                    raise Refus('reconciliation deadline passed')
                rows = tx.query('SELECT name FROM agent_registry WHERE name=%s AND host=%s '
                                'AND runner_seen_host=%s AND runner_seen_owner IS NOT NULL '
                                'AND runner_seen_at >= to_timestamp(%s) '
                                'AND runner_seen_at > clock_timestamp()-make_interval(secs=>%s) '
                                'AND (lease_owner IS NULL OR lease_expires_at <= clock_timestamp() '
                                'OR lease_owner=runner_seen_owner) '
                                'AND NOT handoff_fenced AND retired_at IS NULL',
                                (self.cfg['probe_agent'], self.cfg['host'], self.cfg['host'],
                                 admitted, self.cfg['fresh_seconds']))
                if len(rows) != 1:
                    return False
            pending = tx.query('SELECT 1 FROM turn_resources WHERE host=%s '
                               "AND status IN ('running','orphan') LIMIT 1", (self.cfg['host'],))
            if pending:
                return False
            if returning:
                pending = tx.query('SELECT 1 FROM host_lifecycle_agents a '
                                   'JOIN agent_registry r ON r.name=a.agent '
                                   'WHERE a.host=%s AND a.generation=%s '
                                   'AND (r.host IS DISTINCT FROM a.host OR r.handoff_fenced '
                                   'OR (r.lease_owner IS NOT NULL '
                                   'AND (r.lease_expires_at IS NULL OR r.lease_expires_at > clock_timestamp()) '
                                   'AND (r.runner_seen_host IS DISTINCT FROM a.host '
                                   'OR r.lease_owner IS DISTINCT FROM r.runner_seen_owner))) LIMIT 1',
                                   (self.cfg['host'], control['generation']))
                if pending:
                    return False
                pending = tx.query('SELECT 1 FROM host_handoffs WHERE (source=%s OR target=%s) '
                                   "AND state NOT IN ('verified','cancelled') LIMIT 1",
                                   (self.cfg['host'], self.cfg['host']))
                if pending:
                    return False
            return True


class Endpoint:
    def __init__(self, cfg, native, store):
        self.cfg, self.native, self.store = cfg, native, store

    def execute(self, verb, args):
        if verb == 'probe-start':
            key = dict(zip(('host', 'probe_agent', 'generation', 'release_epoch', 'nonce'), args))
            if any(key[k] != self.cfg[k] for k in ('host', 'probe_agent')):
                raise Refus('foreign probe')
            with self.store.locked():
                old = self.store.load(key['nonce'])
                if old is not None:
                    if any(old.get(k) != v for k, v in key.items()):
                        raise Refus('nonce reused for another return')
                    return 0, None
                # The original database timestamp survives a crash after
                # deposit but before the local record: replay cannot move
                # the lower time bound past the already completed turn.
                request_id, started = self.native.start(key)
                self.store.save(key['nonce'], dict(key, request_id=request_id, started_ts=started))
            return 0, None
        if verb == 'probe-receipt':
            launch = self.store.load(args[0])
            if launch is None:
                return WAIT, None
            return 0, self.native.receipt(launch)
        if verb in ('reconcile', 'quiesce', 'runners'):
            return (0 if self.native.quiet(returning=verb != 'quiesce') else WAIT), None
        if verb == 'mail':
            # Same proof as health; a readmission invalidates old launches.
            for path in self.store.path.glob('*.json'):
                if NONCE.fullmatch(path.stem):
                    launch = self.store.load(path.stem)
                    if self.native.receipt(launch)['verdict'] == 'ok':
                        return 0, None
            return WAIT, None
        raise Refus('unknown native verb')


def main(argv=None, *, environ=None, connect=None, runner=subprocess.run):
    args = sys.argv[1:] if argv is None else argv
    if args != ['dispatch']:
        return 2
    env = os.environ if environ is None else environ
    db = None
    try:
        # No DB, process or privileged command is touched before this check.
        verb, tail = dispatch(env.get('SSH_ORIGINAL_COMMAND', ''))
        cfg = enrollment(private_json(CONFIG, root=True))
        if os.geteuid() == 0:
            raise Refus('non-root VM account required')
        if verb in ('/usr/bin/true', '/usr/bin/journalctl'):
            done = runner([verb] + tail, env=ENV, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False)
            if len(done.stdout) > LIMIT:
                return 1
            if verb == '/usr/bin/journalctl' and done.returncode == 0:
                sys.stdout.buffer.write(done.stdout)
            return done.returncode
        if verb in ('doctor', 'version-mode'):
            # Fixed root-installed checks, never an argv selected by SSH.
            check = cfg['checks'][verb]
            enrolled = Path(check[0])
            executable = enrolled.resolve(strict=True)
            for path in (enrolled, *enrolled.parents, executable, *executable.parents):
                info = path.lstat()
                if info.st_uid != 0 or info.st_mode & 0o022:
                    raise Refus('health executable writable outside root')
            return runner(check, env=ENV, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=5, check=False).returncode
        from ameesh import config, db as db_mod
        private_json(cfg['ameesh_config'], root=True)
        db = (connect or db_mod.connect)(config.load_file(cfg['ameesh_config'], env=ENV))
        native = Native(db, cfg)
        if verb == '/usr/bin/sudo':
            if not native.quiet(returning=False):
                return 1
            return runner(HALT, env=ENV, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=5, check=False).returncode
        code, value = Endpoint(cfg, native, Store(cfg['state_dir'])).execute(verb, tail)
        if value is not None:
            print(json.dumps(value, sort_keys=True))
        return code
    except (Exception, SystemExit):
        # No DSN, private path, command output or provider error in receipts.
        return 1
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
