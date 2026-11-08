#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#6296: same-user health producer and unprivileged, fail-closed readers.

No process environment is read. Paths and unit names come only from root
installed cards. The receipt proves health, not resistance to its producer.
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
    if set(receipt) != {'schema', 'boot_id', 'timestamp', 'version', 'sha', 'mode',
                        'pids', 'version_mode', 'doctor', 'venv'}:
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
    if not re.fullmatch(r'ameesh-runner-agent@[A-Za-z0-9_.-]+\.service', unit):
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
    return int(pid), mode


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
        if set(cfg) != {'venv', 'verifier', 'units', 'environment_file', 'receipt'}:
            raise ValueError('invalid producer card')
        for key in ('venv', 'verifier', 'environment_file', 'receipt'):
            if not isinstance(cfg[key], str) or not Path(cfg[key]).is_absolute():
                raise ValueError('absolute path required')
        if (not isinstance(cfg['units'], list) or not cfg['units']
                or len(cfg['units']) > 64 or len(set(cfg['units'])) != len(cfg['units'])):
            raise ValueError('explicit runner inventory required')
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
        inventory = json.loads(command([
            '/usr/bin/systemctl', '--user', 'list-units', '--all', '--type=service',
            '--output=json', '--no-pager', 'ameesh-runner-agent@*.service'], end))
        active = {row['unit'] for row in inventory if row['active'] == 'active'}
        if active != set(cfg['units']):
            raise ValueError('active runner inventory differs from card')
        stage = 'runners-before'
        before = [runner_state(unit, venv, verifier, end) for unit in cfg['units']]
        if {mode for pid, mode in before} != {'execute'}:
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
                      pids=[pid for pid, mode in before])
        stage = 'doctor'
        command(['/usr/bin/systemd-run', '--user', '--pipe', '--wait', '--quiet',
                 '--property=RuntimeMaxSec=35', '--property=TimeoutStopSec=1',
                 '--property=EnvironmentFile=' + cfg['environment_file'],
                 '--', str(python), '-I', '-m', 'ameesh', 'doctor'], end, capture=False)
        stage = 'runners-after'
        after = [runner_state(unit, venv, verifier, end) for unit in cfg['units']]
        if before != after or verifier.venv_installe(venv) != live:
            raise ValueError('runner changed during health probe')
        result.update(version_mode=True, doctor=True)
    except Exception as error:
        reason = failure(stage, error)
        if result is None:
            return 1
        # Red receipts alone gain a reason; green schema remains unchanged.
        result['reason'] = reason
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
