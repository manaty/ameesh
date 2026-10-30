# SPDX-License-Identifier: AGPL-3.0-only
"""Version-specific HTTP simulations; never contacts Scaleway or real credentials."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tests import support
from tests.test_l232b_retention import retention

PATH = Path(__file__).resolve().parents[1] / 'deploy/mise-a-jour/retention-scaleway.py'
SPEC = importlib.util.spec_from_file_location('scaleway_retention', PATH)
backend = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backend)


class ScalewayRetentionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ameesh-scw-retention-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.staging = self.root / 'staging'
        self.credentials = self.root / 'retention.json'
        self.settings = dict(access_key='SYNTHETIC-ACCESS', secret_key='SYNTHETIC-SECRET',
                             bucket='fixture-private', region='fr-par', purpose='retention-versions',
                             allowed_hosts=['test-host'])
        self.credentials.write_text(json.dumps(self.settings))
        self.credentials.chmod(0o600)
        self.objects = {'old/+?version': b'pinned private dump', 'new-version': b'newer dump'}
        self.sha = hashlib.sha256(self.objects['old/+?version']).hexdigest()
        self.calls = []
        self.force_status = None
        self.header_version = None
        self.keep_deleted = False
        self.lose_delete = False
        self.before_delete = None
        patcher = mock.patch.object(backend.subprocess, 'run', side_effect=self.transport)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.adapter = backend.VersionRetention(self.credentials, self.staging, max_bytes=4096)

    def transport(self, args, **kw):
        if args == ['/usr/bin/curl', '--version']:
            return subprocess.CompletedProcess(args, 0, stdout='curl 8.4.0 (fixture)')
        self.assertEqual(args[:2], ['/usr/bin/curl', '--disable'])
        self.assertNotIn('--location', args)
        self.assertNotIn('--insecure', args)
        self.assertEqual(kw['env'], {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        self.assertLessEqual(kw['timeout'], 130)
        self.assertEqual(kw['stderr'], subprocess.DEVNULL)
        for canary in ('SYNTHETIC-ACCESS', 'SYNTHETIC-SECRET'):
            self.assertNotIn(canary, repr(args))
        config = Path(args[args.index('--config') + 1])
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(config.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn('SYNTHETIC-SECRET', config.read_text())
        self.assertEqual(args[args.index('--proto') + 1], '=https')
        self.assertEqual(args[args.index('--max-filesize') + 1], '4096')
        self.assertEqual(args[args.index('-H') + 1],
                         'x-amz-content-sha256: ' + backend.EMPTY_SHA256)
        url = urlsplit(args[-1])
        self.assertEqual(url.hostname, 'fixture-private.s3.fr-par.scw.cloud')
        self.assertEqual(url.path, '/test-host/old.dump')
        query = parse_qs(url.query)
        self.assertEqual(set(query), {'versionId'})
        version = query['versionId'][0]
        method = args[args.index('--request') + 1]
        self.calls.append((method, version))
        body = Path(args[args.index('--output') + 1])
        headers = Path(args[args.index('--dump-header') + 1])
        status = self.force_status
        if status:
            body.write_bytes(b'<Error><Code>NoSuchBucket</Code></Error>')
        elif method == 'DELETE':
            if self.before_delete:
                self.before_delete()
            if not self.keep_deleted:
                self.objects.pop(version, None)
            if self.lose_delete:
                self.lose_delete = False
                raise subprocess.TimeoutExpired(args, 130, output='SYNTHETIC-SECRET')
            status = '204'
        elif version in self.objects:
            body.write_bytes(self.objects[version])
            headers.write_text('HTTP/1.1 200 OK\r\nx-amz-version-id: ' + (self.header_version or version) + '\r\n\r\n')
            status = '200'
        else:
            body.write_bytes(b'<Error xmlns="fixture"><Code>NoSuchVersion</Code></Error>')
            status = '404'
        return subprocess.CompletedProcess(args, 0, stdout=status)

    def register(self, version='old/+?version', artifact='release-old', filename='old.dump'):
        path = self.root / filename
        path.write_bytes(b'pinned private dump')
        path.chmod(0o600)
        retention.register(self.root, artifact, [filename], expires_at=10, rollback_until=20,
                           remote=[dict(object_id='test-host/old.dump', sha256=self.sha, version_id=version)])
        retention.protect(self.root, artifact, accepted=True)
        return path

    def test_expiry_deletes_only_pinned_version_and_removes_private_staging(self):
        path = self.register()
        self.assertEqual(retention.prune(self.root, now=30, remote_adapter=self.adapter), ['release-old'])
        self.assertFalse(path.exists())
        self.assertEqual(self.objects, {'new-version': b'newer dump'})
        self.assertEqual(list(self.staging.iterdir()), [])
        self.assertTrue(all(version == 'old/+?version' for _, version in self.calls))
        catalog = (self.root / '.retention.json').read_text()
        self.assertIn('old/+?version', catalog)
        self.assertNotIn('SYNTHETIC-SECRET', catalog)

    def test_newer_key_version_during_delete_survives(self):
        path = self.register()
        self.before_delete = lambda: self.objects.update({'new-version': b'concurrent update'})
        retention.prune(self.root, now=30, remote_adapter=self.adapter)
        self.assertFalse(path.exists())
        self.assertEqual(self.objects, {'new-version': b'concurrent update'})

    def test_lost_delete_reply_retains_local_then_reconciles_absent_version(self):
        path = self.register()
        self.lose_delete = True
        with self.assertRaisesRegex(backend.StorageError, '^storage request failed$'):
            retention.prune(self.root, now=30, remote_adapter=self.adapter)
        self.assertTrue(path.exists())
        self.assertNotIn('old/+?version', self.objects)
        self.assertEqual(json.loads((self.root / '.retention.json').read_text())['release-old']['state'], 'deleting')
        self.assertEqual(list(self.staging.iterdir()), [])
        self.assertEqual(retention.prune(self.root, now=31, remote_adapter=self.adapter), ['release-old'])
        self.assertEqual(self.calls.count(('DELETE', 'old/+?version')), 1)
        self.assertEqual(self.objects, {'new-version': b'newer dump'})

    def test_missing_version_before_intent_blocks_expiry(self):
        path = self.register()
        self.objects.pop('old/+?version')
        with self.assertRaisesRegex(retention.RetentionError, 'missing'):
            retention.prune(self.root, now=30, remote_adapter=self.adapter)
        self.assertTrue(path.exists())
        self.assertEqual(json.loads((self.root / '.retention.json').read_text())['release-old']['state'], 'retained')

    def test_wrong_digest_or_ignored_version_query_never_deletes(self):
        path = self.register()
        self.objects['old/+?version'] = b'wrong content'
        with self.assertRaises(retention.RetentionError):
            retention.prune(self.root, now=30, remote_adapter=self.adapter)
        self.objects['old/+?version'] = b'pinned private dump'
        self.header_version = 'new-version'
        with self.assertRaisesRegex(backend.StorageError, 'retrieval failed'):
            retention.prune(self.root, now=30, remote_adapter=self.adapter)
        self.assertTrue(path.exists())
        self.assertFalse(any(method == 'DELETE' for method, _ in self.calls))

    def test_failed_or_unconfirmed_deletion_retains_local(self):
        path = self.register()
        self.keep_deleted = True
        with self.assertRaisesRegex(backend.StorageError, 'not confirmed'):
            retention.prune(self.root, now=30, remote_adapter=self.adapter)
        self.assertTrue(path.exists())
        self.keep_deleted = False
        retention.prune(self.root, now=30, remote_adapter=self.adapter)
        self.assertFalse(path.exists())

    def test_bucket_absence_and_redirect_do_not_prove_version_absence(self):
        path = self.register()
        for status in ('404', '302', '403', '500'):
            with self.subTest(status=status):
                self.force_status = status
                with self.assertRaises(backend.StorageError):
                    retention.prune(self.root, now=30, remote_adapter=self.adapter)
                self.assertTrue(path.exists())
                self.assertFalse(any(method == 'DELETE' for method, _ in self.calls))
                self.assertEqual(list(self.staging.iterdir()), [])

    def test_unqualified_null_version_and_other_host_are_refused_without_network(self):
        for version in (None, '', 'null', 'NULL', 'bad\nversion'):
            with self.subTest(version=version), self.assertRaises(backend.StorageError):
                self.adapter.inspect('test-host/old.dump', version_id=version)
        with self.assertRaisesRegex(backend.StorageError, 'outside'):
            self.adapter.inspect('another-host/old.dump', version_id='v1')
        self.assertEqual(self.calls, [])

    def test_wrong_version_receipt_does_not_unlock_local_expiry(self):
        path = self.register()
        original = self.adapter.delete_if_digest
        def wrong(*args, **kw):
            return dict(original(*args, **kw), version_id='other-version')
        with mock.patch.object(self.adapter, 'delete_if_digest', side_effect=wrong), self.assertRaises(retention.RetentionError):
            retention.prune(self.root, now=30, remote_adapter=self.adapter)
        self.assertTrue(path.exists())
        retention.prune(self.root, now=31, remote_adapter=self.adapter)
        self.assertFalse(path.exists())

    def test_staging_admission_and_response_size_hold_without_local_expiry(self):
        path = self.register()
        capacity = mock.Mock(f_bavail=0, f_frsize=4096)
        with mock.patch.object(backend.os, 'statvfs', return_value=capacity), self.assertRaisesRegex(backend.StorageError, 'capacity'):
            retention.prune(self.root, now=30, remote_adapter=self.adapter)
        self.assertEqual(self.calls, [])
        self.assertTrue(path.exists())
        self.objects['old/+?version'] = b'x' * 4097
        with self.assertRaisesRegex(backend.StorageError, 'budget'):
            retention.prune(self.root, now=30, remote_adapter=self.adapter)
        self.assertTrue(path.exists())
        self.assertEqual(list(self.staging.iterdir()), [])
        self.assertFalse(any(method == 'DELETE' for method, _ in self.calls))

    def test_unsafe_staging_and_linked_credentials_are_refused(self):
        self.staging.chmod(0o755)
        with self.assertRaisesRegex(backend.StorageError, 'private owned on-disk'):
            backend.VersionRetention(self.credentials, self.staging, max_bytes=4096)
        self.staging.chmod(0o700)
        link = self.root / 'linked.json'
        link.symlink_to(self.credentials)
        with self.assertRaises(OSError):
            backend.VersionRetention(link, self.staging, max_bytes=4096)
        self.assertEqual(self.calls, [])

    def test_private_credentials_dedicated_purpose_and_curl_version_required(self):
        self.credentials.chmod(0o644)
        with self.assertRaisesRegex(backend.StorageError, 'private owned'):
            backend.VersionRetention(self.credentials, self.staging, max_bytes=4096)
        self.credentials.chmod(0o600)
        self.credentials.write_text(json.dumps(dict(self.settings, purpose='upload')))
        with self.assertRaisesRegex(backend.StorageError, 'separate'):
            backend.VersionRetention(self.credentials, self.staging, max_bytes=4096)
        with mock.patch.object(backend.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='curl 7.74.0')):
            with self.assertRaisesRegex(backend.StorageError, '7.75'):
                backend.VersionRetention(self.credentials, self.staging, max_bytes=4096)
        # Debian 12's curl: signed transfers are accepted and the response size
        # is bounded by the child's own RLIMIT_FSIZE, not by a curl version.
        self.credentials.write_text(json.dumps(self.settings))
        self.credentials.chmod(0o600)
        with mock.patch.object(backend.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='curl 7.88.1')):
            self.assertIsInstance(backend.VersionRetention(self.credentials, self.staging, max_bytes=4096),
                                  backend.VersionRetention)

    def test_curl_child_carries_a_kernel_file_size_bound(self):
        limits = []
        def capture(what, values):
            limits.append((what, values))
        def with_child(args, **kw):
            if args != ['/usr/bin/curl', '--version']:
                kw['preexec_fn']()
            return self.transport(args, **kw)
        with mock.patch.object(backend.resource, 'setrlimit', side_effect=capture), \
                mock.patch.object(backend.subprocess, 'run', side_effect=with_child):
            self.adapter.inspect('test-host/old.dump', version_id='old/+?version')
        self.assertTrue(limits)
        self.assertEqual(limits[0][0], backend.resource.RLIMIT_FSIZE)
        self.assertLessEqual(limits[0][1][0], 4096 + 65537)

    def test_catalog_rejects_null_duplicate_and_mixed_version_references(self):
        self.register()
        for version in ('null', 'old/+?version', None):
            path = self.root / 'other.dump'
            path.write_bytes(b'pinned private dump')
            path.chmod(0o600)
            item = dict(object_id='test-host/old.dump', sha256=self.sha)
            if version is not None:
                item['version_id'] = version
            with self.subTest(version=version), self.assertRaises(retention.RetentionError):
                retention.register(self.root, 'other', ['other.dump'], expires_at=10, rollback_until=20, remote=[item])
        self.objects['second-pinned'] = b'pinned private dump'
        self.register(version='second-pinned', artifact='second-release', filename='second.dump')
        retention.protect(self.root, 'second-release', hold=True)
        retention.prune(self.root, now=30, remote_adapter=self.adapter)
        self.assertEqual(self.objects['second-pinned'], b'pinned private dump')
        self.assertTrue((self.root / 'second.dump').exists())


if __name__ == '__main__':
    unittest.main()
