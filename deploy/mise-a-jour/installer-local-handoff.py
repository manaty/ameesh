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
import sys

BASE = Path('/opt/ameesh-release')
NAMES = ('ameesh-handoff-local.socket', 'ameesh-handoff-local@.service')
UNITS = Path('/etc/systemd/system')
RECEIPT = Path('/etc/ameesh-handoff/local-install.json')


def run(*args):
    subprocess.run(['/usr/bin/systemctl', *args], check=True, timeout=90,
                   env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if os.geteuid() != 0 or len(args) != 1 or args[0] not in ('install', 'rollback'):
        raise ValueError('root install|rollback required')
    source = BASE / 'lib/dispatch-local-handoff.py'
    # Bootstrap chain checked before importing any privileged code.
    import stat
    for path in (*reversed(source.parents), source):
        info = path.lstat()
        if (info.st_uid != 0 or info.st_mode & 0o022 or path.is_symlink()
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
            raise ValueError('unsafe root bundle')
    spec = importlib.util.spec_from_file_location('h8_install', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if args[0] == 'install':
        if RECEIPT.exists():
            raise ValueError('already installed: rollback before replacing')
        # Include all importable code and venv configuration in the root seal.
        # Symlinks are accepted only to another protected root-owned target.
        for root in (BASE / 'runtime', BASE / 'lib'):
            if not root.is_dir() or root.is_symlink():
                raise ValueError('missing root bundle')
            for path in root.rglob('*'):
                if path.is_symlink():
                    target = path.resolve(strict=True)
                    if target.is_dir():
                        raise ValueError('directory symlink')
                    module.protected(target)
                elif path.is_file():
                    module.protected(path)
                else:
                    info = path.lstat()
                    if info.st_uid != 0 or info.st_mode & 0o022:
                        raise ValueError('unsafe directory')
        endpoint = Path('/run/ameesh-handoff')
        endpoint.mkdir(mode=0o755, exist_ok=True)
        info = endpoint.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('unsafe socket directory')
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
        module.executor().load_execution()
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
            if digest(module.protected(UNITS / name)) != expected:
                raise ValueError('unit changed: operator review required')
        run('disable', '--now', NAMES[0])
        # No connected request survives rollback. Host journal is retained;
        # any interrupted effect must be reconciled before re-enrollment.
        run('stop', 'ameesh-handoff-local@*.service')
        for name in hashes:
            (UNITS / name).unlink()
        RECEIPT.unlink()
        run('daemon-reload')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('local_handoff_install_unavailable', file=sys.stderr)
        sys.exit(1)
