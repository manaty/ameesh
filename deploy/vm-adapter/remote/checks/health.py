#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#6296: same-user health producer and unprivileged, fail-closed readers.

Paths come only from root installed cards, never from the process
environment. #8069: the runner units are read from the user manager itself
(every loaded unit enabled or not inactive), not from a card posed once: a
root card went stale as soon as an agent was added or retired. The producer service still loads the ameesh
EnvironmentFile (its doctor and readiness children need it), and the
venv_processus verifier resolves its state directory from XDG_STATE_HOME.
The receipt proves health, not resistance to its producer.

version_mode and doctor are proven separately: version_mode needs the same
live execute runners and venv before and after the probe; doctor also needs
`ameesh doctor` and each runner's readiness, read from the lease its own
launched process (with its drop-ins) holds, never from a re-created context.
"""
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time

REMOTE = Path('/etc/ameesh-vm-adapter/health-reader.json')
CARD = Path('/etc/ameesh-vm-adapter/health-producer.json')
BOOT = Path('/proc/sys/kernel/random/boot_id')
SCHEMA = 'ameesh-vm-health/1'
# Runner blocks that mean "this host is not ready" (runner.py HOST_STATUS,
# DB_STATUS, WORKDIR_STATUS); other blocks (account, budget) are not health.
HOST_BLOCKS = frozenset({'hôte non prêt', 'base injoignable', 'dossier absent'})
RUNNER_UNIT = r'ameesh-runner-agent@[A-Za-z0-9_.-]+\.service'


def document(path, uid, mode):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or stat.S_IMODE(info.st_mode) != mode or info.st_size > 65536
                or info.st_nlink != 1):
            raise ValueError('unsafe document')
        value = json.loads(stream.read(65537))
    if not isinstance(value, dict):
        raise ValueError('object required')
    return value


def health_policy(cfg):
    policy = cfg
    if set(policy) != {'receipt', 'producer_uid', 'period_s', 'venv'}:
        raise ValueError('invalid policy')
    if (type(policy['producer_uid']) is not int or policy['producer_uid'] <= 0
            or type(policy['period_s']) is not int or not 1 <= policy['period_s'] <= 60
            or any(not isinstance(policy[key], str)
                   or not Path(policy[key]).is_absolute()
                   or '..' in Path(policy[key]).parts for key in ('receipt', 'venv'))):
        raise ValueError('invalid policy')
    return policy


def installed(venv):
    stable = Path(venv)
    current = stable.parent / 'venv-courant'
    return str((current if current.is_symlink() else stable).resolve(strict=True))


def check(kind, *, remote=REMOTE, boot=BOOT, now=time.time, reader=document):
    policy = health_policy(reader(remote, 0, 0o640))
    receipt = reader(Path(policy['receipt']), policy['producer_uid'], 0o640)
    keys = {'schema', 'boot_id', 'timestamp', 'version', 'sha', 'mode',
            'pids', 'version_mode', 'doctor', 'venv'}
    # A red doctor keeps its bounded reason next to a still proven version_mode.
    if (set(receipt) - {'reason'} != keys
            or ('reason' in receipt) == (receipt['doctor'] is True)
            or not re.fullmatch('[a-z-]+:[A-Za-z]+', str(receipt.get('reason', 'x:X')))):
        return 1
    stamp = receipt['timestamp']
    if (receipt['schema'] != SCHEMA or receipt['boot_id'] != boot.read_text().strip()
            or type(stamp) not in (float, int) or not math.isfinite(stamp)
            or not 0 <= now() - stamp <= 3 * policy['period_s']
            or not isinstance(receipt['pids'], list) or not receipt['pids']
            or any(type(pid) is not int or pid <= 0 for pid in receipt['pids'])
            or receipt['version_mode'] is not True
            or receipt['mode'] != 'execute'
            or not isinstance(receipt['version'], str) or not receipt['version']
            or not isinstance(receipt['sha'], str)
            or not re.fullmatch('[0-9a-f]{7,40}', receipt['sha'])
            or receipt['venv'] != installed(policy['venv'])):
        return 1
    return 0 if kind == 'version-mode' or receipt['doctor'] is True else 1


def command(argv, end, *, capture=True):
    remaining = end - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('producer deadline')
    done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, check=True, timeout=remaining)
    return done.stdout.decode() if capture else ''


def runner_state(unit, venv, verifier, end, *, proc=Path('/proc'), run=command):
    if not re.fullmatch(RUNNER_UNIT, unit):
        raise ValueError('invalid runner unit')
    props = run(['/usr/bin/systemctl', '--user', 'show', unit,
                 '--property=MainPID,ActiveState,SubState,NeedDaemonReload'], end)
    values = dict(line.split('=', 1) for line in props.splitlines() if '=' in line)
    if any(values.get(key) != val for key, val in (
            ('ActiveState', 'active'), ('SubState', 'running'), ('NeedDaemonReload', 'no'))):
        raise ValueError('runner inactive or configuration stale')
    pid = values['MainPID']
    verifier.verifier(pid, venv)
    argv = (proc / pid / 'cmdline').read_bytes().split(b'\0')
    # The stable ameesh launcher execs the versioned Python + ameesh script.
    if len(argv) < 4 or Path(os.fsdecode(argv[1])).name != 'ameesh' or argv[2] != b'run':
        raise ValueError('unexpected live command')
    if b'--once' in argv or b'--max-turns' in argv:
        raise ValueError('finite runner is not a permanent service')
    mode = 'dry-run' if b'--dry-run' in argv else 'execute'
    ids = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == b'--runner-id']
    if len(ids) != 1 or not re.fullmatch(rb'[A-Za-z0-9_.@-]{1,200}', ids[0]):
        raise ValueError('runner id not proven')
    return int(pid), mode, ids[0].decode()



def runner_units(end):
    """#8069: the permanent runners, read from the user manager, never a card.

    Expected: every instance default.target wants (its `.wants` links, the
    enablement, read whether or not the instance is loaded), and every loaded
    instance enabled or not `inactive` (failed, activating, active). A
    disabled and stopped unit (agent retired, trial over) is not expected; an
    expected one that is stopped, crashed or not loaded is refused by
    runner_state."""
    rows = json.loads(command([
        '/usr/bin/systemctl', '--user', 'list-units', '--all', '--type=service',
        '--output=json', '--no-pager', 'ameesh-runner-agent@*.service'], end))
    loaded = {row['unit'] for row in rows}
    wants = command(['/usr/bin/systemctl', '--user', 'show', '--property=Wants',
                     '--value', 'default.target'], end).split()
    wanted = {unit for unit in wants if unit.startswith('ameesh-runner-agent@')}
    names = sorted(loaded | wanted)
    if not names or len(names) > 64:
        raise ValueError('runner inventory empty or unbounded')
    if any(not re.fullmatch(RUNNER_UNIT, unit) for unit in names):
        raise ValueError('invalid runner unit')
    props = command(['/usr/bin/systemctl', '--user', 'show',
                     '--property=Id,ActiveState,UnitFileState', '--', *names], end)
    found = {}
    for block in props.strip().split('\n\n'):
        values = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        found[values.get('Id')] = values
    if set(found) != set(names):
        raise ValueError('runner inventory unreadable')
    units = [unit for unit in names if unit in wanted
             or found[unit].get('ActiveState') != 'inactive'
             or found[unit].get('UnitFileState', '').startswith('enabled')]
    if not units:
        raise ValueError('no permanent runner')
    return units


def runner_host(rows, runner_ids):
    """The one host the launched runners report from (`runner_seen_host` of
    the rows they last saw). Never the agent's `host`: that is its placement,
    the destination after a move, while the last sighting stays here."""
    hosts = {row.get('runner_seen_host') for row in rows if isinstance(row, dict)
             and row.get('runner_seen_owner') in runner_ids}
    if len(hosts) != 1 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', str(*hosts)):
        raise ValueError('runner host not proven')
    return hosts.pop()


def closed_return_probe(control, host):
    """The probe agent of a `returning` host whose claims are still closed,
    else None. Any unreadable control is a refusal, never an open host."""
    if (not isinstance(control, list) or len(control) != 1 or not isinstance(control[0], dict)
            or control[0].get('host') != host):
        raise ValueError('host control unreadable')
    row = control[0]
    if row.get('mode') != 'returning' or row.get('claims_open') is not False:
        return None
    if not isinstance(row.get('return_probe'), str) or not row['return_probe']:
        raise ValueError('closed return without probe')
    return row['return_probe']


def lacking(rows, runner_ids):
    return [runner_id for runner_id in runner_ids
            if not any(isinstance(row, dict) and row.get('lease_owner') == runner_id
                       for row in rows)]


def readiness(rows, runner_ids, now, probe=None):
    """Each launched runner holds a live lease and reports no host block.

    #8069: in a `returning` return whose claims are still closed (`probe`
    named), only the probe agent can be claimed (#7874); the other runners
    hold no lease and stay proven by runner_state (active, execute, venv).
    The probe's own lease is then required, from one of the launched runners."""
    if not isinstance(rows, list):
        raise ValueError('invalid agent overview')
    required = runner_ids
    if probe is not None:
        owners = [row.get('lease_owner') for row in rows
                  if isinstance(row, dict) and row.get('name') == probe]
        if len(owners) != 1 or owners[0] not in runner_ids:
            raise ValueError('probe holds no lease')
        required = owners
    for runner_id in runner_ids:
        held = [row for row in rows if isinstance(row, dict)
                and row.get('lease_owner') == runner_id]
        if not held and runner_id in required:
            raise ValueError('runner holds no lease')
        for row in held:
            expires = row.get('lease_expires_ts')
            if type(expires) not in (float, int) or not math.isfinite(expires) or expires <= now:
                raise ValueError('runner lease expired')
            if row.get('status') == 'blocked' and row.get('status_text') in HOST_BLOCKS:
                raise ValueError('runner reports host not ready')


def atomic_receipt(path, value):
    fd, name = tempfile.mkstemp(prefix='.health-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o640)
            json.dump(value, stream, allow_nan=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def failure(stage, error):
    # Fixed vocabulary: even a custom exception class name may carry secrets.
    classes = (FileNotFoundError, PermissionError, TimeoutError,
               subprocess.TimeoutExpired, subprocess.CalledProcessError,
               OSError, ValueError, KeyError, TypeError, RuntimeError,
               ImportError, Exception)
    name = next(cls.__name__ for cls in classes if isinstance(error, cls))
    reason = stage + ':' + name
    print('vm-health ' + reason, file=sys.stderr)
    return reason


def produce(*, card=CARD, boot=BOOT, reader=document):
    stage = 'setup'
    result = None
    try:
        if os.geteuid() == 0:
            raise ValueError('producer must run as ameesh')
        cfg = reader(card, 0, 0o644)
        # #8069: `units` of an older card is obsolete and ignored.
        if set(cfg) - {'units'} != {'venv', 'verifier', 'environment_file', 'receipt'}:
            raise ValueError('invalid producer card')
        for key in ('venv', 'verifier', 'environment_file', 'receipt'):
            if not isinstance(cfg[key], str) or not Path(cfg[key]).is_absolute():
                raise ValueError('absolute path required')
        stage = 'boot'
        end = time.monotonic() + 45
        result = dict(schema=SCHEMA, boot_id='', timestamp=time.time(),
                      version='', sha='', mode='', venv='', pids=[], version_mode=False, doctor=False)
        result['boot_id'] = boot.read_text().strip()
        stage = 'verifier'
        spec = importlib.util.spec_from_file_location('venv_processus', cfg['verifier'])
        if spec is None or spec.loader is None:
            raise ValueError('verifier unavailable')
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        venv = Path(cfg['venv'])
        stage = 'inventory'
        units = runner_units(end)
        stage = 'runners-before'
        before = [runner_state(unit, venv, verifier, end) for unit in units]
        if {state[1] for state in before} != {'execute'}:
            raise ValueError('runner mode is not execute')
        stage = 'venv'
        live = verifier.venv_installe(venv)
        python = live / 'bin/python'
        # Same direct_url source identity as vm.sh installe_agents, from the
        # venv proven above, never from an arbitrary checkout or stable launcher.
        stage = 'metadata'
        metadata = json.loads(command([str(python), '-I', '-c',
            'import importlib.metadata as m,json; '
            'd=m.distribution("ameesh"); '
            'print(json.dumps({"version":d.version,"source":json.loads(d.read_text("direct_url.json"))}))'], end))
        source = metadata['source']
        sha = source.get('vcs_info', {}).get('commit_id', '')
        if not sha:
            sha = source.get('url', '').rsplit('/src-', 1)[-1]
        if not re.fullmatch('[0-9a-f]{7,40}', sha):
            raise ValueError('unproven source SHA')
        result.update(version=metadata['version'], sha=sha, mode=before[0][1], venv=str(live),
                      pids=[state[0] for state in before])

        def transient(seconds, *verb):
            return ['/usr/bin/systemd-run', '--user', '--pipe', '--wait', '--quiet',
                    '--property=RuntimeMaxSec=%d' % seconds, '--property=TimeoutStopSec=1',
                    '--property=EnvironmentFile=' + cfg['environment_file'],
                    '--', str(python), '-I', '-m', 'ameesh', *verb]
        try:
            stage = 'doctor'
            command(transient(30, 'doctor'), end, capture=False)
            stage = 'readiness'
            # Bounded: one registry read, after doctor, for the runners above;
            # the host control is read only when a runner holds no lease.
            rows = json.loads(command(transient(8, 'list', '--json'), end))
            ids = [state[2] for state in before]
            probe = None
            if isinstance(rows, list) and lacking(rows, ids):
                host = runner_host(rows, ids)
                probe = closed_return_probe(json.loads(command(
                    transient(8, 'host', 'status', host), end)), host)
            readiness(rows, ids, time.time(), probe)
            healthy = True
        except Exception as error:
            # doctor red does not hide version_mode, still proven below.
            reason, healthy = failure(stage, error), False
        stage = 'runners-after'
        after = [runner_state(unit, venv, verifier, end) for unit in units]
        if before != after or verifier.venv_installe(venv) != live:
            raise ValueError('runner changed during health probe')
        result.update(version_mode=True, doctor=healthy)
        if not healthy:
            result['reason'] = reason
    except Exception as error:
        reason = failure(stage, error)
        if result is None:
            return 1
        # Red receipts alone gain a reason; green schema remains unchanged.
        result.update(version_mode=False, doctor=False, reason=reason)
    try:
        atomic_receipt(Path(cfg['receipt']), result)
    except Exception as error:
        failure('publish', error)
        return 1
    return 0 if result['version_mode'] and result['doctor'] else 1


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        if args == ['produce']:
            return produce()
        if args in (['doctor'], ['version-mode']):
            return check(args[0])
    except Exception as error:
        failure('produce' if args == ['produce'] else 'read', error)
    return 1


if __name__ == '__main__':
    sys.exit(main())
