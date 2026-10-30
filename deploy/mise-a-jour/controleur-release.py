#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Durable release ordering; no executable production adapter is enrolled.

Run from an independent controller, never from a runner being restarted.
Adapters must reconcile stable action ids, not blindly repeat side effects.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid


class ControllerError(ValueError):
    pass


STEPS = ('prepare', 'backup', 'pin', 'fence', 'migrate', 'configure',
         'activate', 'restart', 'health', 'accept', 'unfence')
RECOVERY = ('recovery_fence', 'rollback', 'recovery_restart',
            'recovery_health', 'recovery_unfence')
FENCED = {'migrate', 'configure', 'activate', 'restart', 'health', 'accept',
          'rollback', 'recovery_restart', 'recovery_health'}


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate(manifest):
    """Only reference fields enter the journal; credentials stay in adapters."""
    fields = {'host', 'commit', 'release', 'database', 'units', 'policy', 'rollback_until'}
    if not isinstance(manifest, dict) or set(manifest) != fields:
        raise ControllerError('invalid manifest fields')
    for key in ('host', 'database', 'policy'):
        if not isinstance(manifest[key], str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', manifest[key]):
            raise ControllerError('invalid manifest reference')
    if not re.fullmatch(r'[0-9a-f]{40}', str(manifest['commit'])):
        raise ControllerError('full commit required')
    if not re.fullmatch(manifest['commit'] + r'-[0-9a-f]{64}', str(manifest['release'])):
        raise ControllerError('release identity mismatch')
    units = manifest['units']
    if (not isinstance(units, list) or not units
            or any(not isinstance(unit, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,127}\.service', unit) for unit in units)
            or len(set(units)) != len(units)):
        raise ControllerError('invalid unit allowlist')
    if type(manifest['rollback_until']) is not int or manifest['rollback_until'] < 0:
        raise ControllerError('invalid rollback deadline')


@contextmanager
def locked(root, host):
    root = Path(root).absolute()
    # Reject linked ancestors as well as a linked final directory.
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise ControllerError('linked journal directory')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ControllerError('journal directory must be private and owned')
    descriptor = os.open(root / (host + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ControllerError('unsafe journal lock')
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield root
    finally:
        os.close(descriptor)


def save(path, record):
    candidate = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(record, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(candidate, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        candidate.unlink(missing_ok=True)


def load(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ControllerError('unsafe journal file')
        return json.load(stream)


def checked(method, *args):
    try:
        allowed = method(*args) is True
    except Exception:
        raise ControllerError('authority or fence verification unavailable') from None
    reason = getattr(getattr(method, '__self__', None), 'denial_reason', None)
    if not allowed and reason:
        raise ControllerError(reason)
    return allowed


def run(root, operation, manifest, *, authority, adapter):
    """Resume one host operation with live authority/fence checks per action.

    authority.validate(operation, manifest_digest, step) must verify an enrolled
    delegation or scoped signed receipt, including host, payload and expiry.
    adapter.perform(step, request) returns EXACTLY request + {'ok': bool}.
    Its receipt attests reconciled effects. False denotes definitive failure;
    exceptions mean uncertain effects and keep the intent for reconciliation.
    adapter.fenced(request) verifies a live claim fence AND no active turns.
    Unfence must atomically check the owned fence on FIRST execution and
    replay an existing receipt after its removal (including after a crash).
    All methods must have bounded deadlines and sanitize their own logs.
    """
    validate(manifest)
    if not isinstance(operation, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', operation):
        raise ControllerError('invalid operation id')
    digest = identity(manifest)
    with locked(root, manifest['host']) as directory:
        path = directory / (operation + '.json')
        active = directory / (manifest['host'] + '.active.json')
        if path.name == 'readiness.json':
            # Reserved controller/service name: an operation journal never
            # replaces the independent service readiness document.
            raise ControllerError('reserved operation id')
        # The journal is read before the host is reserved, so a changed manifest
        # can never leave an orphan reservation that would block every later
        # operation of the host (the reservation itself stays authoritative when
        # the host is still held by an interrupted run).
        record = None
        if path.exists() or path.is_symlink():
            record = load(path)
        changed = record is not None and (not isinstance(record, dict)
                                          or record.get('manifest') != manifest
                                          or record.get('digest') != digest)
        if active.exists() or active.is_symlink():
            owner = load(active)
            if owner != {'operation': operation, 'digest': digest}:
                raise ControllerError('host has another unresolved release')
        elif changed:
            # Refuse before reserving: this operation name already names another
            # manifest, and no later run could resume that reservation.
            raise ControllerError('operation manifest changed')
        elif record is None or record['status'] not in ('accepted', 'rolled_back'):
            # Admission itself requires current authority, before reserving host.
            # A finished operation is replayed without re-reserving it.
            if not checked(authority.validate, operation, digest, 'prepare'):
                raise ControllerError('release authority denied')
            save(active, {'operation': operation, 'digest': digest})
        if changed:
            raise ControllerError('operation manifest changed')
        if record is None:
            record = {'manifest': manifest, 'digest': digest, 'mode': 'release',
                      'receipts': {}, 'intent': None, 'status': 'pending'}
            save(path, record)
        if record['status'] in ('accepted', 'rolled_back'):
            # A crash after final receipt can leave only the reservation.
            active.unlink(missing_ok=True)
            return record['status']
        while True:
            steps = STEPS if record['mode'] == 'release' else RECOVERY
            step = next((item for item in steps if item not in record['receipts']), None)
            if step is None:
                record['status'] = 'accepted' if record['mode'] == 'release' else 'rolled_back'
                save(path, record)
                active.unlink()
                return record['status']
            request = {'operation': operation, 'manifest_digest': digest,
                       'action_id': operation + ':' + step, 'manifest': manifest,
                       'step': step}
            if not checked(authority.validate, operation, digest, step):
                raise ControllerError('release authority denied')
            if step in FENCED and not checked(adapter.fenced, request):
                raise ControllerError('live drained claim fence required')
            record['intent'] = step
            save(path, record)
            try:
                receipt = adapter.perform(step, request)
            except Exception:
                # No raw provider exception (possibly containing secrets) escapes.
                raise ControllerError('action outcome uncertain; reconcile intent') from None
            if (not isinstance(receipt, dict) or set(receipt) != set(request) | {'ok'}
                    or any(receipt.get(key) != value for key, value in request.items())
                    or type(receipt.get('ok')) is not bool):
                raise ControllerError('action receipt mismatch; reconcile intent')
            if not receipt['ok']:
                if record['mode'] == 'release' and step in ('configure', 'activate', 'restart', 'health'):
                    record['mode'] = 'recovery'
                    record['status'] = 'recovering'
                    record['intent'] = None
                    save(path, record)
                    continue
                raise ControllerError('action failed; release held')
            record['receipts'][step] = receipt
            record['intent'] = None
            save(path, record)
