#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#6252: the five operator commands of one enrolled VM (README step 5).

    vm-commande.py <quiesce|halt|halted|health|reconcile> <cible.json>

Run by the adapter worker (`run_probe`): fixed absolute argv, no shell,
outputs discarded, a scrubbed environment (`PATH`, `LANG`), at most
`io_timeout_s`. This program adds nothing to that environment and passes
nothing of it on: every child gets the same two variables, so no cloud
secret is ever inherited. Host, user, key and known_hosts come from the
operator's private target file `cible.json` (one per VM, 0600, no link),
never from code: no client or host name is written here.

Exit codes follow the worker (#6073): 0 proven, 75 (EX_TEMPFAIL) health
turn still in flight, anything else not proven. The worker reads every
non-zero code of quiesce, halted and reconcile as "pending", and halt's code
as nothing at all; failure is a wait, never an effect.

- quiesce: read-only. No turn of the host running in the native base, the
  native control closed (`draining`), then the operator's remote checks.
- halt: the halt marker is written first (intent), then the remote in-guest
  poweroff of this VM alone, bounded; a dropped connection is expected.
- halted: 0 only when the operator SSH of this VM no longer answers within
  its bound. Authentication or host-key refusals are not silence. A witness,
  never a proof alone: the worker also requires `stopped in place`.
- health: bound to the native control (`returning`, claims closed,
  `return_probe` = `probe_agent`, generation, epoch, admission time). The
  probe turn is launched ONCE per admission, asynchronously, with a fresh
  nonce recorded before the launch; until the remote receipt of that very
  nonce is complete (delivered, acknowledged, turn ended, verdict ok) the
  answer is 75. A receipt of another nonce, generation, epoch or probe is
  old and never accepted; delivery alone is not completion. Then the remote
  health checks (version/mode, runners, doctor, mail) and, after an adapter
  halt, the end of the previous boot's journal (`journalctl -b -1`,
  read-only) must pass.
- reconcile: closed return only (never opens claims, never forces `lost`),
  then the operator's remote reconciliation commands.

