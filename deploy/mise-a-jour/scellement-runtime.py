#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""H8 trust entry, installed OUTSIDE the measured tree by signed bootstrap.

Only distribution Python/stdlib is loaded before verification. No cache, no
network, no signature key, no imports from the measured tree. A protected pin
is an installed result of SignedBootstrapAuthority, never caller input.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

BASE = Path('/opt/ameesh-release')
PIN = Path('/etc/ameesh-release/runtime-seal.json')
LOCK = Path('/etc/ameesh-release/runtime-seal.lock')
ENTRY = '/usr/local/libexec/ameesh-runtime-seal.py'
MAX_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 30000
MAX_DOCUMENT = 8 * 1024 * 1024
SECONDS = 8


class Refused(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def identity(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused('duplicate key')
        result[key] = value
    return result


def protected(path, *, uid=0):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise Refused('path')
    for ancestor in (*reversed(path.parents), path):
        info = ancestor.lstat()
        if (info.st_uid != uid or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode)
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
            raise Refused('unprotected path')
    return path


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_nlink)


def read(path, limit, *, uid=0):
    protected(path, uid=uid)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != uid or before.st_mode & 0o7022 or before.st_size > limit):
            raise Refused('unsafe file')
        data = stream.read(limit + 1)
        if len(data) > limit or stamp(before) != stamp(os.fstat(stream.fileno())):
            raise Refused('file changed')
        if stamp(path.lstat()) != stamp(before):
            raise Refused('file substituted')
        return data


def file_digest(path, limit, *, uid, started, clock):
    protected(path, uid=uid)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid
                or before.st_nlink != 1 or before.st_mode & 0o7022 or before.st_size != limit):
            raise Refused('unsafe measured file')
        hashed, size = hashlib.sha256(), 0
        while True:
            if clock() - started > SECONDS:
                raise Refused('verification deadline')
            part = stream.read(min(65536, limit - size + 1))
            if not part:
                break
            size += len(part)
            if size > limit:
                raise Refused('file grew')
            hashed.update(part)
        if (size != limit or stamp(before) != stamp(os.fstat(stream.fileno()))
                or stamp(before) != stamp(path.lstat())):
            raise Refused('file changed during measurement')
        return hashed.hexdigest()


