#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#5655: prefill the runner's read-only Linux action archive cache.

Runner v2.338.0 ActionManager.cs consumes <owner>_<repo>/<resolved-sha>.tar.gz.
Only immutable workflow references are admitted. No archive is extracted here.
Run as root on the CI VM; jobs must never be able to publish into this cache.
"""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import tempfile

USES = re.compile(r'\buses:\s*[\'"]?([\w-]+/[\w.-]+)(?:/[\w./-]+)?@([\w.-]+)')


def actions(workflows):
    result = set()
    for path in Path(workflows).glob('*.y*ml'):
        for repo, ref in USES.findall(path.read_text()):
            if not re.fullmatch('[0-9a-f]{40}', ref):
                raise ValueError(f'{path.name}: action {repo}@{ref} is not pinned to a SHA')
            result.add((repo, ref))
    if not result:
        raise ValueError('no pinned workflow actions found')
    return sorted(result)


def safe_directory(path):
    path = Path(os.path.abspath(path))
    for parent in [*reversed(path.parents), path]:
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022 or info.st_uid not in (0, os.geteuid()):
                raise ValueError(f'unsafe cache directory: {parent}')
        else:
            parent.mkdir(mode=0o755)
    return path


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def prefill(workflows, cache):
    pinned = actions(workflows)
    cache = safe_directory(cache)
    # Root owns both the lock and the archive tree. Jobs only read it.
    with (cache / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for repo, sha in pinned:
            folder = safe_directory(cache / repo.replace('/', '_'))
            target = folder / f'{sha}.tar.gz'
            checksum = folder / f'{sha}.sha256'
            if target.is_symlink() or checksum.is_symlink():
                raise ValueError('symlink in action cache')
            if target.is_file() and checksum.is_file() and digest(target) == checksum.read_text().strip():
                continue
            # Download privately, check the archive before publishing it. A
            # failed/interrupted transfer never becomes a cache hit.
            with tempfile.TemporaryDirectory(prefix='ameesh-actions-') as temp:
                archive = Path(temp) / 'archive.tar.gz'
                subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error',
                                '--retry', '4', '--retry-delay', '3', '--max-time', '120',
                                '--retry-max-time', '180', '--max-filesize', '52428800',
                                '--output', str(archive),
                                f'https://codeload.github.com/{repo}/tar.gz/{sha}'], check=True)
                with tarfile.open(archive) as contents:
                    if not contents.getmembers():
                        raise ValueError('empty action archive')
                payload = archive.read_bytes()
                # Same-directory atomic publication, inaccessible for writes
                # to the guest user. Shared readers always see a whole archive.
                partial = folder / f'{sha}.partial'
                with partial.open('wb') as stream:
                    stream.write(payload)
                partial.chmod(0o644)
                os.replace(partial, target)
                checksum.write_text(hashlib.sha256(payload).hexdigest() + '\n')
                checksum.chmod(0o644)
            print(f'cached {repo}@{sha}')


def check(workflows, cache):
    cache = Path(cache)
    if not cache.is_dir():
        raise ValueError('action cache is absent')
    safe_directory(cache)
    for repo, sha in actions(workflows):
        folder = cache / repo.replace('/', '_')
        if not folder.is_dir():
            raise ValueError(f'missing cache folder: {repo}')
        safe_directory(folder)
        archive = folder / f'{sha}.tar.gz'
        checksum = folder / f'{sha}.sha256'
        if (archive.is_symlink() or checksum.is_symlink()
                or not archive.is_file() or not checksum.is_file()
                or digest(archive) != checksum.read_text().strip()):
            raise ValueError(f'missing or corrupt action archive: {repo}@{sha}')
    print('action archive cache: all current workflow pins present')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workflows', required=True)
    parser.add_argument('--cache', required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    (check if args.check else prefill)(args.workflows, args.cache)
