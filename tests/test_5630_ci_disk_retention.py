# SPDX-License-Identifier: AGPL-3.0-only
"""Rotation de vrais fichiers : ctime, inodes ouverts, archives et échecs."""
import gzip
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from .test_5325_ci_disque import disque


class RotationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / 'Worker_20261101.log'
        self.log.write_bytes(b'journal\n' * 10000)
        self.now = time.time() + 73 * 3600
        self.open_patch = mock.patch.object(disque, '_journaux_ouverts', return_value=set())
        self.open_patch.start()
        self.addCleanup(self.open_patch.stop)

    def rotate(self, **kw):
        return disque._rotation_diag([self.root], maintenant=self.now, **kw)

    def test_closed_log_compressed_losslessly(self):
        contenu = self.log.read_bytes()
        result = self.rotate()
        self.assertFalse(self.log.exists())
        archive = self.log.with_suffix('.log.gz')
        self.assertEqual(gzip.decompress(archive.read_bytes()), contenu)
        self.assertEqual(result['fichiers'], 1)
        self.assertGreater(result['octets'], 0)
        self.assertFalse(list(self.root.glob('*.part')))

    def test_open_old_listener_is_kept(self):
        info = self.log.stat()
        with mock.patch.object(disque, '_journaux_ouverts',
                               return_value={(info.st_dev, info.st_ino)}):
            self.assertEqual(self.rotate()['fichiers'], 0)
        self.assertTrue(self.log.exists())

    def test_antidated_log_symlink_and_non_log_are_kept(self):
        os.utime(self.log, (100, 100))
        (self.root / 'Runner_link.log').symlink_to(self.log)
        (self.root / 'other.log').write_text('x')
        self.now = time.time()
        self.assertEqual(self.rotate()['fichiers'], 0)
        self.assertTrue(self.log.exists())

    def test_existing_archive_is_never_overwritten(self):
        archive = self.log.with_suffix('.log.gz')
        archive.write_bytes(b'preuve')
        self.rotate()
        self.assertEqual(archive.read_bytes(), b'preuve')
        self.assertTrue(self.log.exists())
        self.assertFalse(list(self.root.glob('*.part')))

    def test_archive_has_its_own_seven_day_retention(self):
        self.rotate()
        archive = self.log.with_suffix('.log.gz')
        self.assertEqual(self.rotate()['fichiers'], 0)
        self.now = time.time() + 8 * 86400
        self.assertEqual(self.rotate()['fichiers'], 1)
        self.assertFalse(archive.exists())

    def test_dry_run_and_unreadable_process_preserve_source(self):
        self.assertEqual(self.rotate(dry_run=True)['fichiers'], 1)
        self.assertTrue(self.log.exists())
        with mock.patch.object(disque, '_journaux_ouverts', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                self.rotate()
        self.assertTrue(self.log.exists())

    def test_compression_failure_removes_partial_and_keeps_source(self):
        with mock.patch.object(disque.gzip.GzipFile, 'write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.rotate()
        self.assertTrue(self.log.exists())
        self.assertFalse(list(self.root.glob('*.part')))
        self.assertFalse(list(self.root.glob('*.gz')))

    def test_modified_source_is_not_removed(self):
        original = disque.gzip.GzipFile.write
        changed = []
        def write(gz, data):
            if not changed:
                changed.append(True)
                with self.log.open('ab') as f:
                    f.write(b'new')
            return original(gz, data)
        with mock.patch.object(disque.gzip.GzipFile, 'write', write):
            self.rotate()
        self.assertTrue(self.log.exists())
        self.assertFalse(list(self.root.glob('*.gz')))


class ProcessReadingTest(unittest.TestCase):
    def test_only_runner_descriptors_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / 'log'
            log.write_text('x')
            for pid, comm in [('1', 'Runner.Listener'), ('2', 'other')]:
                p = root / pid
                (p / 'fd').mkdir(parents=True)
                (p / 'comm').write_text(comm)
                (p / 'fd' / '1').symlink_to(log)
            original = disque._journaux_ouverts
            self.assertIn((log.stat().st_dev, log.stat().st_ino), original(root))


class InstallationTest(unittest.TestCase):
    def test_install_verify_remove_without_system_changes(self):
        import subprocess
        repo = Path(__file__).resolve().parent.parent
        installer = repo / 'deploy/ci-flotte/install-disque.sh'
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, CI_FLOTTE_TEST_ROOT=tmp)
            def run(mode):
                return subprocess.run(['bash', str(installer), mode], env=env,
                                      text=True, capture_output=True)
            self.assertNotEqual(run('--check').returncode, 0)
            installed = run('--install')
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            tool = Path(tmp) / 'usr/local/lib/ameesh-ci/ci-disque.py'
            self.assertTrue(os.access(tool, os.X_OK))
            self.assertEqual(run('--check').returncode, 0)
            tool.write_text('drift')
            self.assertNotEqual(run('--check').returncode, 0)
            self.assertEqual(run('--remove').returncode, 0)
            self.assertFalse(tool.exists())


class DiskTransportTest(unittest.TestCase):
    def setUp(self):
        from ameesh import ci_disk
        self.disk = ci_disk
        self.now = 1793700000
        self.repo = 'acme/widget'
        self.host = 'ci-a'
        self.payload = dict(schema=ci_disk.SCHEMA, host=self.host, volume='/',
                            free_bytes=1024**3, total_bytes=100 * 1024**3)

    def row(self, **over):
        out = dict(self.payload, repo=self.repo, run_id=42, sampled_ts=self.now)
        out.pop('schema')
        out.update(over)
        return out

    def annotation(self, **over):
        import json
        return dict(title=self.disk.SCHEMA, message=json.dumps(dict(self.payload, **over)))

    def test_parser_rejects_wrong_host_schema_and_invalid_sizes(self):
        for over in [dict(host='other'), dict(schema='other'), dict(free_bytes=-1),
                     dict(total_bytes=0), dict(free_bytes=True), dict(volume='/other'),
                     dict(free_bytes=2**64), dict(total_bytes=2**64)]:
            self.assertIsNone(self.disk.annotation(self.annotation(**over), self.host))
        self.assertEqual(self.disk.annotation(self.annotation(), self.host)['free_bytes'], 1024**3)

    def test_low_and_healthy_follow_native_thresholds(self):
        from ameesh import exploitation, notify
        low = self.disk.findings([self.row()], [], self.now)
        self.assertEqual([r['type'] for r in low], ['ci_disk_low'])
        self.assertIn('ci_disk_low', notify.DEFAULT_TYPES)
        self.assertEqual(exploitation.alert_key(low[0]),
                         exploitation.alert_key(dict(low[0], run_id=43, since=self.now + 3600)))
        self.assertEqual(self.disk.findings([self.row(free_bytes=80 * 1024**3)], [], self.now), [])

    def test_stale_low_stays_low_and_reports_missing_sampling(self):
        out = self.disk.findings([self.row(sampled_ts=self.now - 4 * 3600)], [], self.now)
        self.assertEqual({r['type'] for r in out}, {'ci_disk_low', 'ci_disk_unreadable'})
        self.assertEqual([r['type'] for r in self.disk.findings(
            [self.row(free_bytes=None, total_bytes=None, sampled_ts=None)], [], self.now)],
                         ['ci_disk_unreadable'])

    def test_collector_binds_annotation_to_base_workflow_job_and_timestamp(self):
        from types import SimpleNamespace
        calls, measures, expected = [], [], []
        def api(path):
            calls.append(path)
            if '/workflows/' in path:
                return {'workflow_runs': [dict(id=44, head_branch='evil', event='pull_request'),
                                          dict(id=42, head_branch='main', event='schedule')]}
            if '/jobs?' in path:
                return {'jobs': [dict(name='disque ci-a', conclusion='success',
                    completed_at='2026-11-03T10:00:00Z',
                    check_run_url='https://api.github.com/repos/acme/widget/check-runs/99')]}
            return [self.annotation(host='wrong'), self.annotation()]
        store = SimpleNamespace(expect_disk=lambda *a, **kw: expected.append(kw),
                                observe_disk=lambda *a, **kw: measures.append(kw))
        self.disk.collect(api, self.repo, 'main', store)
        self.assertEqual(len(measures), 1)
        self.assertEqual(measures[0]['run_id'], 42)
        self.assertEqual(measures[0]['host'], self.host)
        self.assertEqual(len(expected), 1)
        self.assertFalse(any('/runs/44/' in call for call in calls))
        self.assertEqual(measures[0]['sampled_ts'], 1793700000)

    def test_probe_emits_parseable_notice_without_docker(self):
        import contextlib
        import io
        import json
        output = io.StringIO()
        with mock.patch.object(disque, 'espace', return_value=dict(chemin='/', libre=1,
                                total=100, utilise_pct=99)), contextlib.redirect_stdout(output):
            disque.main(['mesure', '--host', 'ci-a'])
        data = json.loads(output.getvalue().strip().split('::')[-1])
        self.assertEqual(data['host'], 'ci-a')
        self.assertEqual((data['free_bytes'], data['used_pct']), (1, 99))

    def test_native_host_threshold_overrides_default(self):
        from types import SimpleNamespace
        canon = SimpleNamespace(id='test', host=lambda name: SimpleNamespace(
            policy={'resources': {'warn_disk_free': '90 GiB', 'warn_disk_used': '95%'}}))
        rows = self.disk.findings([self.row(free_bytes=80 * 1024**3)], [canon], self.now)
        self.assertEqual([r['type'] for r in rows], ['ci_disk_low'])

    def test_watcher_collects_on_a_bounded_cadence(self):
        from ameesh import ci
        from types import SimpleNamespace
        watcher = ci.Watcher(SimpleNamespace(), api=lambda path: None, clock=lambda: self.now)
        with mock.patch.object(watcher, 'discovered_workflows', return_value=(self.disk.WORKFLOW,)), \
                mock.patch.object(ci.storage, 'of', return_value=SimpleNamespace(ci=object())), \
                mock.patch.object(self.disk, 'collect') as collect:
            watcher._disks(object(), self.repo, ('main',))
            watcher._disks(object(), self.repo, ('main',))
            collect.assert_called_once()
            self.assertEqual(collect.call_args.args[2], 'main')
