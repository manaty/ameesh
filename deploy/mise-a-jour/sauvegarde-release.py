#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Verified recovery sets for the independent release controller (L232b).

`RecoverySet(db, store=, root=, retention=, config=)` binds the controller's
`backup`, `pin` and `accept` steps to the private backup primitives:

- `backup` dumps every affected database through a private credential channel,
  verifies an isolated disposable restore of the artifact against its pinned
  inventory, proves a configured remote copy can be retrieved with the separate
  recovery identity, and records the verified set in a private receipt. It
  never claims a completed backup for an incomplete artifact;
- `pin` registers that exact set in the private retention catalog with the
  release's rollback deadline and the enrolled retention window, before any
  migration. An unaccepted set is never expired;
- `accept` records release acceptance for retention and drops the transient
  receipt. An explicit operator hold is preserved.

`config` is the administrator-enrolled `backup.json` document (exact keys):
`dsn` (backup identity), `restore_dsn` (separate restore identity),
`retention_seconds` and an optional `remote` object naming the object host and
the two private credential files. Credential values never enter arguments,
receipts, errors or logs; the enrolled document holds references only.

This module is source only: it is not installed, no service is enabled and no
root, SSH, systemd, PostgreSQL migration or production effect is executed by
building or testing it. A definitive refusal answers False (the controller
holds); an ambiguous effect raises so the durable intent reconciles.
"""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
import uuid

REQUIRED_STEPS = ('backup', 'pin', 'accept')
REQUEST_FIELDS = {'operation', 'manifest_digest', 'action_id', 'manifest', 'step'}
PENDING_FIELDS = {'artifact', 'manifest_digest', 'files', 'remote', 'remote_verified'}
INVENTORY_FIELDS = {'schema', 'migrations', 'row_counts', 'artifact_sha256'}
PROOF_FIELDS = {'artifact_sha256', 'restore_verified', 'disposable_database_removed'}
CREDENTIAL_FIELDS = {'access_key', 'secret_key', 'bucket', 'region'}
REMOTE_FIELDS = {'host', 'upload_credentials', 'restore_credentials'}
CONFIG_FIELDS = {'dsn', 'restore_dsn', 'retention_seconds', 'remote'}
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
OPERATION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}')
SHA256 = re.compile(r'[0-9a-f]{64}')
MINIMUM_FREE_BYTES = 512 * 1024 * 1024
# Private staging left by a process killed mid-transfer is removed only after
# this delay, so it can never race a live sibling.
STAGING_SECONDS = 900


def module(name):
    spec = importlib.util.spec_from_file_location('recovery_' + name.replace('-', '_'),
                                                  Path(__file__).with_name(name + '.py'))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


backup = module('sauvegarder-prive')
retention = module('retention-privee')
protocol = module('controleur-release')


class RecoveryError(ValueError):
    pass


def configuration(document):
    """Exact administrator enrollment; credential values are never inlined."""
    if not isinstance(document, dict) or set(document) != CONFIG_FIELDS:
        raise RecoveryError('exact recovery enrollment required')
    for key in ('dsn', 'restore_dsn'):
        value = document[key]
        if not isinstance(value, str) or not value or '\x00' in value:
            raise RecoveryError('invalid recovery connection reference')
    seconds = document['retention_seconds']
    if type(seconds) is not int or not 3600 <= seconds <= 10 * 365 * 86400:
        raise RecoveryError('invalid recovery retention')
    remote = document['remote']
    if remote is not None:
        if not isinstance(remote, dict) or set(remote) != REMOTE_FIELDS:
            raise RecoveryError('exact remote recovery enrollment required')
        host = remote['host']
        if (not isinstance(host, str) or OPERATION.fullmatch(host) is None
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,127}', host)):
            raise RecoveryError('invalid recovery object host')
        for key in ('upload_credentials', 'restore_credentials'):
            path = remote[key]
            if (not isinstance(path, str) or not path.startswith('/') or '\x00' in path
                    or '..' in Path(path).parts):
                raise RecoveryError('invalid recovery credential reference')
        remote = {'host': host, 'upload_credentials': remote['upload_credentials'],
                  'restore_credentials': remote['restore_credentials']}
    return dict(dsn=document['dsn'], restore_dsn=document['restore_dsn'],
                retention_seconds=seconds, remote=remote)


def private_directory(path, what):
    path = Path(path)
    try:
        info = path.lstat()
        resolved = path.resolve()
    except OSError:
        raise RecoveryError('private owned ' + what + ' required') from None
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid() or info.st_mode & 0o077 or resolved != path):
        raise RecoveryError('private owned ' + what + ' required')
    return path


class RecoverySet:
    """One host's verified pre-migration recovery sets, registered by operation."""

    def __init__(self, db, *, store, root, retention, config=None, clock=time.time):
        if not callable(getattr(db, 'ping', None)):
            raise RecoveryError('release database required')
        self.db = db
        self.store = Path(store)
        self.root = private_directory(root, 'recovery state')
        self.retention = private_directory(retention, 'recovery retention')
        self.config = configuration(config)
        self.clock = clock

    # -- controller contract ------------------------------------------------
    def perform(self, step, request, release):
        request = json.loads(json.dumps(request))
        self._validate(request, step)
        if step == 'backup':
            return self._backup(request)
        if step == 'pin':
            return self._pin(request)
        return self._accept(request)

    @staticmethod
    def _validate(request, step):
        if step not in REQUIRED_STEPS:
            raise RecoveryError('invalid recovery step')
        if not isinstance(request, dict) or set(request) != REQUEST_FIELDS:
            raise RecoveryError('invalid recovery request')
        operation = request['operation']
        if (not isinstance(operation, str) or OPERATION.fullmatch(operation) is None
                or request['step'] != step or request['action_id'] != operation + ':' + step):
            raise RecoveryError('invalid recovery action')
        protocol.validate(request['manifest'])
        if protocol.identity(request['manifest']) != request['manifest_digest']:
            raise RecoveryError('invalid recovery manifest digest')

    # -- backup -------------------------------------------------------------
    def _backup(self, request):
        artifact = request['operation']
        if self._record(artifact) is not None:
            # `pin` already registered this set; reconcile its exact bytes.
            return self._registered_set(artifact)
        pending = self._pending(artifact)
        if pending is not None:
            return self._reconcile(request, pending)
        self._clean_staging()
        archive = self.retention / (artifact + '.dump')
        inventory_path = self.retention / (artifact + '.inventory')
        proof_path = self.retention / (artifact + '.restore.json')
        expected = self._complete_set(archive, inventory_path)
        if expected is False:
            return False
        if not self._restore(archive, proof_path, expected):
            return False
        files = {path.name: backup.digest(path)
                 for path in (archive, inventory_path, proof_path)}
        remote, remote_verified = [], True
        if self.config['remote'] is not None:
            item = self._upload_item(artifact, archive)
            if item is None:
                return False
            remote = [item]
            if not self._remote_proof(artifact, item):
                return False
        self._write_pending(artifact, dict(artifact=artifact,
            manifest_digest=request['manifest_digest'], files=files, remote=remote,
            remote_verified=remote_verified))
        pending = self._pending(artifact)
        return pending is not None and self._pending_files(pending)

    def _reconcile(self, request, pending):
        artifact = request['operation']
        if pending['artifact'] != artifact or pending['manifest_digest'] != request['manifest_digest']:
            raise RecoveryError('recovery receipt mismatch')
        if not self._pending_files(pending):
            return False
        if self.config['remote'] is not None:
            if len(pending['remote']) != 1:
                raise RecoveryError('recovery receipt mismatch')
            if pending['remote_verified'] is not True:
                archive = self.retention / (artifact + '.dump')
                if not self._remote_proof(artifact, pending['remote'][0]):
                    return False
                pending['remote_verified'] = True
                self._write_pending(artifact, pending)
        return True

    def _complete_set(self, archive, inventory_path):
        """Reconcile a crash-interrupted publication or create the artifact."""
        for path in (archive, inventory_path):
            if os.path.lexists(path) and (path.is_symlink() or not path.is_file()):
                raise RecoveryError('unsafe recovery artifact')
        if archive.is_file() and inventory_path.is_file():
            expected = self._read_inventory(inventory_path)
            if expected is not None and backup.digest(archive) == expected['artifact_sha256']:
                return expected
        # No catalog record and no verified proof exist for an incomplete pair:
        # it is not a recovery set and is recreated from the live source.
        for path in (archive, inventory_path):
            if os.path.lexists(path):
                path.unlink()
        return self._dump(archive, inventory_path)

    def _dump(self, archive, inventory_path):
        if shutil.disk_usage(self.retention).free < MINIMUM_FREE_BYTES:
            return False
        with self._config_file(self.config['dsn']) as config:
            try:
                backup.snapshot_dump(config, archive, inventory_path)
            except backup.BackupError:
                return False
        expected = self._read_inventory(inventory_path)
        if expected is None or not archive.is_file() or backup.digest(archive) != expected['artifact_sha256']:
            raise RecoveryError('incomplete recovery publication')
        return expected

    def _restore(self, archive, proof_path, expected):
        if os.path.lexists(proof_path):
            if proof_path.is_symlink() or not proof_path.is_file():
                raise RecoveryError('unsafe recovery proof')
            proof = self._read_proof(proof_path, expected['artifact_sha256'])
            if proof is not None:
                return True
            proof_path.unlink()
        with self._config_file(self.config['restore_dsn']) as config:
            try:
                proof = backup.verify_restore(config, archive, expected)
            except backup.BackupError:
                return False
        value = {'artifact_sha256': expected['artifact_sha256'], 'restore_verified': True,
                 'disposable_database_removed': True}
        if proof != value:
            raise RecoveryError('restore proof mismatch')
        self._write_private(proof_path, value)
        return True

    def _upload_item(self, artifact, archive):
        remote = self.config['remote']
        receipt_path = self.retention / (artifact + '.remote.json')
        sha = backup.digest(archive)
        if os.path.lexists(receipt_path):
            item = self._remote_receipt(receipt_path, remote['host'], artifact, sha)
            if item is None:
                raise RecoveryError('invalid remote recovery receipt')
            return item
        try:
            credentials = backup.storage_credentials(remote['upload_credentials'])
        except backup.BackupError:
            return None
        staging = self.root / '.staging'
        staging.mkdir(mode=0o700, exist_ok=True)
        staging.chmod(0o700)
        try:
            backup.upload(archive, remote['host'], receipt=receipt_path,
                          credentials=credentials, staging=staging)
        except backup.BackupError:
            return None
        item = self._remote_receipt(receipt_path, remote['host'], artifact, sha)
        if item is None:
            raise RecoveryError('missing remote recovery receipt')
        return item

    @staticmethod
    def _remote_receipt(path, host, artifact, sha):
        try:
            info, value = path.lstat(), json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1):
            return None
        version = value.get('version_id') if isinstance(value, dict) else None
        if (not isinstance(value, dict) or set(value) != {'object_id', 'sha256', 'version_id',
                                                          'bucket', 'region'}
                or value.get('object_id') != host + '/' + artifact + '.dump'
                or value.get('sha256') != sha
                or not isinstance(version, str) or not 1 <= len(version) <= 1024
                or version.lower() == 'null' or any(ord(c) < 33 or ord(c) > 126 for c in version)):
            return None
        return {'object_id': value['object_id'], 'sha256': value['sha256'],
                'deleted': False, 'version_id': version}

    def _remote_proof(self, artifact, item):
        remote = self.config['remote']
        try:
            with tempfile.TemporaryDirectory(prefix='.verify-', dir=self.root) as staging:
                target = Path(staging) / 'verify.dump'
                backup.retrieve(remote['restore_credentials'], remote['host'],
                                artifact + '.dump', item['sha256'], target,
                                version_id=item['version_id'])
        except backup.BackupError:
            return False
        return True

    # -- pin and accept -----------------------------------------------------
    def _pin(self, request):
        artifact = request['operation']
        record = self._record(artifact)
        if record is not None:
            if record.get('state') != 'retained':
                raise RecoveryError('recovery set already deleting')
            return self._files_match(record.get('files'))
        pending = self._pending(artifact)
        if pending is None or pending['manifest_digest'] != request['manifest_digest']:
            return False
        if not self._pending_files(pending):
            return False
        if self.config['remote'] is not None and pending['remote_verified'] is not True:
            return False
        try:
            retention.register(self.retention, artifact, sorted(pending['files']),
                               expires_at=self.clock() + self.config['retention_seconds'],
                               rollback_until=request['manifest']['rollback_until'],
                               remote=pending['remote'])
        except retention.RetentionError:
            return False
        record = self._record(artifact)
        if (record is None or record.get('accepted') is not False
                or record.get('hold') is not False or record.get('state') != 'retained'):
            raise RecoveryError('recovery registration not durable')
        return self._files_match(record.get('files'))

    def _accept(self, request):
        artifact = request['operation']
        record = self._record(artifact)
        if record is None:
            return False
        if record.get('state') != 'retained':
            # A durable acceptance survives pruning; a deleted unaccepted set
            # was never accepted.
            return record.get('accepted') is True and record.get('state') in ('deleting', 'deleted')
        if not self._files_match(record.get('files')):
            return False
        try:
            retention.protect(self.retention, artifact, accepted=True)
        except retention.RetentionError:
            return False
        record = self._record(artifact)
        if record is None or record.get('accepted') is not True:
            raise RecoveryError('recovery acceptance not durable')
        path = self.root / (artifact + '.set.json')
        if os.path.lexists(path):
            try:
                path.unlink()
            except OSError:
                raise RecoveryError('recovery receipt removal unavailable') from None
        return True

    # -- private catalog and receipts --------------------------------------
    def _record(self, artifact):
        try:
            with retention.locked(self.retention) as (_, records):
                value = records.get(artifact)
        except retention.RetentionError:
            raise RecoveryError('recovery retention catalog unavailable') from None
        if value is None:
            return None
        if not isinstance(value, dict):
            raise RecoveryError('invalid recovery retention record')
        return json.loads(json.dumps(value))

    def _registered_set(self, artifact):
        record = self._record(artifact)
        if record is None or record.get('state') != 'retained':
            return False
        return self._files_match(record.get('files'))

    def _pending(self, artifact):
        path = self.root / (artifact + '.set.json')
        if not os.path.lexists(path):
            return None
        try:
            info = path.lstat()
        except OSError:
            raise RecoveryError('recovery receipt unavailable') from None
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1):
            raise RecoveryError('unsafe recovery receipt')
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            raise RecoveryError('invalid recovery receipt') from None
        return self._pending_value(value, artifact)

    @staticmethod
    def _pending_value(value, artifact):
        if (not isinstance(value, dict) or set(value) != PENDING_FIELDS
                or value.get('artifact') != artifact
                or not isinstance(value.get('manifest_digest'), str)
                or SHA256.fullmatch(value['manifest_digest']) is None
                or type(value.get('remote_verified')) is not bool):
            raise RecoveryError('invalid recovery receipt')
        files = value.get('files')
        if not isinstance(files, dict) or not files:
            raise RecoveryError('invalid recovery receipt')
        checked = {}
        for name, sha in files.items():
            if (not isinstance(name, str) or name.startswith('.') or IDENTIFIER.fullmatch(name) is None
                    or not isinstance(sha, str) or SHA256.fullmatch(sha) is None):
                raise RecoveryError('invalid recovery receipt')
            checked[name] = sha
        remote = value.get('remote')
        if not isinstance(remote, list) or len(remote) > 8:
            raise RecoveryError('invalid recovery receipt')
        items = []
        for item in remote:
            if (not isinstance(item, dict)
                    or set(item) not in ({'object_id', 'sha256', 'deleted'},
                                         {'object_id', 'sha256', 'deleted', 'version_id'})
                    or item.get('deleted') is not False):
                raise RecoveryError('invalid recovery receipt')
            object_id, sha = item.get('object_id'), item.get('sha256')
            if (not isinstance(object_id, str) or object_id.count('/') != 1
                    or not isinstance(sha, str) or SHA256.fullmatch(sha) is None
                    or sha not in checked.values()):
                raise RecoveryError('invalid recovery receipt')
            host, name = object_id.split('/')
            if IDENTIFIER.fullmatch(host) is None or IDENTIFIER.fullmatch(name) is None:
                raise RecoveryError('invalid recovery receipt')
            if 'version_id' in item:
                version = item['version_id']
                if (not isinstance(version, str) or not 1 <= len(version) <= 1024
                        or version.lower() == 'null'
                        or any(ord(c) < 33 or ord(c) > 126 for c in version)):
                    raise RecoveryError('invalid recovery receipt')
            items.append(dict(item))
        return dict(artifact=artifact, manifest_digest=value['manifest_digest'],
                    files=checked, remote=items, remote_verified=value['remote_verified'])

    def _write_pending(self, artifact, value):
        protocol.save(self.root / (artifact + '.set.json'), value)

    def _pending_files(self, pending):
        return self._files_match(pending['files'])

    def _files_match(self, files):
        if not isinstance(files, dict) or not files:
            return False
        for name, sha in files.items():
            if (not isinstance(name, str) or name.startswith('.') or IDENTIFIER.fullmatch(name) is None
                    or not isinstance(sha, str) or SHA256.fullmatch(sha) is None):
                return False
            path = self.retention / name
            if not os.path.lexists(path) or path.is_symlink() or not path.is_file():
                return False
            try:
                if retention.private_digest(path) != sha:
                    return False
            except retention.RetentionError:
                return False
        return True

    @staticmethod
    def _read_inventory(path):
        try:
            value = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return None
        if (not isinstance(value, dict) or set(value) != INVENTORY_FIELDS
                or not isinstance(value.get('schema'), str) or not value['schema']
                or '\x00' in value['schema']
                or not isinstance(value.get('artifact_sha256'), str)
                or SHA256.fullmatch(value['artifact_sha256']) is None):
            return None
        migrations, counts = value.get('migrations'), value.get('row_counts')
        if (not isinstance(migrations, list) or not migrations
                or any(not isinstance(row, list) or len(row) != 2
                       or isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] < 1
                       or not isinstance(row[1], str) or SHA256.fullmatch(row[1]) is None
                       for row in migrations)):
            return None
        if (not isinstance(counts, dict) or not counts
                or any(not isinstance(name, str) or not name or '\x00' in name
                       or isinstance(count, bool) or not isinstance(count, int) or count < 0
                       for name, count in counts.items())):
            return None
        return value

    @staticmethod
    def _read_proof(path, sha256):
        try:
            value = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return None
        if (not isinstance(value, dict) or set(value) != PROOF_FIELDS
                or value.get('artifact_sha256') != sha256
                or value.get('restore_verified') is not True
                or value.get('disposable_database_removed') is not True):
            return None
        return value

    def _write_private(self, path, value):
        candidate = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
        descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                json.dump(value, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(candidate, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            candidate.unlink(missing_ok=True)

    @contextmanager
    def _config_file(self, dsn):
        """Never pass a credential-bearing DSN in arguments or environment."""
        with tempfile.TemporaryDirectory(prefix='.config-', dir=self.root) as staging:
            path = Path(staging) / 'config.json'
            with path.open('x') as stream:
                path.chmod(0o600)
                stream.write(json.dumps({'dsn': dsn}, sort_keys=True))
                stream.flush()
                os.fsync(stream.fileno())
            yield str(path)

    def _clean_staging(self):
        now = self.clock()
        for parent, prefixes in ((self.retention, ('.backup-', '.inventory-', '.restore-')),
                                 (self.root, ('.config-', '.verify-')),
                                 (self.root / '.staging', ('backup-upload-',))):
            if not parent.is_dir():
                continue
            for entry in parent.iterdir():
                if not entry.name.startswith(prefixes):
                    continue
                try:
                    info = entry.lstat()
                    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                            or now - info.st_mtime < STAGING_SECONDS):
                        continue
                    shutil.rmtree(entry)
                except OSError:
                    continue
