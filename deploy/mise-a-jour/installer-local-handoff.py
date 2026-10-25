#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Install/remove only H8 socket units from an operator-pinned root bundle.

The operator first stages the reviewed runtime, package and lib tree from the
signed enrollment SHA. This entry never copies executable agent checkouts or
creates an identity/enrollment. Rollback leaves policy, code and journals intact.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import stat
import sys

BASE = Path('/opt/ameesh-release')
NAMES = ('ameesh-handoff-local.socket', 'ameesh-handoff-local@.service')
UNITS = Path('/etc/systemd/system')
RECEIPT = Path('/etc/ameesh-handoff/local-install.json')
ENDPOINT = Path('/run/ameesh-handoff')


def run(*args, absent_ok=False):
    result = subprocess.run(['/usr/bin/systemctl', *args], check=False, timeout=90,
                   env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})
    # systemctl exit 5 means the unit is not installed. Other failures remain fatal.
    if not (absent_ok and result.returncode == 5):
        result.check_returncode()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_bundle(module):
    for root in (BASE / 'runtime', BASE / 'lib'):
        if not root.is_dir() or root.is_symlink():
            raise ValueError('missing root bundle')
        for path in (root, *root.rglob('*')):
            info = path.lstat()
            if path.suffix.lower() == '.pth':
                raise ValueError('startup path hook')
            if stat.S_ISLNK(info.st_mode):
                target = path.resolve(strict=True)
                if target.suffix.lower() == '.pth' or not target.is_file():
                    raise ValueError('unsafe symlink')
                module.protected(target)
            elif stat.S_ISREG(info.st_mode):
                module.protected(path)
            elif stat.S_ISDIR(info.st_mode):
                if info.st_uid != 0 or info.st_mode & 0o022:
                    raise ValueError('unsafe directory')
            else:
                raise ValueError('special file')


def bootstrap(source):
    # Bootstrap chain checked before importing any privileged code.
    for path in (*reversed(source.parents), source):
        info = path.lstat()
        if (info.st_uid != 0 or info.st_mode & 0o022 or path.is_symlink()
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
            raise ValueError('unsafe root bundle')
    spec = importlib.util.spec_from_file_location('h8_install', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if os.geteuid() != 0 or len(args) != 1 or args[0] not in ('install', 'rollback'):
        raise ValueError('root install|rollback required')
    source = BASE / 'lib/dispatch-local-handoff.py'
    module = bootstrap(source)
    if args[0] == 'install':
        if RECEIPT.exists():
            raise ValueError('already installed: rollback before replacing')
        # Include all importable code and venv configuration in the root seal.
        # Symlinks are accepted only to another protected root-owned target.
        validate_bundle(module)
        endpoint = ENDPOINT
        endpoint.mkdir(mode=0o755, exist_ok=True)
        info = endpoint.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('unsafe socket directory')
        endpoint.chmod(0o755)
        rules = module.policy()
        import pwd
        if pwd.getpwnam('ameesh').pw_uid != rules['uid']:
            raise ValueError('caller uid differs from socket group enrollment')
        for name in NAMES:
            if (UNITS / name).exists() or (UNITS / name).is_symlink():
                raise ValueError('existing unit')
        sources = {name: module.protected(BASE / 'lib/systemd' / name).read_bytes()
                   for name in NAMES}
        # Pin the fixed interpreter before systemd can execute it.
        module.protected((BASE / 'runtime/bin/python').resolve(strict=True))
        module.protected(BASE / 'lib/hote-handoff.py')
        module.consistent_policy(rules, module.executor().load_execution())
        hashes = {}
        try:
            for name, content in sources.items():
                path = UNITS / name
                with path.open('xb') as stream:
                    os.fchmod(stream.fileno(), 0o644)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                hashes[name] = digest(path)
        finally:
            # Even a partial write is recorded and can be rolled back.
            with RECEIPT.open('x') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({'units': hashes}, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
        run('daemon-reload')
        run('enable', '--now', NAMES[0])
    else:
        value = json.loads(module.protected(RECEIPT, private=True).read_bytes())
        hashes = value['units']
        if not isinstance(hashes, dict) or set(hashes) - set(NAMES):
            raise ValueError('receipt')
        for name, expected in hashes.items():
            path = UNITS / name
            if not path.exists() and not path.is_symlink():
                continue
            if digest(module.protected(path)) != expected:
                raise ValueError('unit changed: operator review required')
        # Stop a loaded socket even when its unit file was removed.
        run('stop', NAMES[0], absent_ok=True)
        if (UNITS / NAMES[0]).exists():
            run('disable', NAMES[0])
        # No connected request survives rollback. Host journal is retained;
        # any interrupted effect must be reconciled before re-enrollment.
        run('stop', 'ameesh-handoff-local@*.service')
        for name in hashes:
            (UNITS / name).unlink(missing_ok=True)
        RECEIPT.unlink()
        run('daemon-reload')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('local_handoff_install_unavailable', file=sys.stderr)
        sys.exit(1)
