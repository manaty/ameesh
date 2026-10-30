#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Build the release bootstrap runtime from one exact Git SHA (#4778).

    construction-runtime.py --clone REPOSITORY --sha SHA --output DIRECTORY

The released bootstrap expects a supplied `runtime` tree next to the `sources`
tree of `entree-operateur.py` (#4767). This command builds that runtime outside
root, in a new directory, without publishing anything:

* a fresh venv from `/usr/bin/python3`, with no pip in the final tree;
* the `ameesh` wheel built from the exact commit, never from checkout bytes;
* the wheel's complete dependency closure (`psycopg[binary]`, `psutil` and
  their transitive dependencies), pinned by version and SHA-256 in
  `runtime-pins.txt`, installed with `--require-hashes --only-binary=:all:`;
* the wheel build backend pinned in `runtime-build-pins.txt` and installed in
  a disposable tools venv, never in the runtime;
* a provenance receipt carrying the SHA, both pin-file digests, the Python
  version, the wheel digest and the measured tree digest.

The clone must be at exactly the requested full SHA and clean (tracked and
untracked bytes): the wheel and both pin files are read from `git archive` of
that commit, not from the working tree. There is no pip, download or
installation outside the new output directory, no root gesture and no
publication. The administrator stages the measured `runtime` tree; this
command does not choose a host, a destination or an authority.

The final runtime is read-only and measured with `runtime-release.py`'s
inventory (same 128 MiB / 20000-entry limits and refusals). Entry-point
shebangs are replaced by a fixed POSIX preamble that resolves the interpreter
beside the script: the tree is relocatable, works in the build directory and
after the administrator publishes it, and its bytes never contain the build
path. `pyvenv.cfg` is rewritten to the three stable keys (`home`,
`include-system-site-packages`, `version`) for the same reason.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile

_SPEC = importlib.util.spec_from_file_location(
    'construction_runtime_measure',
    Path(__file__).with_name('runtime-release.py'))
runtime = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runtime)

PYTHON = Path('/usr/bin/python3')
GIT = Path('/usr/bin/git')
SCRIPT_PREAMBLE = (
    b'#!/bin/sh\n'
    b"'''exec' \"$(dirname \"$(readlink -f \"$(command -v \"$0\" || printf '%s' \"$0\")\")\")/python\" \"$0\" \"$@\"\n"
    b"' '''\n"
)
PINS_NAME = 'deploy/mise-a-jour/runtime-pins.txt'
BUILD_PINS_NAME = 'deploy/mise-a-jour/runtime-build-pins.txt'
RECEIPT = 'receipt.json'
MAX_PIN_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 200000
SHA = re.compile(r'[0-9a-f]{40}\Z')
HEX256 = re.compile(r'[0-9a-f]{64}\Z')
REQUIREMENT = re.compile(
    r'([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[([A-Za-z0-9._,-]+)\])?'
    r'==([A-Za-z0-9][A-Za-z0-9.+!_~-]*)\Z')
HASH_OPTION = re.compile(r'--hash=sha256:([0-9a-f]{64})\Z')
CLEAN_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')
WHEEL_NAME = re.compile(r'ameesh-([^-]+)-[^-]+-[^-]+-[^-]+\.whl\Z')


class BuildError(ValueError):
    pass


