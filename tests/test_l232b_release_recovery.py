# SPDX-License-Identifier: AGPL-3.0-only
"""Recovery sets: real private files, simulated dump/restore/provider effects.

No root, SSH, systemd, PostgreSQL dump or production effect is executed: the
disposable fixture roots are the only filesystem touched, and the recovery
primitives are replaced by recording fakes.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    'release_recovery', Path(__file__).resolve().parents[1] / 'deploy/mise-a-jour/sauvegarde-release.py')
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


class Database:
    def ping(self):
        return True


class RecoverySetTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ameesh-release-recovery-')
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        # The enrolled capacity attestation is only usable under a parent chain
        # no third party can rewrite. The disposable root itself is private; its
        # shared ancestors (/tmp and below) are made to look owned and unwritable
        # exactly as the other private-file fixtures do.
        original = Path.lstat
        ancestors = set(Path(self.temp.name).parents)
        def fixture_ancestors(path):
            info = original(path)
            if path in ancestors and stat.S_ISDIR(info.st_mode):
                fields = list(info); fields[0] &= ~0o022; fields[4] = os.geteuid()
                return os.stat_result(fields)
            return info
        patch = mock.patch.object(Path, 'lstat', fixture_ancestors)
        patch.start(); self.addCleanup(patch.stop)
        self.root = base / 'state'
        self.retention = base / 'retention'
        self.store = base / 'store'
        for path in (self.root, self.retention, self.store):
            path.mkdir(mode=0o700)
        self.clock = lambda: 2_000_000.0
        self.capacity_path = base / 'restore-capacity.json'
        self.manifest = dict(host='fixture-host', commit='a' * 40, release='a' * 40 + '-' + 'b' * 64,
                             database='fixture-db', units=['ameesh-runner@one.service'],
                             policy='policy-' + 'c' * 64, rollback_until=2_000_000 + 3600)
        self.config = dict(dsn='postgresql://backup:CANARY-DSN@localhost/fixture-db',
                           restore_dsn='postgresql://restore:CANARY-RESTORE@localhost/fixture-db',
                           retention_seconds=14 * 24 * 3600, remote=None,
                           restore_capacity=str(self.capacity_path))
        self.write_capacity()
        self.value = recovery.RecoverySet(Database(), store=self.store, root=self.root,
                                          retention=self.retention, config=self.config,
                                          clock=self.clock)
        self.release = dict(commit='a' * 40)

    def write_capacity(self, *, free_bytes=8 * 1024 ** 3, checked_at=None, target=None, mode=0o600):
        """Refresh the live attestation the enrolled producer would write."""
        document = dict(free_bytes=free_bytes,
                        checked_at=int(self.clock() if checked_at is None else checked_at),
                        target_sha256=(target if target is not None else
                                       hashlib.sha256(self.config['restore_dsn'].encode()).hexdigest()))
        self.capacity_path.write_text(json.dumps(document, sort_keys=True))
        self.capacity_path.chmod(mode)
        return document

    def perform(self, step, operation='release-fixture', manifest=None):
        return self.value.perform(step, self.request(step, operation=operation, manifest=manifest), self.release)

    def request(self, step, operation='release-fixture', manifest=None):
        manifest = manifest or self.manifest
        return dict(operation=operation, manifest_digest=recovery.protocol.identity(manifest),
                    action_id=operation + ':' + step, manifest=manifest, step=step)

    def archive(self):
        return Path(self.retention) / 'release-fixture.dump'

    def inventory(self):
        return Path(self.retention) / 'release-fixture.inventory'

    def write_inventory(self, archive, value=None, inventory=None):
        digest = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
        document = value or dict(schema='public', migrations=[[1, 'd' * 64]],
                                 row_counts={'agent_registry': 4}, artifact_sha256=digest,
                                 source_bytes=1024 * 1024)
        path = Path(inventory) if inventory is not None else Path(self.inventory())
        path.write_text(json.dumps(document, sort_keys=True))
        path.chmod(0o600)
        return document

    def fake_dump(self, config, output, inventory):
        settings = json.loads(Path(config).read_text())
        self.assertEqual(set(settings), {'dsn'})
        Path(output).write_bytes(b'archive-bytes')
        Path(output).chmod(0o600)
        document = self.write_inventory(output, inventory=inventory)
        return {'artifact_sha256': document['artifact_sha256'], 'inventory': str(inventory)}

    def fake_restore(self, config, archive, expected):
        settings = json.loads(Path(config).read_text())
        self.assertEqual(settings['dsn'], self.config['restore_dsn'])
        return {'artifact_sha256': expected['artifact_sha256'], 'restore_verified': True,
                'disposable_database_removed': True}

    def catalog(self):
        path = Path(self.retention) / '.retention.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def backup(self, manifest=None, operation='release-fixture'):
        with mock.patch.object(recovery.backup, 'snapshot_dump', side_effect=self.fake_dump), \
                mock.patch.object(recovery.backup, 'verify_restore', side_effect=self.fake_restore):
            return self.perform('backup', operation=operation, manifest=manifest)

    def test_backup_pin_accept_register_and_protect_the_exact_verified_set(self):
        self.assertTrue(self.backup())
        pending = self.value._pending('release-fixture')
        self.assertTrue(pending['remote_verified'])
        self.assertEqual(set(pending['files']),
                         {'release-fixture.dump', 'release-fixture.inventory', 'release-fixture.restore.json'})
        self.assertEqual(self.catalog(), {}, 'backup must not register before pin')
        self.assertTrue(self.perform('pin'))
        record = self.catalog()['release-fixture']
        self.assertFalse(record['accepted'])
        self.assertFalse(record['hold'])
        self.assertEqual(record['state'], 'retained')
        self.assertEqual(record['expires_at'], self.clock() + self.config['retention_seconds'])
        self.assertEqual(record['rollback_until'], self.manifest['rollback_until'])
        self.assertEqual(record['files'], pending['files'])
        self.assertTrue(self.perform('accept'))
        self.assertTrue(self.catalog()['release-fixture']['accepted'])
        for name in pending['files']:
            self.assertTrue((Path(self.retention) / name).is_file())
        self.assertFalse((Path(self.root) / 'release-fixture.set.json').exists())
        self.assertTrue(self.perform('accept'))
        self.assertTrue(self.backup())

    def test_lost_reply_reconciles_without_a_second_dump_or_restore(self):
        with mock.patch.object(recovery.backup, 'snapshot_dump', side_effect=self.fake_dump) as dump, \
                mock.patch.object(recovery.backup, 'verify_restore', side_effect=self.fake_restore) as restore:
            self.assertTrue(self.perform('backup'))
            self.assertTrue(self.perform('backup'))
        self.assertEqual(dump.call_count, 1)
        self.assertEqual(restore.call_count, 1)

    def test_interrupted_publication_is_reconciled_without_a_second_dump(self):
        archive = self.archive()
        archive.write_bytes(b'archive-bytes')
        archive.chmod(0o600)
        self.write_inventory(archive)
        with mock.patch.object(recovery.backup, 'snapshot_dump', side_effect=self.fake_dump) as dump, \
                mock.patch.object(recovery.backup, 'verify_restore', side_effect=self.fake_restore):
            self.assertTrue(self.perform('backup'))
        dump.assert_not_called()
        self.assertTrue(self.value._pending('release-fixture'))

    def test_incomplete_publication_is_removed_and_recreated(self):
        archive = self.archive()
        archive.write_bytes(b'incomplete')
        archive.chmod(0o600)
        with mock.patch.object(recovery.backup, 'snapshot_dump', side_effect=self.fake_dump) as dump, \
                mock.patch.object(recovery.backup, 'verify_restore', side_effect=self.fake_restore):
            self.assertTrue(self.perform('backup'))
        self.assertEqual(dump.call_count, 1)
        self.assertEqual(archive.read_bytes(), b'archive-bytes')

    def test_pending_receipt_of_another_manifest_is_ambiguous(self):
        self.assertTrue(self.backup())
        pending = self.value._pending('release-fixture')
        pending['manifest_digest'] = '0' * 64
        self.value._write_pending('release-fixture', pending)
        with self.assertRaises(recovery.RecoveryError):
            self.perform('backup')

    def test_definitive_dump_or_restore_failure_returns_false_without_receipt(self):
        with mock.patch.object(recovery.backup, 'snapshot_dump',
                               side_effect=recovery.backup.BackupError('dump failed')):
            self.assertFalse(self.perform('backup'))
        self.assertFalse((Path(self.root) / 'release-fixture.set.json').exists())
        with mock.patch.object(recovery.backup, 'snapshot_dump', side_effect=self.fake_dump), \
                mock.patch.object(recovery.backup, 'verify_restore',
                                  side_effect=recovery.backup.BackupError('restore failed')):
            self.assertFalse(self.perform('backup'))
        self.assertFalse((Path(self.root) / 'release-fixture.set.json').exists())

    def test_pin_requires_a_verified_set_and_accept_requires_an_accepted_record(self):
        self.assertFalse(self.perform('pin'))
        self.assertFalse(self.perform('accept'))
        self.assertTrue(self.backup())
        self.assertFalse(self.perform('accept'))
        self.assertTrue(self.perform('pin'))

    def test_pinned_set_is_never_expired_until_accepted(self):
        self.assertTrue(self.backup())
        self.assertTrue(self.perform('pin'))
        record = self.catalog()['release-fixture']
        now = max(record['expires_at'], record['rollback_until']) + 1
        self.assertEqual(recovery.retention.prune(self.retention, now=now), [])
        self.assertEqual(self.catalog()['release-fixture']['state'], 'retained')
        self.assertTrue(self.perform('accept'))
        deleted = recovery.retention.prune(self.retention, now=now)
        self.assertEqual(deleted, ['release-fixture'])
        self.assertEqual(self.catalog()['release-fixture']['state'], 'deleted')
        # Acceptance is durable even after the set is pruned.
        self.assertTrue(self.perform('accept'))

    def test_changed_or_missing_recovery_artifact_refuses_acceptance(self):
        self.assertTrue(self.backup())
        self.assertTrue(self.perform('pin'))
        self.archive().write_bytes(b'changed')
        self.archive().chmod(0o600)
        self.assertFalse(self.perform('accept'))
        self.assertFalse(self.catalog()['release-fixture']['accepted'])

    def test_invalid_step_action_or_request_shape_is_refused(self):
        with self.assertRaises(recovery.RecoveryError):
            self.value.perform('configure', self.request('configure'), self.release)
        broken = self.request('backup')
        broken['action_id'] = 'release-fixture:pin'
        with self.assertRaises(recovery.RecoveryError):
            self.value.perform('backup', broken, self.release)

    def remote_value(self, upload_credentials, restore_credentials):
        return recovery.RecoverySet(Database(), store=self.store, root=self.root, retention=self.retention,
            config=dict(self.config, remote={'host': 'ameesh-release-fixture',
                'upload_credentials': str(upload_credentials),
                'restore_credentials': str(restore_credentials)}), clock=self.clock)

    def credential_files(self):
        base = Path(self.temp.name)
        upload_credentials, restore_credentials = base / 'upload.json', base / 'restore.json'
        upload_credentials.write_text(json.dumps({'access_key': 'CANARY-ACCESS', 'secret_key': 'CANARY-SECRET',
                                                  'bucket': 'acme', 'region': 'fr-par'}))
        restore_credentials.write_text(json.dumps({'access_key': 'CANARY-RESTORE-ACCESS',
                                                   'secret_key': 'CANARY-RESTORE-SECRET',
                                                   'bucket': 'acme', 'region': 'fr-par'}))
        for path in (upload_credentials, restore_credentials):
            path.chmod(0o600)
        return upload_credentials, restore_credentials

    @staticmethod
    def fake_upload(archive, host, *, receipt=None, credentials=None, staging=None):
        digest = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
        Path(receipt).write_text(json.dumps({'object_id': host + '/' + Path(archive).name,
                                             'sha256': digest, 'version_id': 'v1',
                                             'bucket': 'acme', 'region': 'fr-par'}))
        Path(receipt).chmod(0o600)
        return json.loads(Path(receipt).read_text())

    @staticmethod
    def fake_retrieve(credentials, host, name, sha256, output, *, version_id=None):
        Path(output).write_bytes(b'archive-bytes')
        Path(output).chmod(0o600)
        return {'artifact_sha256': sha256, 'retrieved': True, 'version_id': version_id}

    def test_remote_backup_uses_private_credentials_and_proves_retrieval(self):
        upload_credentials, restore_credentials = self.credential_files()
        self.value = self.remote_value(upload_credentials, restore_credentials)
        seen = {}

        def recording_upload(archive, host, *, receipt=None, credentials=None, staging=None):
            seen['upload'] = (host, dict(credentials), Path(staging))
            return self.fake_upload(archive, host, receipt=receipt, credentials=credentials, staging=staging)

        def recording_retrieve(credentials, host, name, sha256, output, *, version_id=None):
            seen['retrieve'] = (str(credentials), host, name, sha256, version_id)
            return self.fake_retrieve(credentials, host, name, sha256, output, version_id=version_id)

        with mock.patch.object(recovery.backup, 'snapshot_dump', side_effect=self.fake_dump), \
                mock.patch.object(recovery.backup, 'verify_restore', side_effect=self.fake_restore), \
                mock.patch.object(recovery.backup, 'upload', side_effect=recording_upload) as upload, \
                mock.patch.object(recovery.backup, 'retrieve', side_effect=recording_retrieve) as retrieve:
            self.assertTrue(self.perform('backup'))
            self.assertTrue(self.perform('backup'))
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(retrieve.call_count, 1)
        self.assertEqual(seen['upload'][1]['secret_key'], 'CANARY-SECRET')
        self.assertTrue(seen['upload'][2].is_dir())
        self.assertEqual(seen['retrieve'][0], str(restore_credentials))
        self.assertEqual(seen['retrieve'][4], 'v1')
        pending = self.value._pending('release-fixture')
        self.assertTrue(pending['remote_verified'])
        self.assertEqual(pending['remote'], [{'object_id': 'ameesh-release-fixture/release-fixture.dump',
                                              'sha256': hashlib.sha256(b'archive-bytes').hexdigest(),
                                              'deleted': False, 'version_id': 'v1'}])
        self.assertNotIn('CANARY-SECRET', json.dumps(pending))
        self.assertTrue(self.perform('pin'))
        self.assertEqual(self.catalog()['release-fixture']['remote'], pending['remote'])

    def test_failed_remote_proof_is_definitive_and_resumes_on_retry(self):
        upload_credentials, restore_credentials = self.credential_files()
        self.value = self.remote_value(upload_credentials, restore_credentials)
        with mock.patch.object(recovery.backup, 'snapshot_dump', side_effect=self.fake_dump), \
                mock.patch.object(recovery.backup, 'verify_restore', side_effect=self.fake_restore), \
                mock.patch.object(recovery.backup, 'upload', side_effect=self.fake_upload) as upload, \
                mock.patch.object(recovery.backup, 'retrieve',
                                  side_effect=recovery.backup.BackupError('retrieval failed')):
            self.assertFalse(self.perform('backup'))
        self.assertIsNone(self.value._pending('release-fixture'))
        with mock.patch.object(recovery.backup, 'snapshot_dump', side_effect=self.fake_dump) as dump, \
                mock.patch.object(recovery.backup, 'verify_restore', side_effect=self.fake_restore), \
                mock.patch.object(recovery.backup, 'upload', side_effect=self.fake_upload) as again, \
                mock.patch.object(recovery.backup, 'retrieve', side_effect=self.fake_retrieve):
            self.assertTrue(self.perform('backup'))
        dump.assert_not_called()
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(again.call_count, 0)

    def test_dsn_and_credential_canaries_leave_no_plaintext_staging_behind(self):
        self.assertTrue(self.backup())
        self.assertTrue(self.perform('pin'))
        self.assertTrue(self.perform('accept'))
        leftovers = []
        for parent in (self.root, self.retention):
            for path in parent.rglob('*'):
                if path.is_file() and 'CANARY' in path.read_text(errors='ignore'):
                    leftovers.append(path)
        self.assertEqual(leftovers, [])

    # -- K3: a stale artifact is never pinned as the pre-migration state -----
    def stale_stamp(self):
        return self.clock() - recovery.REUSE_SECONDS - 1

    def test_stale_publication_is_recreated_instead_of_pinned(self):
        archive = self.archive()
        archive.write_bytes(b'archive-bytes')
        archive.chmod(0o600)
        self.write_inventory(archive)
        stale = self.stale_stamp()
        for path in (archive, self.inventory()):
            os.utime(path, (stale, stale))
        with mock.patch.object(recovery.backup, 'snapshot_dump', side_effect=self.fake_dump) as dump, \
                mock.patch.object(recovery.backup, 'verify_restore', side_effect=self.fake_restore):
            self.assertTrue(self.perform('backup'))
        self.assertEqual(dump.call_count, 1)
        self.assertGreater(archive.stat().st_mtime, stale)

    def test_stale_pending_receipt_is_not_reused(self):
        self.assertTrue(self.backup())
        stale = self.stale_stamp()
        for name in self.value._pending('release-fixture')['files']:
            os.utime(Path(self.retention) / name, (stale, stale))
        with mock.patch.object(recovery.backup, 'snapshot_dump', side_effect=self.fake_dump) as dump, \
                mock.patch.object(recovery.backup, 'verify_restore', side_effect=self.fake_restore) as restore:
            self.assertTrue(self.perform('backup'))
        self.assertEqual(dump.call_count, 1)
        self.assertEqual(restore.call_count, 1)

    def controller(self, *, stop=None):
        """Contrôleur réel autour du jeu : `backup`/`pin`/`accept` délégués,
        autres étapes simulées ; `stop` interrompt la reprise avant une étape
        (processus arrêté entre deux reçus, comme une release tenue)."""
        class Authority:
            def validate(self, operation, digest, step):
                return True

        value, release = self.value, self.release

        class Adapter:
            def __init__(self):
                self.steps = []

            def perform(self, step, request):
                self.steps.append(step)
                if step == stop:
                    raise RuntimeError('interrupted before %s' % step)
                ok = (value.perform(step, request, release)
                      if step in ('backup', 'pin', 'accept') else True)
                return dict(request, ok=ok)

            def fenced(self, request):
                return True

        return Authority(), Adapter()

    def test_stale_receipt_is_recreated_at_pin_through_the_controller(self):
        """K3 (dev7/4971 R2) : après un `backup` reçu puis une longue attente,
        la reprise du contrôleur sur `pin` recrée le jeu depuis la source vive
        et l'inscrit — elle ne se cale pas sur un refus que le temps aggrave."""
        autorite, premier = self.controller(stop='pin')
        appels = []

        def dump_once(config, output, inventory):
            appels.append(1)
            source = self.fake_dump if len(appels) == 1 else self.fresh_dump
            return source(config, output, inventory)

        with mock.patch.object(recovery.backup, 'snapshot_dump',
                               side_effect=dump_once) as dump, \
                mock.patch.object(recovery.backup, 'verify_restore',
                                  side_effect=self.fake_restore) as restore:
            with self.assertRaises(recovery.protocol.ControllerError):
                recovery.protocol.run(self.root, 'release-fixture', self.manifest,
                                      authority=autorite, adapter=premier)
            self.assertEqual(premier.steps[:2], ['prepare', 'backup'])
            avant = dict(self.value._pending('release-fixture')['files'])
            # La release attend : des jours passent entre le reçu et le pin.
            stale = self.stale_stamp()
            for name in avant:
                os.utime(Path(self.retention) / name, (stale, stale))
            autorite, second = self.controller()
            status = recovery.protocol.run(self.root, 'release-fixture', self.manifest,
                                           authority=autorite, adapter=second)
        self.assertEqual(status, 'accepted')                     # reprise non calée
        self.assertEqual(dump.call_count, 2)                     # reçu initial + recréation au pin
        self.assertEqual(restore.call_count, 2)
        self.assertIn('pin', second.steps)
        record = self.value._record('release-fixture')
        self.assertEqual(record['state'], 'retained')
        self.assertNotEqual(record['files'], avant)              # recréé, pas re-stampé
        self.assertEqual(record['files']['release-fixture.dump'],
                         hashlib.sha256(b'fresh-archive').hexdigest())
        for name, digest in record['files'].items():
            self.assertEqual(digest, recovery.backup.digest(Path(self.retention) / name))

    def fresh_dump(self, config, output, inventory):
        Path(output).write_bytes(b'fresh-archive')
        Path(output).chmod(0o600)
        document = self.write_inventory(output, inventory=inventory)
        return {'artifact_sha256': document['artifact_sha256'], 'inventory': str(inventory)}

    def test_stale_remote_attempt_reuploads_instead_of_colliding(self):
        upload_credentials, restore_credentials = self.credential_files()
        self.value = self.remote_value(upload_credentials, restore_credentials)
        with mock.patch.object(recovery.backup, 'snapshot_dump', side_effect=self.fake_dump), \
                mock.patch.object(recovery.backup, 'verify_restore', side_effect=self.fake_restore), \
                mock.patch.object(recovery.backup, 'upload', side_effect=self.fake_upload), \
                mock.patch.object(recovery.backup, 'retrieve', side_effect=self.fake_retrieve):
            self.assertTrue(self.perform('backup'))
        stale = self.stale_stamp()
        for name in self.value._pending('release-fixture')['files']:
            os.utime(Path(self.retention) / name, (stale, stale))

        def fresh_dump(config, output, inventory):
            Path(output).write_bytes(b'fresh-archive')
            Path(output).chmod(0o600)
            document = self.write_inventory(output, inventory=inventory)
            return {'artifact_sha256': document['artifact_sha256'], 'inventory': str(inventory)}

        with mock.patch.object(recovery.backup, 'snapshot_dump', side_effect=fresh_dump), \
                mock.patch.object(recovery.backup, 'verify_restore', side_effect=self.fake_restore), \
                mock.patch.object(recovery.backup, 'upload', side_effect=self.fake_upload) as upload, \
                mock.patch.object(recovery.backup, 'retrieve', side_effect=self.fake_retrieve) as retrieve:
            self.assertTrue(self.perform('backup'))
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(retrieve.call_count, 1)

    # -- K2: admission checks the target volume, not the retention one ------
    def refused_restore(self):
        """Run one backup attempt whose restore must be refused before any effect."""
        with mock.patch.object(recovery.backup, 'snapshot_dump', side_effect=self.fake_dump), \
                mock.patch.object(recovery.backup, 'verify_restore', side_effect=self.fake_restore) as restore:
            self.assertFalse(self.perform('backup'))
        restore.assert_not_called()
        return restore

    def compressible_dump(self, config, output, inventory):
        # A compressed-size check would admit this tiny archive; the pinned
        # uncompressed source size is what bounds the restored copy.
        Path(output).write_bytes(b'x')
        Path(output).chmod(0o600)
        document = self.write_inventory(
            output, value=dict(schema='public', migrations=[[1, 'd' * 64]],
                               row_counts={'agent_registry': 4},
                               artifact_sha256=hashlib.sha256(b'x').hexdigest(),
                               source_bytes=1024 * 1024 * 1024),
            inventory=inventory)
        return {'artifact_sha256': document['artifact_sha256'], 'inventory': str(inventory)}

    def test_restore_refuses_when_the_target_volume_is_not_attested(self):
        # Enrollment is valid; no fresh attestation exists yet. The retention
        # filesystem is free, but the copy lands on the restore volume.
        self.capacity_path.unlink()
        self.refused_restore()
        self.assertTrue(self.archive().exists(), 'the published artifact stays for reconciliation')
        self.assertFalse((Path(self.root) / 'release-fixture.set.json').exists())

    def test_restore_refuses_a_stale_target_volume_attestation(self):
        self.write_capacity(checked_at=self.clock() - recovery.CAPACITY_TTL_SECONDS - 1)
        self.refused_restore()

    def test_restore_refuses_an_attestation_for_another_target(self):
        self.write_capacity(target=hashlib.sha256(b'postgresql://restore@other/fixture-db').hexdigest())
        self.refused_restore()

    def test_restore_refuses_when_the_attested_free_space_is_too_small(self):
        self.write_capacity(free_bytes=recovery.MINIMUM_FREE_BYTES + 2 * 1024 * 1024 - 1)
        self.refused_restore()

    def test_restore_refuses_a_compressible_archive_under_the_attested_floor(self):
        self.write_capacity(free_bytes=recovery.MINIMUM_FREE_BYTES + 1024 * 1024 * 1024)
        with mock.patch.object(recovery.backup, 'snapshot_dump', side_effect=self.compressible_dump), \
                mock.patch.object(recovery.backup, 'verify_restore', side_effect=self.fake_restore) as restore:
            self.assertFalse(self.perform('backup'))
        restore.assert_not_called()

    def test_restore_follows_the_live_attestation_between_two_attempts(self):
        # Same enrollment, same published artifact: the first attempt is
        # admitted and fails definitively; the volume then fills and the
        # refreshed attestation refuses the second attempt before any copy.
        with mock.patch.object(recovery.backup, 'snapshot_dump', side_effect=self.fake_dump), \
                mock.patch.object(recovery.backup, 'verify_restore',
                                  side_effect=recovery.backup.BackupError('no room')) as restore:
            self.assertFalse(self.perform('backup'))
        self.assertEqual(restore.call_count, 1)
        self.write_capacity(free_bytes=recovery.MINIMUM_FREE_BYTES - 1)
        self.refused_restore()

    def test_restore_refuses_when_the_enrolled_capacity_is_not_referenced(self):
        with self.assertRaisesRegex(recovery.RecoveryError, 'exact recovery enrollment'):
            recovery.RecoverySet(Database(), store=self.store, root=self.root,
                                 retention=self.retention,
                                 config={key: value for key, value in self.config.items()
                                         if key != 'restore_capacity'},
                                 clock=self.clock)

    def forged_capacity(self, *, free_bytes=10 ** 12):
        path = self.capacity_path.with_name('forged-capacity.json')
        path.write_text(json.dumps(dict(free_bytes=free_bytes, checked_at=int(self.clock()),
                                        target_sha256=hashlib.sha256(
                                            self.config['restore_dsn'].encode()).hexdigest()),
                                   sort_keys=True))
        path.chmod(0o600)
        return path

    def test_capacity_refuses_an_attestation_under_a_third_party_writable_parent(self):
        # A correctly shaped document proves nothing when a third party can
        # rewrite its parent: it could be replaced between the path check and
        # the read. The restore refuses before any copy.
        self.write_capacity(free_bytes=0)
        self.capacity_path.parent.chmod(0o777)
        self.addCleanup(self.capacity_path.parent.chmod, 0o700)
        self.assertIsNone(self.value._capacity())
        self.refused_restore()

    def test_capacity_refuses_a_link_substituted_before_the_open(self):
        # The forged document below carries the same target and date but a huge
        # free space. Substituting it as a link once the parent chain has been
        # checked must not decide the admitted bytes: the file is opened without
        # following links and the descriptor read is checked again.
        self.write_capacity(free_bytes=0)
        forged = self.forged_capacity()
        real_open = recovery.os.open

        def substituted(path, flags, *args, **kwargs):
            if Path(path) == self.capacity_path:
                self.capacity_path.unlink()
                self.capacity_path.symlink_to(forged)
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(recovery.os, 'open', side_effect=substituted):
            self.assertIsNone(self.value._capacity())

    def test_capacity_refuses_a_hardlinked_attestation(self):
        self.write_capacity(free_bytes=0)
        os.link(self.capacity_path, self.capacity_path.with_name('capacity-link.json'))
        self.assertIsNone(self.value._capacity())

    def test_remote_verification_refuses_when_staging_has_no_room(self):
        upload_credentials, restore_credentials = self.credential_files()
        self.value = self.remote_value(upload_credentials, restore_credentials)
        real = recovery.shutil.disk_usage

        def usage(path):
            # The retention volume has room; the private staging root does not.
            free = (recovery.MINIMUM_FREE_BYTES - 1 if Path(path) == Path(self.root)
                    else recovery.MINIMUM_FREE_BYTES + 4096)
            return real(path)._replace(free=free)

        with mock.patch.object(recovery.shutil, 'disk_usage', side_effect=usage), \
                mock.patch.object(recovery.backup, 'snapshot_dump', side_effect=self.fake_dump), \
                mock.patch.object(recovery.backup, 'verify_restore', side_effect=self.fake_restore), \
                mock.patch.object(recovery.backup, 'upload', side_effect=self.fake_upload), \
                mock.patch.object(recovery.backup, 'retrieve', side_effect=self.fake_retrieve) as retrieve:
            self.assertFalse(self.perform('backup'))
        retrieve.assert_not_called()

    # -- K1: acceptance expires accepted sets that reached their window -----
    def advance_past_window(self):
        self.value.clock = lambda: 2_000_000.0 + self.config['retention_seconds'] + 1
        # The enrolled producer refreshes the target attestation at each step,
        # so moving time forward must not leave the next backup on a stale one.
        self.write_capacity(checked_at=int(self.value.clock()))

    def test_acceptance_expires_only_eligible_accepted_sets(self):
        self.assertTrue(self.backup())
        self.assertTrue(self.perform('pin'))
        self.assertTrue(self.perform('accept'))
        self.advance_past_window()
        self.assertTrue(self.backup(operation='second-release'))
        self.assertTrue(self.perform('pin', operation='second-release'))
        self.assertTrue(self.perform('accept', operation='second-release'))
        self.assertEqual(self.catalog()['release-fixture']['state'], 'deleted')
        self.assertFalse(self.archive().exists())
        self.assertEqual(self.catalog()['second-release']['state'], 'retained')

    def test_acceptance_leaves_held_sets_protected(self):
        self.assertTrue(self.backup())
        self.assertTrue(self.perform('pin'))
        self.assertTrue(self.perform('accept'))
        recovery.retention.protect(self.retention, 'release-fixture', hold=True)
        self.advance_past_window()
        self.assertTrue(self.backup(operation='second-release'))
        self.assertTrue(self.perform('pin', operation='second-release'))
        self.assertTrue(self.perform('accept', operation='second-release'))
        self.assertEqual(self.catalog()['release-fixture']['state'], 'retained')
        self.assertTrue(self.archive().exists())

    def test_acceptance_keeps_a_remote_set_without_a_deletion_adapter(self):
        upload_credentials, restore_credentials = self.credential_files()
        self.value = self.remote_value(upload_credentials, restore_credentials)
        with mock.patch.object(recovery.backup, 'snapshot_dump', side_effect=self.fake_dump), \
                mock.patch.object(recovery.backup, 'verify_restore', side_effect=self.fake_restore), \
                mock.patch.object(recovery.backup, 'upload', side_effect=self.fake_upload), \
                mock.patch.object(recovery.backup, 'retrieve', side_effect=self.fake_retrieve):
            self.assertTrue(self.perform('backup'))
        self.assertTrue(self.perform('pin'))
        self.assertTrue(self.perform('accept'))
        self.advance_past_window()
        self.assertTrue(self.perform('accept'))
        self.assertFalse(self.value.expire())
        self.assertEqual(self.catalog()['release-fixture']['state'], 'retained')
        self.assertTrue(self.archive().exists())


if __name__ == '__main__':
    unittest.main()
