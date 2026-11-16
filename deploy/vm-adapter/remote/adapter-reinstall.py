#!/usr/bin/python3 -I
# SPDX-License-Identifier: AGPL-3.0-only
"""#7570: R3, install only a root-enrolled, staging-attested release.

No caller configuration, environment, paths, Git checkout or stdin is used.
Install this file root:root 0755 as /usr/local/sbin/ameesh-vm-adapter-reinstall.
The root-only release proof enrollment is described in R3.md.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

BASE = Path('/opt/ameesh-vm-adapter')
PROOFS = Path('/etc/ameesh-vm-adapter/releases')
UNIT = Path('/etc/systemd/system/ameesh-vm-adapter.service')
SELF = Path('/usr/local/sbin/ameesh-vm-adapter-reinstall')
ORIGIN = 'https://github.com/manaty/ameesh.git'
EXPIRES = dt.datetime(2026, 12, 13, tzinfo=dt.timezone.utc)
TAG = re.compile(r'v(?:0|[1-9][0-9]*)[.](?:0|[1-9][0-9]*)[.](?:0|[1-9][0-9]*)\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')
ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/root',
       'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
       'GIT_TERMINAL_PROMPT': '0', 'GIT_ATTR_NOSYSTEM': '1',
       'PYTHONDONTWRITEBYTECODE': '1'}
TIMER = 'ameesh-vm-adapter.timer'
SERVICE = 'ameesh-vm-adapter.service'
MAX_JSON = 1024 * 1024
MAX_ARCHIVE = 128 * 1024 * 1024


class Refused(ValueError):
    pass


def trusted(path, *, directory=False):
    """All components must be real root-owned, non-writable-by-agent paths."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise Refused('absolute installed path required')
    for parent in reversed(path.parents):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022):
            raise Refused('unsafe installed parent')
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
            or (not directory and info.st_nlink != 1)):
        raise Refused('unsafe installed path')
    return path


def read_file(path, limit=MAX_JSON):
    trusted(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022 or info.st_nlink != 1 or info.st_size > limit):
            raise Refused('unsafe installed file')
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise Refused('installed file too large')
        return data


def request(args, now):
    if len(args) != 1 or len(args[0]) > 64 or not TAG.fullmatch(args[0]):
        raise Refused('one release tag vX.Y.Z required')
    if now >= EXPIRES:
        raise Refused('R3 expired after 2026-12-12 UTC')
    return args[0]


def attested(report, tag):
    """Native release report + native actif proof; enrollment supplies trust."""
    if (not isinstance(report, dict) or report.get('schema') != 'ameesh-release-cutoff/1'
            or report.get('project') != 'ameesh' or report.get('tag') != tag
            or report.get('result') not in ('released', 'reconciled')):
        raise Refused('release not attested')
    sha = report.get('release_sha')
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise Refused('full attested commit required')
    staging = report.get('staging')
    if not isinstance(staging, dict) or type(staging.get('exit')) is not int or staging['exit'] != 0:
        raise Refused('successful staging required')
    proof = staging.get('attestation')
    if (not isinstance(proof, dict) or proof.get('schema') != 'ameesh-staging-actif/1'
            or proof.get('sha') != sha):
        raise Refused('staging commit differs')
    hosts, reported = proof.get('hotes'), proof.get('reportes', {})
    if not isinstance(hosts, dict) or not isinstance(reported, dict):
        raise Refused('staging hosts required')
    optional = {'manaty-mesh-1', 'pc-smichea'}
    required = {'ameesh-agents-1', 'ameesh-agents-2', 'ameesh-agents-3'} | optional
    if (set(reported) - optional or set(hosts) & set(reported)
            or set(hosts) | set(reported) != required):
        raise Refused('staging coverage incomplete')
    for value in hosts.values():
        active = value.get('actif') if isinstance(value, dict) else None
        if (not isinstance(active, str) or not re.fullmatch(r'[0-9a-f]{7,40}', active)
                or not sha.startswith(active)):
            raise Refused('active host commit differs')
    for value in reported.values():
        if not isinstance(value, dict) or not isinstance(value.get('raison'), str) or not value['raison'].strip():
            raise Refused('reported host reason required')
    return sha


def run(argv, *, cwd=BASE, stdout=subprocess.PIPE, timeout=600):
    # Never execute a shell or import from the invoking user's cwd/environment.
    result = subprocess.run([str(a) for a in argv], cwd=cwd, env=ENV,
                            stdin=subprocess.DEVNULL, stdout=stdout,
                            stderr=subprocess.PIPE, timeout=timeout, check=False)
    if result.returncode:
        # Child stderr may include package/network credentials: do not echo it.
        raise Refused('fixed installation command failed: ' + Path(str(argv[0])).name)
    return result.stdout


def idle():
    for unit in (TIMER, SERVICE):
        state = run(['/usr/bin/systemctl', 'show', '--property=ActiveState',
                     '--value', unit], timeout=15)
        allowed = (b'inactive', b'failed') if unit == SERVICE else (b'inactive',)
        if state.strip() not in allowed:
            raise Refused('adapter timer and service must be inactive')