def canonical_name(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def canonical(document):
    return json.dumps(document, sort_keys=True, separators=(',', ':'))


def _regular_bytes(path, limit):
    """Read one owned regular file without following a link or racing it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise BuildError('regular file unavailable: %s' % Path(path).name) from None
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise BuildError('regular file required: %s' % Path(path).name)
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if (len(data) != before.st_size or len(data) > limit
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise BuildError('file changed while read: %s' % Path(path).name)
    return data


def read_pins(path, *, kind):
    """Parse the repository's hash-pinned requirements and hash the exact bytes.

    Only exact `name==version` requirements with at least one SHA-256 hash are
    accepted. Options, URLs, editables, markers and unpinned lines are refused
    here as well as by pip's `--require-hashes`, so a malformed pin file fails
    before the first download with a precise error.
    """
    if kind not in ('runtime', 'build'):
        raise BuildError('unknown pin file kind')
    data = _regular_bytes(path, MAX_PIN_BYTES)
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise BuildError('UTF-8 pin file required') from None
    if '\x00' in text:
        raise BuildError('binary pin file refused')
    logical, pending = [], ''
    for raw in text.splitlines():
        line = (pending + raw) if pending else raw
        if line.rstrip().endswith('\\'):
            pending = line.rstrip()[:-1] + ' '
            continue
        pending = ''
        logical.append(line)
    if pending:
        raise BuildError('dangling continuation in pin file')
    records = {}
    for line in logical:
        line = re.sub(r'(^|\s)#.*$', r'\1', line).strip()
        if not line:
            continue
        if (line.startswith('-') or '://' in line or '@' in line
                or ';' in line or line.endswith('\\')):
            raise BuildError('unhashed or non-exact pin required')
        tokens = line.split()
        match = REQUIREMENT.fullmatch(tokens[0])
        if match is None:
            raise BuildError('exact name==version pin required')
        name, extras, version = match.groups()
        hashes = []
        for token in tokens[1:]:
            digest = HASH_OPTION.fullmatch(token)
            if digest is None:
                raise BuildError('only --hash=sha256 options are accepted')
            hashes.append(digest.group(1))
        if not hashes:
            raise BuildError('at least one SHA-256 hash is required per pin')
        name = canonical_name(name)
        if name in records:
            raise BuildError('duplicate pin: %s' % name)
        records[name] = dict(name=name, extras=extras, version=version, hashes=hashes)
    if not records:
        raise BuildError('empty pin file refused')
    if kind == 'runtime':
        missing = {'psycopg', 'psycopg-binary', 'psutil'} - set(records)
        if missing:
            raise BuildError('runtime pins must cover psycopg[binary], psycopg-binary and psutil')
    elif 'setuptools' not in records:
        raise BuildError('build pins must pin setuptools')
    return dict(records=records, digest=hashlib.sha256(data).hexdigest(), bytes=len(data))


def _base_env(home):
    return {
        'PATH': '/usr/bin:/bin',
        'HOME': str(home),
        'LANG': 'C',
        'LC_ALL': 'C',
        'TZ': 'UTC',
        'PYTHONHASHSEED': '0',
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONNOUSERSITE': '1',
    }


def _git_env(home):
    env = _base_env(home)
    env.update(GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0',
               GIT_OPTIONAL_LOCKS='0')
    return env


def _pip_env(home, epoch):
    env = _base_env(home)
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_NO_INPUT='1',
               PIP_DISABLE_PIP_VERSION_CHECK='1',
               TMPDIR=str(home / 'tmp'), SOURCE_DATE_EPOCH=str(epoch))
    return env


def _run(argv, *, env, timeout):
    try:
        result = subprocess.run(
            argv, env=env, cwd=None, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
            check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise BuildError('command unavailable: %s' % Path(argv[0]).name) from None
    if result.returncode != 0:
        lines = result.stderr.decode('utf-8', 'replace').strip().splitlines()
        detail = ' '.join(lines[-3:])[:600]
        raise BuildError('%s failed%s' % (
            Path(argv[0]).name, (': ' + detail) if detail else ''))
    return result.stdout


def _git(repo, *arguments, env, timeout=180):
    return _run([str(GIT), '-C', str(repo), *arguments], env=env, timeout=timeout)


def verify_clone(repo, sha):
    """Refuse anything but a clean clone whose HEAD is exactly `sha`."""
    if SHA.fullmatch(sha) is None:
        raise BuildError('full lowercase 40-hex SHA required')
    repo = Path(repo).absolute()
    if not repo.is_dir():
        raise BuildError('clone directory required')
    if not GIT.is_file():
        raise BuildError('git unavailable')
    env = _git_env(repo)
    top = _git(repo, 'rev-parse', '--show-toplevel', env=env).decode().strip()
    if not top or Path(top).resolve() != repo.resolve():
        raise BuildError('clone root required')
    head = _git(repo, 'rev-parse', '--verify', 'HEAD^{commit}', env=env).decode().strip()
    if head != sha:
        raise BuildError('clone HEAD is not exactly the requested SHA')
    changed = _git(repo, 'status', '--porcelain=v1', '--untracked-files=all', env=env)
    if changed.strip():
        raise BuildError('clone must be clean, tracked and untracked files included')
    stamp = _git(repo, 'show', '-s', '--format=%ct', sha, env=env).decode().strip()
    try:
        epoch = int(stamp)
    except ValueError:
        raise BuildError('commit timestamp unavailable') from None
    if epoch <= 0:
        raise BuildError('commit timestamp unavailable')
    return epoch


def _archive(repo, sha, destination, *, env):
    build_dir = destination.parent
    archive = build_dir / 'source.tar'
    _run([str(GIT), '-C', str(repo), 'archive', '--format=tar',
          '--output=' + str(archive), sha], env=env, timeout=600)
    try:
        size = archive.stat().st_size
    except OSError:
        raise BuildError('source archive unavailable') from None
    if size > MAX_ARCHIVE_BYTES:
        raise BuildError('source archive exceeds the byte budget')
    return archive


def extract_archive(archive, destination):
    """Extract regular files and directories only, with bounded sizes."""
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise BuildError('new source directory required')
    destination.mkdir(mode=0o700)
    seen, count, total = set(), 0, 0
    with tarfile.open(archive, 'r:') as tar:
        for member in tar:
            count += 1
            if count > MAX_ARCHIVE_ENTRIES:
                raise BuildError('source archive entry budget exceeded')
            name = member.name
            pure = PurePosixPath(name)
            if (not name or pure.is_absolute() or '..' in pure.parts
                    or name.startswith('/') or '\\' in name):
                raise BuildError('unsafe source archive path')
            target = destination.joinpath(*pure.parts)
            if member.isdir():
                target.mkdir(parents=True, mode=0o700, exist_ok=True)
                continue
            if not member.isfile():
                raise BuildError('links and special files refused in source archive')
            if name in seen:
                raise BuildError('duplicate source archive path')
            seen.add(name)
            total += member.size
            if total > MAX_ARCHIVE_BYTES:
                raise BuildError('source archive byte budget exceeded')
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            stream = tar.extractfile(member)
            if stream is None:
                raise BuildError('source archive entry unavailable')
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as output:
                remaining = member.size
                while remaining:
                    part = stream.read(min(65536, remaining))
                    if not part:
                        raise BuildError('truncated source archive entry')
                    output.write(part)
                    remaining -= len(part)
                if stream.read(1):
                    raise BuildError('oversized source archive entry')
            os.chmod(target, 0o755 if member.mode & 0o100 else 0o644)
    return destination


def extract_source(repo, sha, destination, *, env):
    """Extract the exact commit with regular files and directories only."""
    if destination.exists() or destination.is_symlink():
        raise BuildError('new source directory required')
    archive = _archive(repo, sha, destination, env=env)
    try:
        return extract_archive(archive, destination)
    finally:
        try:
            archive.unlink()
        except OSError:
            pass


def create_venv(path, *, python, with_pip, env):
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise BuildError('new venv directory required')
    argv = [str(python), '-m', 'venv']
    if not with_pip:
        argv.append('--without-pip')
    argv.append(str(path))
    _run(argv, env=env, timeout=300)
    if not (path / 'bin' / 'python').exists():
        raise BuildError('venv interpreter missing')


def pip(tools, *arguments, env):
    return _run([str(tools / 'bin' / 'python'), '-m', 'pip', '--isolated',
                 '--disable-pip-version-check', '--no-input', *arguments],
                env=env, timeout=900)


def install_build_pins(tools, pins_path, *, env):
    pip(tools, 'install', '--require-hashes', '--only-binary=:all:',
        '--no-cache-dir', '-r', str(pins_path), env=env)


def install_runtime_pins(tools, runtime_dir, pins_path, *, env):
    pip(tools, '--python', str(runtime_dir / 'bin' / 'python'), 'install',
        '--require-hashes', '--only-binary=:all:', '--no-compile',
        '--no-cache-dir', '-r', str(pins_path), env=env)


def build_wheel(tools, source, wheelhouse, *, env):
    pip(tools, 'wheel', '--no-index', '--no-deps', '--no-build-isolation',
        '--no-cache-dir', '--wheel-dir', str(wheelhouse), str(source), env=env)
    wheels = sorted(wheelhouse.glob('ameesh-*.whl'))
    if len(wheels) != 1:
        raise BuildError('exactly one ameesh wheel required')
    wheel = wheels[0]
    match = WHEEL_NAME.fullmatch(wheel.name)
    if match is None:
        raise BuildError('unexpected ameesh wheel name')
    data = _regular_bytes(wheel, MAX_ARCHIVE_BYTES)
    return dict(path=wheel, name=wheel.name, version=match.group(1),
                sha256=hashlib.sha256(data).hexdigest())


def install_wheel(tools, runtime_dir, wheel, *, env):
    pip(tools, '--python', str(runtime_dir / 'bin' / 'python'), 'install',
        '--no-index', '--find-links', str(wheel['path'].parent), '--no-deps',
        '--no-compile', '--no-cache-dir', 'ameesh==' + wheel['version'], env=env)


def _target(runtime_dir, code, *, env, arguments=()):
    return _run([str(runtime_dir / 'bin' / 'python'), '-I', '-B', '-c', code,
                 *arguments], env=env, timeout=120)


def check_runtime(tools, runtime_dir, pins, wheel, *, env):
    """Every pinned distribution, the wheel and the import closure must agree."""
    names = sorted(pins['records']) + ['ameesh']
    code = ('import importlib.metadata as metadata, json, sys\n'
            'print(json.dumps({name: metadata.version(name) for name in sys.argv[1:]}))\n')
    observed = json.loads(_target(runtime_dir, code, env=env, arguments=names))
    if not isinstance(observed, dict) or set(observed) != set(names):
        raise BuildError('installed distribution inventory unavailable')
    for name in pins['records']:
        if observed[name] != pins['records'][name]['version']:
            raise BuildError('installed version differs from the pin: %s' % name)
    if observed['ameesh'] != wheel['version']:
        raise BuildError('installed wheel version differs from the build')
    pip(tools, '--python', str(runtime_dir / 'bin' / 'python'), 'check', env=env)
    _target(runtime_dir,
            'import ameesh, psycopg, psutil\n'
            'print(ameesh.__version__, psycopg.__version__, psutil.__version__)\n',
            env=env)


def _real_python(python):
    base = Path(python).resolve()
    if not base.is_file() or not os.access(base, os.X_OK):
        raise BuildError('system python interpreter required')
    return base


def materialize(runtime_dir, *, python, env):
    """Replace the venv links with a concrete interpreter and stable config."""
    runtime_dir = Path(runtime_dir)
    base = _real_python(python)
    lib64 = runtime_dir / 'lib64'
    if lib64.is_symlink():
        lib64.unlink()
    elif lib64.exists():
        raise BuildError('unexpected lib64 directory')
    bin_dir = runtime_dir / 'bin'
    for name in list(bin_dir.iterdir()):
        if re.fullmatch(r'python[0-9.]*', name.name):
            if name.is_dir() and not name.is_symlink():
                raise BuildError('unexpected interpreter directory')
            name.unlink()
    shutil.copyfile(base, bin_dir / 'python')
    (bin_dir / 'python').chmod(0o755)
    for name in ('activate', 'activate.csh', 'activate.fish', 'Activate.ps1'):
        candidate = bin_dir / name
        if candidate.is_file() and not candidate.is_symlink():
            candidate.unlink()
    include = runtime_dir / 'include'
    if include.is_dir() and not include.is_symlink():
        shutil.rmtree(include)
    elif include.exists():
        raise BuildError('unexpected include link')

    probe = ('import platform, sys\n'
             'print(sys.prefix)\n'
             'print(sys._base_executable)\n'
             'print(platform.python_version())\n')
    first = _target(runtime_dir, probe, env=env).decode().splitlines()
    if len(first) != 3 or Path(first[0]).resolve() != runtime_dir.resolve():
        raise BuildError('venv prefix mismatch')
    if Path(first[1]).resolve().parent != base.parent:
        raise BuildError('venv base interpreter outside the requested python')
    if re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+_-]*', first[2]) is None:
        raise BuildError('unexpected python version')
    version = first[2]
    (runtime_dir / 'pyvenv.cfg').write_text(
        'home = %s\ninclude-system-site-packages = false\nversion = %s\n'
        % (base.parent, version), encoding='ascii')
    second = _target(runtime_dir, probe, env=env).decode().splitlines()
    if len(second) != 3 or Path(second[0]).resolve() != runtime_dir.resolve():
        raise BuildError('materialized venv prefix mismatch')
    if Path(second[1]).resolve().parent != base.parent or second[2] != version:
        raise BuildError('materialized venv configuration mismatch')
    return version


def rebase_script(data):
    """Replace one pip entry point's shebang with the relocatable preamble."""
    if not data.startswith(b'#!'):
        raise BuildError('entry point without a shebang')
    lines = data.splitlines(keepends=True)
    if lines[0].rstrip(b'\n') == b'#!/bin/sh':
        if (len(lines) < 3 or not lines[1].startswith(b"'''exec'")
                or lines[2].strip() != b"' '''"):
            raise BuildError('unexpected entry point preamble')
        body = b''.join(lines[3:])
    else:
        body = b''.join(lines[1:])
    return SCRIPT_PREAMBLE + body


def _record_digest(data):
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')


def rewrite_scripts(runtime_dir):
    """Rebase installed entry points and keep their RECORD hashes exact."""
    runtime_dir = Path(runtime_dir)
    dist_infos = sorted((runtime_dir / 'lib').glob('python*/site-packages/ameesh-*.dist-info'))
    if len(dist_infos) != 1:
        raise BuildError('exactly one ameesh dist-info required')
    dist_info = dist_infos[0]
    record_path = dist_info / 'RECORD'
    text = _regular_bytes(record_path, MAX_PIN_BYTES).decode('utf-8')
    rows = [row for row in csv.reader(io.StringIO(text)) if row]
    scripts, kept = set(), []
    for row in rows:
        if len(row) != 3:
            raise BuildError('unexpected RECORD row')
        relative, digest, size = row
        if relative == 'direct_url.json':
            candidate = dist_info / relative
            if candidate.exists() or candidate.is_symlink():
                candidate.unlink()
            continue
        kept.append(row)
        if relative.startswith('../../../bin/'):
            name = relative.rsplit('/', 1)[1]
            if not CLEAN_NAME.fullmatch(name) or name == 'python':
                raise BuildError('unexpected entry point name')
            scripts.add(name)
    bin_dir = runtime_dir / 'bin'
    for name in sorted(scripts):
        script = bin_dir / name
        data = _regular_bytes(script, MAX_PIN_BYTES)
        data = rebase_script(data)
        script.write_bytes(data)
        for row in kept:
            if row[0] == '../../../bin/' + name:
                row[1] = 'sha256=' + _record_digest(data)
                row[2] = str(len(data))
    for name in sorted(scripts):
        if not CLEAN_NAME.fullmatch(name):
            raise BuildError('unexpected entry point name')
    for item in sorted(bin_dir.iterdir()):
        if item.name == 'python':
            continue
        if not item.is_file() or item.is_symlink():
            raise BuildError('unexpected runtime bin entry')
        if item.name not in scripts:
            raise BuildError('unrecorded runtime executable refused')
    output = io.StringIO()
    writer = csv.writer(output, lineterminator='\n')
    for row in kept:
        writer.writerow(row)
    with open(record_path, 'w', encoding='utf-8', newline='') as stream:
        stream.write(output.getvalue())
    return scripts


def sanitize(runtime_dir):
    """Remove build artifacts and refuse path injection before measurement."""
    runtime_dir = Path(runtime_dir)
    for directory, dirs, files in os.walk(runtime_dir, topdown=False, followlinks=False):
        path = Path(directory)
        for name in files:
            candidate = path / name
            if candidate.is_symlink():
                raise BuildError('symlink refused in runtime tree')
            if candidate.suffix == '.pth':
                raise BuildError('path injection file refused')
            if candidate.suffix == '.pyc':
                candidate.unlink()
        for name in dirs:
            candidate = path / name
            if candidate.is_symlink():
                raise BuildError('symlink refused in runtime tree')
            if name == '__pycache__':
                shutil.rmtree(candidate)
    for direct in runtime_dir.rglob('direct_url.json'):
        if direct.is_file() and not direct.is_symlink():
            direct.unlink()
    for directory, dirs, files in os.walk(runtime_dir, followlinks=False):
        for name in [*dirs, *files]:
            candidate = Path(directory) / name
            if candidate.is_symlink():
                raise BuildError('symlink refused in runtime tree')
            info = candidate.lstat()
            if info.st_uid != os.geteuid():
                raise BuildError('runtime tree must be owned by the builder')
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise BuildError('special file refused in runtime tree')
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise BuildError('hardlinked runtime file refused')
            if info.st_mode & 0o7000:
                raise BuildError('special mode refused in runtime tree')
    if any(runtime_dir.rglob('*.pth')):
        raise BuildError('path injection file refused')
    rewrite_scripts(runtime_dir)


def seal_tree(root):
    root = Path(root)
    for directory, dirs, files in os.walk(root, topdown=False, followlinks=False):
        for name in files:
            path = Path(directory) / name
            info = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(info.st_mode):
                raise BuildError('regular runtime file required')
            path.chmod(0o555 if info.st_mode & 0o111 else 0o444)
        if Path(directory).is_symlink():
            raise BuildError('runtime directory symlink refused')
        Path(directory).chmod(0o555)
    root.chmod(0o555)


def receipt(runtime_dir, *, sha, pins, build_pins, wheel, python_version, output):
    try:
        inventory = runtime.inventory(runtime_dir)
    except (runtime.RuntimeError, OSError, TypeError, ValueError):
        raise BuildError('measured runtime tree refused') from None
    document = {
        'version': 1,
        'sha': sha,
        'python': python_version,
        'pins_sha256': pins['digest'],
        'build_pins_sha256': build_pins['digest'],
        'wheel': {'name': wheel['name'], 'sha256': wheel['sha256']},
        'tree_sha256': inventory['digest'],
        'tree_bytes': inventory['bytes'],
        'tree_files': len(inventory['files']),
        'tree_dirs': len(inventory['dirs']),
    }
    validate_receipt(document)
    path = Path(output) / RECEIPT
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
    with os.fdopen(fd, 'wb') as stream:
        stream.write((canonical(document) + '\n').encode('utf-8'))
        stream.flush()
        os.fsync(stream.fileno())
    return document


def validate_receipt(document):
    keys = {'version', 'sha', 'python', 'pins_sha256', 'build_pins_sha256',
            'wheel', 'tree_sha256', 'tree_bytes', 'tree_files', 'tree_dirs'}
    if not isinstance(document, dict) or set(document) != keys:
        raise BuildError('exact receipt fields required')
    if type(document['version']) is not int or document['version'] != 1:
        raise BuildError('unsupported receipt version')
    if not isinstance(document['sha'], str) or SHA.fullmatch(document['sha']) is None:
        raise BuildError('receipt SHA')
    if (not isinstance(document['python'], str)
            or re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+_-]*', document['python']) is None):
        raise BuildError('receipt Python version')
    for field in ('pins_sha256', 'build_pins_sha256', 'tree_sha256'):
        if not isinstance(document[field], str) or HEX256.fullmatch(document[field]) is None:
            raise BuildError('receipt digest')
    wheel = document['wheel']
    if (not isinstance(wheel, dict) or set(wheel) != {'name', 'sha256'}
            or not isinstance(wheel['name'], str) or WHEEL_NAME.fullmatch(wheel['name']) is None
            or not isinstance(wheel['sha256'], str) or HEX256.fullmatch(wheel['sha256']) is None):
        raise BuildError('receipt wheel')
    for field in ('tree_bytes', 'tree_files', 'tree_dirs'):
        if type(document[field]) is not int or document[field] < 0:
            raise BuildError('receipt tree count')
    if document['tree_files'] <= 0 or document['tree_dirs'] <= 0:
        raise BuildError('empty runtime tree refused')
    return document


def _remove(path):
    def make_writable(function, target, _error):
        try:
            os.chmod(target, 0o700)
            function(target)
        except OSError:
            raise
    shutil.rmtree(path, onerror=make_writable)


def build(repo, sha, output, *, python=PYTHON):
    """Build and measure one runtime; return its provenance receipt."""
    if os.geteuid() == 0:
        raise BuildError('build must run outside root')
    if SHA.fullmatch(sha) is None:
        raise BuildError('full lowercase 40-hex SHA required')
    repo = Path(repo).absolute()
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise BuildError('new output directory required')
    if not output.parent.is_dir():
        raise BuildError('existing output parent required')
    try:
        runtime.trusted(output.parent)
    except runtime.RuntimeError:
        raise BuildError('output parent must not be group/world writable') from None
    if output == repo or repo in output.parents or output in repo.parents:
        raise BuildError('output must be separate from the clone')
    epoch = verify_clone(repo, sha)
    output.mkdir(mode=0o700)
    build_dir = output / '.build'
    build_dir.mkdir(mode=0o700)
    home = build_dir / 'home'
    home.mkdir(mode=0o700)
    (home / 'tmp').mkdir(mode=0o700)
    git_env = _git_env(home)
    pip_env = _pip_env(home, epoch)
    source = build_dir / 'source'
    extract_source(repo, sha, source, env=git_env)
    pins = read_pins(source / PINS_NAME, kind='runtime')
    build_pins = read_pins(source / BUILD_PINS_NAME, kind='build')
    tools = build_dir / 'tools'
    create_venv(tools, python=python, with_pip=True, env=pip_env)
    install_build_pins(tools, source / BUILD_PINS_NAME, env=pip_env)
    wheels = build_dir / 'wheels'
    wheels.mkdir(mode=0o700)
    wheel = build_wheel(tools, source, wheels, env=pip_env)
    runtime_dir = output / 'runtime'
    create_venv(runtime_dir, python=python, with_pip=False, env=pip_env)
    install_runtime_pins(tools, runtime_dir, source / PINS_NAME, env=pip_env)
    install_wheel(tools, runtime_dir, wheel, env=pip_env)
    version = materialize(runtime_dir, python=python, env=pip_env)
    sanitize(runtime_dir)
    check_runtime(tools, runtime_dir, pins, wheel, env=pip_env)
    seal_tree(runtime_dir)
    document = receipt(runtime_dir, sha=sha, pins=pins, build_pins=build_pins,
                       wheel=wheel, python_version=version, output=output)
    _remove(build_dir)
    output.chmod(0o755)
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Build the measured release runtime from one exact SHA.')
    parser.add_argument('--clone', required=True, help='clean Git clone at --sha')
    parser.add_argument('--sha', required=True, help='full lowercase commit SHA')
    parser.add_argument('--output', required=True,
                        help='new directory, outside root and group-writable parents')
    arguments = parser.parse_args(argv)
    try:
        document = build(arguments.clone, arguments.sha, arguments.output)
    except (BuildError, OSError) as error:
        print('construction-runtime: %s' % error, file=sys.stderr)
        if (Path(arguments.output) / '.build').is_dir():
            print('construction-runtime: partial output is retained for inspection',
                  file=sys.stderr)
        return 2
    print('runtime: %s' % (Path(arguments.output) / 'runtime'))
    print('receipt: %s' % (Path(arguments.output) / RECEIPT))
    print('tree: %s' % document['tree_sha256'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
