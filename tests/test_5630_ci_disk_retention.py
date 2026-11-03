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