def unpack(archive, destination):
    """Extract Git regular files only: no links, devices, traversal or modes."""
    with tarfile.open(archive, 'r:') as stream:
        members = []
        total = 0
        for item in stream:
            members.append(item)
            path = PurePosixPath(item.name)
            total += item.size
            if (not path.parts or path.is_absolute() or '..' in path.parts
                    or not (item.isdir() or item.isfile()) or total > MAX_ARCHIVE
                    or len(members) > 50000):
                raise Refused('unsafe release archive')
        for item in members:
            target = destination / item.name
            if item.isdir():
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
            else:
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                # Exclusive create also rejects duplicate archive entries.
                with stream.extractfile(item) as source, target.open('xb') as out:
                    shutil.copyfileobj(source, out)
                target.chmod(0o755 if item.mode & 0o111 else 0o644)


def fetch(tag, sha, source, work):
    repo = work / 'repository.git'
    run(['/usr/bin/git', '-c', 'init.templateDir=', 'init', '--bare', repo], cwd=work)
    git = ['/usr/bin/git', '--git-dir=' + str(repo), '-c', 'core.hooksPath=/dev/null']
    run(git + ['fetch', '--no-tags', '--depth=1', ORIGIN, 'refs/tags/' + tag], cwd=work)
    actual = run(git + ['rev-parse', '--verify', 'FETCH_HEAD^{commit}'], cwd=work).decode().strip()
    if actual != sha:
        raise Refused('release tag commit differs from attestation')
    archive = work / 'source.tar'
    with archive.open('xb') as stream:
        run(git + ['archive', '--format=tar', sha], cwd=work, stdout=stream)
    if archive.stat().st_size > MAX_ARCHIVE:
        raise Refused('release archive too large')
    unpack(archive, source)


def check_unit(source):
    template = (source / 'deploy/vm-adapter/ameesh-vm-adapter.service').read_bytes()
    marker = b'User=REPLACE_WITH_EXISTING_OPERATOR_ACCOUNT'
    if template.count(marker) != 1 or read_file(UNIT) != template.replace(marker, b'User=agent'):
        raise Refused('installed service differs from release (except User=agent)')
    run(['/usr/bin/systemd-analyze', 'verify', UNIT], timeout=30)


def build(source, venv):
    run(['/usr/bin/python3', '-I', '-m', 'venv', venv])
    run([venv / 'bin/python', '-I', '-m', 'pip', '--isolated',
         '--disable-pip-version-check', '--no-cache-dir', 'install',
         '--index-url', 'https://pypi.org/simple', source])
    run([venv / 'bin/python', '-I', '-c', 'import ameesh.vm_adapter'])
    # Make the root-built artifacts readable/executable by the service account.
    for folder, dirs, files in os.walk(venv):
        Path(folder).chmod(0o755)
        for name in files:
            path = Path(folder) / name
            if not path.is_symlink():
                path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)


def previous_venv():
    link = BASE / 'venv'
    if not link.is_symlink():
        raise Refused('existing venv must be a versioned symlink')
    target = link.resolve(strict=True)
    if target.parent != BASE or not re.fullmatch(r'venv-[0-9a-f]{12}', target.name):
        raise Refused('existing venv outside enrolled installation')
    trusted(target, directory=True)
    return target


def point(link, target, work):
    temporary = work / link.name
    temporary.symlink_to(target.name)
    os.replace(temporary, link)
    fd = os.open(BASE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def install(tag):
    trusted(BASE, directory=True)
    trusted(PROOFS, directory=True)
    sha = attested(json.loads(read_file(PROOFS / (tag + '.json'))), tag)
    lock = BASE / '.r3.lock'
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        trusted(lock)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        idle()
        previous = previous_venv()
        source, venv = BASE / ('src-' + sha[:12]), BASE / ('venv-' + sha[:12])
        # Never re-use a partial build, path collision or previously altered tree.
        if os.path.lexists(source) or os.path.lexists(venv):
            raise Refused('versioned destination already exists; root inspection required')
        with tempfile.TemporaryDirectory(prefix='.r3-', dir=BASE) as directory:
            work = Path(directory)
            source.mkdir(mode=0o755)
            switched = False
            try:
                fetch(tag, sha, source, work)
                check_unit(source)
                build(source, venv)
                # Expiry/timer can change during a long network/build operation.
                request([tag], dt.datetime.now(dt.timezone.utc))
                idle()
                if previous_venv() != previous:
                    raise Refused('current venv changed during installation')
                point(BASE / 'venv-previous', previous, work)
                point(BASE / 'venv', venv, work)
                switched = True
                print('R3 installed %s %s; previous=%s; timer left inactive' %
                      (tag, sha, previous.name))
            finally:
                if not switched:
                    # Do not remove a target if the atomic switch succeeded but
                    # a subsequent fsync/output operation failed.
                    if (BASE / 'venv').resolve() != venv:
                        shutil.rmtree(source)
                        if venv.exists():
                            shutil.rmtree(venv)
    finally:
        os.close(fd)


def main(args=None):
    try:
        tag = request(sys.argv[1:] if args is None else args, dt.datetime.now(dt.timezone.utc))
        if os.getuid() != 0 or os.geteuid() != 0:
            raise Refused('R3 requires root')
        trusted(SELF)
        os.umask(0o022)
        os.chdir('/')
        install(tag)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        # No child stderr, secret/config content, stack trace or caller env.
        print('R3 refused: ' + (str(exc) if isinstance(exc, Refused) else type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