def validate(document):
    if (not isinstance(document, dict)
            or set(document) != {'version', 'sha', 'machine_id', 'trees'}
            or type(document['version']) is not int or document['version'] != 1
            or not isinstance(document['sha'], str) or not re.fullmatch('[0-9a-f]{40}', document['sha'])
            or not isinstance(document['machine_id'], str)
            or not re.fullmatch('[0-9a-f]{32}', document['machine_id'])
            or not isinstance(document['trees'], dict) or set(document['trees']) != {'runtime', 'lib', 'store'}):
        raise Refused('manifest')
    total = 0
    count = 2
    for tree in document['trees'].values():
        if (not isinstance(tree, dict) or set(tree) != {'version', 'dirs', 'files', 'bytes', 'digest'}
                or type(tree['version']) is not int or tree['version'] != 1
                or type(tree['bytes']) is not int or tree['bytes'] < 0
                or not isinstance(tree['dirs'], list) or not isinstance(tree['files'], dict)):
            raise Refused('tree manifest')
        body = {key: value for key, value in tree.items() if key != 'digest'}
        if tree['digest'] != identity(body) or tree['dirs'] != sorted(set(tree['dirs'])) or '.' not in tree['dirs']:
            raise Refused('tree digest')
        names = set(tree['dirs'])
        size = 0
        for name, entry in tree['files'].items():
            if (not isinstance(entry, dict) or set(entry) != {'size', 'mode', 'sha256'}
                    or type(entry['size']) is not int or entry['size'] < 0
                    or type(entry['mode']) is not int or entry['mode'] not in (0o444, 0o555)
                    or not isinstance(entry['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])
                    or name in names):
                raise Refused('file manifest')
            size += entry['size']
        for name in [*tree['dirs'], *tree['files']]:
            if (not isinstance(name, str) or not name or (name != '.' and
                    (Path(name).is_absolute() or '..' in Path(name).parts or Path(name).as_posix() != name))
                    or Path(name).suffix.lower() == '.pth'):
                raise Refused('manifest path')
            if name != '.' and Path(name).parent.as_posix() not in names:
                raise Refused('missing parent')
        if size != tree['bytes']:
            raise Refused('byte count')
        total += size
        count += len(tree['dirs']) + len(tree['files'])
    if total > MAX_BYTES or count > MAX_ENTRIES:
        raise Refused('manifest budget')
    return document


def verify(document, base=BASE, *, uid=0, clock=time.monotonic):
    """Full bounded measurement, no privileged imports. Caller holds the lock."""
    validate(document)
    started = clock()
    protected(base, uid=uid)
    expected = {'.': ('dir', 0o755)}
    for prefix, tree in document['trees'].items():
        for name in tree['dirs']:
            expected[prefix if name == '.' else prefix + '/' + name] = ('dir', 0o711 if prefix == 'store' and name == '.' else 0o555)
        for name, entry in tree['files'].items():
            expected[prefix + '/' + name] = ('file', entry)
    config = read(Path(base) / 'runtime/pyvenv.cfg', 8192, uid=uid).decode('utf-8')
    settings = {}
    for line in config.splitlines():
        if '=' in line:
            key, value = (part.strip() for part in line.split('=', 1))
            if key in settings:
                raise Refused('ambiguous venv configuration')
            settings[key] = value
    if (settings.get('include-system-site-packages') != 'false'
            or settings.get('home') != str(Path(sys._base_executable).resolve().parent)):
        raise Refused('system packages outside manifest')
    seen, snapshots = set(), {}
    def inaccessible(error):
        raise Refused('unreadable tree')
    for directory, children, files in os.walk(base, followlinks=False, onerror=inaccessible):
        for path in [Path(directory), *(Path(directory) / name for name in files)]:
            if clock() - started > SECONDS:
                raise Refused('verification deadline')
            name = path.relative_to(base).as_posix()
            if name not in expected or name in seen:
                raise Refused('unexpected entry')
            seen.add(name)
            kind, value = expected[name]
            info = path.lstat()
            snapshots[path] = stamp(info)
            if info.st_uid != uid or info.st_mode & 0o7022 or path.suffix.lower() == '.pth':
                raise Refused('unsafe entry')
            if kind == 'dir':
                if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != value:
                    raise Refused('directory mismatch')
            else:
                if stat.S_IMODE(info.st_mode) != value['mode']:
                    raise Refused('file mode')
                observed = file_digest(path, value['size'], uid=uid, started=started, clock=clock)
                if observed != value['sha256']:
                    raise Refused('content mismatch')
        # os.walk does not visit linked directories: refuse here, never follow.
        for name in children:
            if (Path(directory) / name).is_symlink():
                raise Refused('linked directory')
        if len(seen) + len(children) > MAX_ENTRIES:
            raise Refused('entry budget')
    if seen != set(expected) or any(stamp(path.lstat()) != value for path, value in snapshots.items()):
        raise Refused('incomplete or changed tree')
    if clock() - started > SECONDS:
        raise Refused('verification deadline')
    return clock() - started


@contextlib.contextmanager
def locked(path=LOCK, *, exclusive=False, uid=0):
    # The lock inode is installed once by bootstrap; never unlink or replace.
    protected(path, uid=uid)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != uid:
            raise Refused('lock')
        deadline = time.monotonic() + SECONDS
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Refused('installation busy')
                time.sleep(0.02)
        if stamp(Path(path).lstat()) != stamp(info):
            raise Refused('lock replaced')
        yield
    finally:
        os.close(fd)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if (os.geteuid() != 0 or args not in ([], ['install'], ['rollback'])
            or not sys.flags.isolated or not sys.flags.no_site or not sys.flags.dont_write_bytecode):
        raise Refused('isolated root entry required')
    with locked(exclusive=bool(args)):
        data = read(PIN, MAX_DOCUMENT)
        document = json.loads(data, object_pairs_hook=unique)
        validate(document)
        if stat.S_IMODE(PIN.stat().st_mode) != 0o600:
            raise Refused('private pin required')
        if canonical(document) != data:
            raise Refused('canonical pin required')
        if document['machine_id'] != Path('/etc/machine-id').read_text().strip():
            raise Refused('host pin')
        elapsed = verify(document, BASE)
        # Fixed entries only. -S on the first interpreter excludes all site hooks.
        target = 'installer-local-handoff.py' if args else 'dispatch-local-handoff.py'
        result = subprocess.run([str(BASE / 'runtime/bin/python'), '-I', '-B',
                                 str(BASE / 'lib' / target), *args],
                                env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'},
                                timeout=90 if args else 65, check=False)
        print('runtime_seal_verified sha=%s seconds=%.3f' % (document['sha'], elapsed), file=sys.stderr)
        return result.returncode


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print('runtime_seal_unavailable', file=sys.stderr)
        sys.exit(1)
