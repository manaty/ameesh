#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded content-pinned tree publication for an administrator's bootstrap.

No downloads, pip, archive extraction, executable probing or privileged CLI.
This pins supplied bytes; external toolchain libraries need separate enrollment.
"""
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import stat

_spec = importlib.util.spec_from_file_location('runtime_protocol',
        Path(__file__).with_name('controleur-release.py'))
protocol = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(protocol)


class RuntimeError(ValueError):
    pass


def trusted(path):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise RuntimeError('tree traversal refused')
    for ancestor in (path, *path.parents):
        info = ancestor.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid not in (0,os.geteuid())
                or info.st_mode & 0o022):
            raise RuntimeError('untrusted tree path')
    return path


def stamp(info):
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def digest_file(path, *, output=None, remaining):
    try:
        fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except OSError:
        raise RuntimeError('tree file unavailable') from None
    with os.fdopen(fd,'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or before.st_mode & 0o022
                or before.st_mode & 0o7000 or before.st_size > remaining):
            raise RuntimeError('unsafe or oversized tree file')
        hashed,size = hashlib.sha256(),0
        while True:
            part = stream.read(min(65536,remaining-size+1))
            if not part:
                break
            size += len(part)
            if size > remaining:
                raise RuntimeError('tree byte budget exceeded')
            hashed.update(part)
            if output is not None:
                output.write(part)
        if size != before.st_size or stamp(before) != stamp(os.fstat(stream.fileno())):
            raise RuntimeError('tree changed during read')
    return dict(size=size,sha256=hashed.hexdigest(),mode=0o555 if before.st_mode & 0o111 else 0o444)


def inventory(root, *, max_bytes=128*1024*1024, max_files=20000):
    if type(max_bytes) is not int or max_bytes<=0 or type(max_files) is not int or max_files<=0:
        raise RuntimeError('explicit positive tree budgets required')
    root = trusted(root)
    if not root.is_dir() or root.stat().st_uid != os.geteuid():
        raise RuntimeError('owned tree directory required')
    files,dirs,stamps,size = {},[],{},0
    def inaccessible(error):
        raise RuntimeError('tree traversal unavailable') from None
    for directory,children,names in os.walk(root,followlinks=False,onerror=inaccessible):
        path = Path(directory)
        before = path.lstat()
        if (not stat.S_ISDIR(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_mode & 0o022 or before.st_mode & 0o7000):
            raise RuntimeError('unsafe tree directory')
        relative = path.relative_to(root).as_posix()
        dirs.append(relative)
        stamps[path] = stamp(before)
        # Bound empty directories as well as files.
        if len(dirs)+len(files)+len(children)+len(names)>max_files:
            raise RuntimeError('tree entry budget exceeded')
        for name in children:
            if (path/name).is_symlink():
                raise RuntimeError('linked tree directory refused')
        for name in sorted(names):
            file = path/name
            info = digest_file(file,remaining=max_bytes-size)
            files[file.relative_to(root).as_posix()] = info
            size += info['size']
    if not files:
        raise RuntimeError('empty runtime tree refused')
    if any(stamp(path.lstat()) != before for path,before in stamps.items()):
        raise RuntimeError('tree changed during inventory')
    result = dict(version=1,dirs=sorted(dirs),files=files,bytes=size)
    return dict(result,digest=protocol.identity(result))


def code_digest(directory):
    """Digest of the installed Python companions a backend can load."""
    items = {}
    for path in sorted(Path(directory).iterdir()):
        if path.suffix == '.py' and path.is_file() and not path.is_symlink():
            items[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return protocol.identity(items)


def fsync_directory(path):
    fd = os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(source,target,journal,operation,expected_digest, *, authorize=None,
            max_bytes=128*1024*1024,max_files=20000):
    """Copy with no overwrite; reconcile a crash/lost reply after final rename.

    authorize must independently revalidate signed bootstrap scope. It is called
    after slow inventory/copy/lock waits and before publication/receipt replay.
    A digest, root uid or local receipt is not approval. There is no CLI that
    can supply an authorization boolean or a caller-selected destination.
    Existing unresolved partial stages require inspection; they are never deleted
    or guessed complete. Final destinations are adopted only with our own intent.
    """
    if (not isinstance(operation,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',operation)
            or not isinstance(expected_digest,str) or not re.fullmatch(r'[0-9a-f]{64}',expected_digest)):
        raise RuntimeError('typed tree operation required')
    def authority():
        try:
            allowed = callable(authorize) and authorize() is True
        except Exception:
            allowed = False
        if not allowed:
            raise RuntimeError('tree publication authority unavailable')
    authority()
    source = Path(source).absolute()
    target = Path(target).absolute()
    parent = trusted(target.parent)
    if source == target or source in target.parents or target in source.parents:
        raise RuntimeError('source and destination must be separate trees')
    request = dict(operation=operation,target=str(target),digest=expected_digest)
    lock_id = protocol.identity(str(target))
    with protocol.locked(journal,lock_id) as directory:
        path = directory/(operation+'.json')
        stage = parent/('.'+target.name+'-'+lock_id[:24]+'.pending')
        exists = path.exists() or path.is_symlink()
        if exists:
            record = protocol.load(path)
            if record.get('request') != request or set(record) != {'request','receipt'}:
                raise RuntimeError('tree intent mismatch')
            if target.exists() or target.is_symlink():
                completed = inventory(target,max_bytes=max_bytes,max_files=max_files)
                if completed['digest'] != expected_digest:
                    raise RuntimeError('published tree changed')
                # All installed files/directories must retain read-only modes.
                for relative in completed['dirs']+list(completed['files']):
                    if (target/relative).stat().st_mode & 0o222:
                        raise RuntimeError('published tree became writable')
                receipt = dict(request,ok=True)
                if (record['receipt'] is not None
                        and protocol.identity(record['receipt']) != protocol.identity(receipt)):
                    raise RuntimeError('tree receipt mismatch')
                if record['receipt'] is None:
                    authority()
                    protocol.save(path,dict(request=request,receipt=receipt))
                authority()
                return receipt
            if record['receipt'] is not None or stage.exists() or stage.is_symlink():
                raise RuntimeError('tree publication unresolved; inspect retained stage')
        else:
            if target.exists() or target.is_symlink() or stage.exists() or stage.is_symlink():
                raise RuntimeError('unowned tree destination exists')
        observed = inventory(source,max_bytes=max_bytes,max_files=max_files)
        if observed['digest'] != expected_digest:
            raise RuntimeError('enrolled tree digest mismatch')
        space = os.statvfs(parent)
        if space.f_bavail*space.f_frsize < observed['bytes']+1024*1024:
            raise RuntimeError('tree staging capacity unavailable')
        authority()
        if not exists:
            protocol.save(path,dict(request=request,receipt=None))
        stage.mkdir(mode=0o700)
        # Never recursively remove a stage on error. The intent and retained
        # bytes establish what happened, including failed admission/copy.
        for relative in observed['dirs']:
            if relative != '.':
                (stage/relative).mkdir(mode=0o700)
        for relative,expected in sorted(observed['files'].items()):
            output = stage/relative
            fd = os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as stream:
                actual = digest_file(source/relative,output=stream,remaining=expected['size'])
                if actual != expected:
                    raise RuntimeError('tree changed before copy')
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(),expected['mode'])
        for relative in sorted(observed['dirs'],key=lambda name:len(Path(name).parts),reverse=True):
            fsync_directory(stage/relative)
            (stage/relative).chmod(0o555)
        if inventory(stage,max_bytes=max_bytes,max_files=max_files)['digest'] != expected_digest:
            raise RuntimeError('copied tree digest mismatch')
        authority()
        # The protected parent and lock exclude untrusted/concurrent publishers.
        if target.exists() or target.is_symlink():
            raise RuntimeError('tree destination appeared during copy')
        os.rename(stage,target)
        fsync_directory(parent)
        receipt = dict(request,ok=True)
        protocol.save(path,dict(request=request,receipt=receipt))
        return receipt