The native VM endpoint (#6276), remote/vm-remote.py, implements probe_start,
probe_receipt and closed reconciliation under a forced SSH command. The
operator enrolls the VM account and its fixed version/mode and doctor checks
(see README, "Commandes natives dans la VM").
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import sys
import time

VERBS = ('quiesce', 'halt', 'halted', 'health', 'reconcile')
WAIT = 75  # ameesh.host_release.ADAPTER_WAIT_CODE, EX_TEMPFAIL
ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
OUTPUT = 65536
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')
USER = re.compile(r'[a-z_][a-z0-9_-]{0,31}\Z')
DESTINATION = re.compile(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}\Z')
NONCE = re.compile(r'[0-9a-f]{32}\Z')
#: The only substitutions, in `probe_start` and `probe_receipt` alone.
FIELDS = ('host', 'probe_agent', 'generation', 'release_epoch', 'nonce')
REMOTE = {'quiesce': 'many', 'halt': 'one', 'alive': 'one', 'probe_start': 'one',
          'probe_receipt': 'one', 'health': 'many', 'previous_boot': 'one',
          'reconcile': 'many'}
KEYS = {'host', 'probe_agent', 'ssh', 'state_dir', 'ameesh_config', 'budget_s',
        'previous_boot_end', 'remote'}
SSH_KEYS = {'executable', 'destination', 'user', 'port', 'identity_file',
            'known_hosts', 'connect_timeout_s'}
#: ssh's own refusals: the VM answered, so it is not silent (halted).
ANSWERED = ('Permission denied', 'Host key verification failed',
            'REMOTE HOST IDENTIFICATION HAS CHANGED', 'No ED25519 host key',
            'Too many authentication failures')


class Refus(ValueError):
    pass


def _private(path, *, directory=False, readable_by_others=False):
    """Operator-owned, not a link, not writable (nor readable) by others."""
    info = os.lstat(path)
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    mask = 0o022 if readable_by_others else 0o077
    if (not kind(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & mask
            or (not directory and info.st_nlink != 1)):
        raise Refus('private operator-owned path required')


def read_json(path, *, limit=OUTPUT):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > limit):
            raise Refus('private operator-owned file required')
        try:
            return json.loads(stream.read(limit + 1))
        except ValueError:
            raise Refus('invalid private JSON') from None


def _absolute(value):
    if (not isinstance(value, str) or not value.startswith('/') or '\x00' in value
            or '..' in Path(value).parts):
        raise Refus('absolute operator path required')
    return value


def _argv(value, *, templated=False):
    if (not isinstance(value, list) or not value
            or not all(isinstance(a, str) and a and '\x00' not in a and '\n' not in a
                       for a in value)):
        raise Refus('fixed remote argv required')
    if not value[0].startswith('/'):
        raise Refus('absolute remote executable required')
    if not templated and any('{' + f + '}' in a for a in value for f in FIELDS):
        raise Refus('substitution outside the probe commands')
    return list(value)


def target(value):
    """Validate one operator target document (`cible.json`); returns it."""
    if not isinstance(value, dict) or set(value) - KEYS or set(KEYS) - set(value) - {'budget_s'}:
        raise Refus('invalid target document')
    for key in ('host', 'probe_agent'):
        if not isinstance(value[key], str) or not NAME.fullmatch(value[key]):
            raise Refus('invalid host or probe agent')
    ssh = value['ssh']
    if not isinstance(ssh, dict) or set(ssh) != SSH_KEYS:
        raise Refus('invalid ssh access')
    for key in ('executable', 'identity_file', 'known_hosts'):
        _absolute(ssh[key])
    if not isinstance(ssh['destination'], str) or not DESTINATION.fullmatch(ssh['destination']):
        raise Refus('invalid ssh destination')
    if not isinstance(ssh['user'], str) or not USER.fullmatch(ssh['user']):
        raise Refus('invalid ssh user')
    port = ssh['port']
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise Refus('invalid ssh port')
    budget = value.setdefault('budget_s', 9)
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not 1 <= budget <= 9.5:
        raise Refus('command budget must stay under the 10 s worker bound')
    connect = ssh['connect_timeout_s']
    if isinstance(connect, bool) or not isinstance(connect, int) or not 1 <= connect < budget:
        raise Refus('invalid ssh connect timeout')
    _absolute(value['state_dir'])
    _absolute(value['ameesh_config'])
    try:
        re.compile(value['previous_boot_end'])
    except (TypeError, re.error):
        raise Refus('invalid previous boot pattern') from None
    remote = value['remote']
    if not isinstance(remote, dict) or set(remote) != set(REMOTE):
        raise Refus('the remote commands are exactly ' + ', '.join(sorted(REMOTE)))
    for key, shape in REMOTE.items():
        templated = key in ('probe_start', 'probe_receipt')
        if shape == 'one':
            _argv(remote[key], templated=templated)
        elif not isinstance(remote[key], list) or not remote[key]:
            raise Refus('fixed remote argv list required')
        else:
            for argv in remote[key]:
                _argv(argv)
    return value


def run(argv, timeout, *, capture=False):
    """(code, stdout, stderr); code None when the command did not end in time
    or could not start (`started` False). Outputs bounded, never printed."""
    sink = subprocess.PIPE if capture else subprocess.DEVNULL
    try:
        done = subprocess.run(argv, env=dict(ENV), stdin=subprocess.DEVNULL, stdout=sink,
                              stderr=sink, timeout=max(timeout, 0.1), check=False)
    except subprocess.TimeoutExpired:
        return None, b'', b''
    except (OSError, subprocess.SubprocessError):
        return 'unstarted', b'', b''
    return done.returncode, (done.stdout or b'')[:OUTPUT + 1], (done.stderr or b'')[:OUTPUT + 1]


class Natif:
    """Read-only view of the native base, the one the worker reads."""

    def __init__(self, path):
        from ameesh import config, db as db_mod
        from ameesh.storage.postgres.vm_adapter import Queue
        self.db = db_mod.connect(config.load_file(path), writer=False)
        self.queue = Queue(self.db)

    @classmethod
    def of(cls, db):
        from ameesh.storage.postgres.vm_adapter import Queue
        natif = cls.__new__(cls)
        natif.db, natif.queue = db, Queue(db)
        return natif

    def control(self, host):
        control = self.queue.control(host)
        if control:
            control = dict(control, return_probe=self.queue.return_probe(host))
        return control

    def admitted(self, host, generation, epoch):
        return self.queue.result_since(dict(host=host, generation=generation,
                                            release_epoch=epoch, state='busy'),
                                       'probe admitted')

    def now(self):
        return self.queue.now()

    def running_turns(self, host):
        return int(self.db.query("SELECT count(*) AS n FROM turn_resources "
                                 "WHERE host=%s AND status='running'", (host,))[0]['n'])

    def close(self):
        close = getattr(self.db, 'close', None)
        if close:
            close()


class Commande:
    def __init__(self, cible, *, natif, runner=run, clock=time.monotonic):
        self.t, self.natif, self.runner, self.clock = cible, natif, runner, clock
        self.deadline = clock() + cible['budget_s']
        self.state = Path(cible['state_dir'])

    # -- transport -----------------------------------------------------------
    def left(self):
        return self.deadline - self.clock()

    def ssh(self, remote, *, capture=False, alive=False):
        s = self.t['ssh']
        connect = min(s['connect_timeout_s'], max(1, int(self.left())))
        argv = [s['executable'], '-F', 'none', '-T', '-x', '-a', '-n',
                '-p', str(s['port']), '-l', s['user'], '-i', s['identity_file'],
                '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
                '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + s['known_hosts'],
                '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'UpdateHostKeys=no',
                '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
                '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
                '-o', 'ClearAllForwardings=yes', '-o', 'LogLevel=ERROR',
                '-o', 'ConnectTimeout=%d' % connect,
                '-o', 'ServerAliveInterval=1', '-o', 'ServerAliveCountMax=2',
                '--', s['destination'], shlex.join(remote)]
        if self.left() <= 0:
            return 'unstarted', b'', b''  # never read as a silent VM
        return self.runner(argv, self.left(), capture=capture or alive)

    def checks(self, key):
        """Every remote argv of `key` answers 0, in order, within the budget."""
        return self.outcome(key) == 'ok'

    def outcome(self, key):
        """'ok', 'transport' (no answer: bound reached, ssh not started, ssh's
        own 255) or 'failed' (a remote command answered non-zero). Stops at
        the first argv that is not 'ok'."""
        for argv in self.t['remote'][key]:
            verdict = transport(self.ssh(argv)[0])
            if verdict != 'ok':
                return verdict
        return 'ok'

    def preflight(self):
        s = self.t['ssh']
        _private(s['identity_file'])
        _private(s['known_hosts'], readable_by_others=True)
        _private(self.t['state_dir'], directory=True)
        _private(self.t['ameesh_config'])

    # -- private state ---------------------------------------------------------
    def path(self, name):
        # One shared StateDirectory: `<name>.<host>.json` per VM.
        return self.state / ('%s.%s.json' % (name, self.t['host']))

    def load(self, name):
        try:
            return read_json(self.path(name))
        except FileNotFoundError:
            return None

    def save(self, name, value):
        path = self.path(name)
        tmp = self.state / ('.%s.%s' % (path.name, secrets.token_hex(4)))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        # The intent survives a crash only once the directory entry is durable.
        directory = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def exclusive(self):
        """The operator lock of this VM, held by the caller until it closes
        the returned descriptor. Two worker invocations may run the same verb
        at once (health and reconcile run before the worker's own lock): the
        read, decision, persistence and launch of a probe are one critical
        section. Never waits: a busy VM answers like a wait."""
        fd = os.open(self.path('lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                     | os.O_CLOEXEC, 0o600)
        try:
            _private(self.path('lock'))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def drop(self, name):
        try:
            os.unlink(self.path(name))
        except FileNotFoundError:
            pass

    # -- verbs ---------------------------------------------------------------
    def control(self, mode):
        control = self.natif.control(self.t['host'])
        if not control or control['claims_open'] or control['mode'] != mode:
            raise Refus('native control not closed ' + mode)
        return control

    def quiesce(self):
        self.control('draining')
        if self.natif.running_turns(self.t['host']):
            return 1
        return 0 if self.checks('quiesce') else 1

    def halt(self):
        self.control('draining')
        if self.natif.running_turns(self.t['host']):
            return 1
        # Intent first: health checks the previous boot's end after our halt.
        self.save('halt', {'host': self.t['host'], 'at': self.natif.now()})
        code = self.ssh(self.t['remote']['halt'])[0]
        # The guest drops the connection while powering off (255) or keeps
        # it until our bound (None): both are expected; the code proves nothing.
        return 0 if code in (0, 255, None) else 1

    def halted(self):
        code, _out, err = self.ssh(self.t['remote']['alive'], alive=True)
        if code == 'unstarted' or code == 0:
            return 1
        if code is None:
            return 0
        if code == 255 and not any(m.encode() in err for m in ANSWERED):
            return 0
        return 1

    def key(self, control):
        return {'host': self.t['host'], 'probe_agent': self.t['probe_agent'],
                'generation': control['generation'], 'release_epoch': control['release_epoch']}

    def substituted(self, argv, fields):
        out = []
        for arg in argv:
            for name in FIELDS:
                arg = arg.replace('{' + name + '}', str(fields[name]))
            out.append(arg)
        return out

    def health(self):
        control = self.control('returning')
        if control.get('return_probe') != self.t['probe_agent']:
            return 1
        key = self.key(control)
        admitted = self.natif.admitted(self.t['host'], key['generation'], key['release_epoch'])
        if admitted is None:
            return 1
        launch = self.load('probe')
        if (not isinstance(launch, dict) or {k: launch.get(k) for k in key} != key
                or not isinstance(launch.get('launched_at'), (int, float))
                or launch['launched_at'] < admitted
                or not NONCE.fullmatch(str(launch.get('nonce')))):
            # Once per admission: the nonce is durable before the launch, so a
            # lost answer or a crash never launches a second probe turn.
            launch = dict(key, nonce=secrets.token_hex(16), launched_at=self.natif.now())
            self.save('probe', launch)
            self.ssh(self.substituted(self.t['remote']['probe_start'], launch))
            return WAIT
        code, out, _err = self.ssh(self.substituted(self.t['remote']['probe_receipt'], launch),
                                   capture=True)
        verdict = receipt(out, launch) if code == 0 else 'pending'
        if verdict != 'ok':
            return WAIT if verdict == 'pending' else 1
        # Transport unavailable is a wait (bounded by health_deadline_s), a
        # remote check that answers non-zero is a real health failure.
        verdict = self.outcome('health')
        if verdict != 'ok':
            return WAIT if verdict == 'transport' else 1
        if self.load('halt') is not None:
            code, out, _err = self.ssh(self.t['remote']['previous_boot'], capture=True)
            if transport(code) == 'transport':
                return WAIT
            lines = [l for l in out.decode('utf-8', 'replace').splitlines() if l.strip()]
            if (code != 0 or len(out) > OUTPUT or not lines
                    or not re.fullmatch(self.t['previous_boot_end'], lines[-1].strip())):
                return 1
            self.drop('halt')
        return 0

    def reconcile(self):
        control = self.control('returning')
        if control.get('return_probe') != self.t['probe_agent']:
            return 1
        return 0 if self.checks('reconcile') else 1


def transport(code):
    """ssh's own 255, a bound reached (None) or no ssh at all ('unstarted')
    say nothing about the guest; only a remote answer is a verdict."""
    if code == 0:
        return 'ok'
    return 'transport' if code in (None, 'unstarted', 255) else 'failed'


def receipt(raw, launch):
    """'ok' only for the complete receipt of this launch; 'ko' for its failure;
    'pending' otherwise (absent, unreadable, in flight or another launch's)."""
    if len(raw) > OUTPUT:
        return 'pending'
    lines = [l for l in raw.decode('utf-8', 'replace').splitlines() if l.strip()]
    try:
        value = json.loads(lines[-1]) if lines else None
    except ValueError:
        return 'pending'
    if not isinstance(value, dict):
        return 'pending'
    if any(value.get(k) != launch[k] or isinstance(value.get(k), bool) for k in (
            'nonce', 'host', 'probe_agent', 'generation', 'release_epoch')):
        return 'pending'  # an old or foreign receipt is never this probe's proof
    if value.get('verdict') == 'ko':
        return 'ko'
    if (value.get('verdict') == 'ok' and value.get('delivered') is True
            and value.get('acknowledged') is True and value.get('turn_ended') is True):
        return 'ok'
    return 'pending'


def main(argv=None, *, natif=None, runner=run):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in VERBS:
        return 2
    verb = args[0]
    own = lock = None
    try:
        # A missing or unreadable operator file is a refusal, never a wait.
        cible = target(read_json(args[1]))
        command = Commande(cible, natif=natif, runner=runner)
        command.preflight()
    except (Refus, OSError):
        return 1
    try:
        lock = command.exclusive()
    except BlockingIOError:
        # Another invocation holds this VM: it owns the decision.
        return WAIT if verb == 'health' else 1
    except (Refus, OSError):
        return 1
    try:
        if natif is None and verb != 'halted':
            own = command.natif = Natif(cible['ameesh_config'])
        return getattr(command, verb)()
    except Refus:
        return 1
    except Exception:
        # Base or transport unavailable: health waits (bounded by the
        # worker's health_deadline_s), every other verb is "not proven".
        return WAIT if verb == 'health' else 1
    finally:
        if own is not None:
            try:
                own.close()
            except Exception:
                pass
        os.close(lock)


if __name__ == '__main__':
    raise SystemExit(main())
