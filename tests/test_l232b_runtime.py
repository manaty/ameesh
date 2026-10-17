# SPDX-License-Identifier: AGPL-3.0-only
from tests import support
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('release_runtime',
        Path(__file__).resolve().parents[1]/'deploy/mise-a-jour/runtime-release.py')
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.make_writable,root)
        self.root = root
        original = Path.lstat
        ancestors = set(root.parents)
        def trusted_ancestors(path):
            info = original(path)
            if path in ancestors and stat.S_ISDIR(info.st_mode):
                fields = list(info); fields[0] &= ~0o022; fields[4] = os.geteuid()
                return os.stat_result(fields)
            return info
        patch = mock.patch.object(Path,'lstat',trusted_ancestors)
        patch.start(); self.addCleanup(patch.stop)
        self.source = root/'source'
        (self.source/'bin').mkdir(parents=True,mode=0o700)
        (self.source/'lib').mkdir(mode=0o700)
        (self.source/'bin/python').write_bytes(b'synthetic executable, never launched')
        (self.source/'bin/python').chmod(0o700)
        (self.source/'lib/module.py').write_bytes(b'# synthetic library\n')
        self.target,self.journal = root/'runtime',root/'journal'
        self.digest = runtime.inventory(self.source)['digest']

    @staticmethod
    def make_writable(root):
        for directory,dirs,names in os.walk(root):
            Path(directory).chmod(0o700)

    def publish(self,digest=None,operation='bootstrap-runtime'):
        return runtime.publish(self.source,self.target,self.journal,operation,digest or self.digest,
                               authorize=lambda:True)

    def test_exact_copy_is_read_only_and_replay_does_not_need_staging_source(self):
        receipt = self.publish()
        self.assertTrue(receipt['ok'])
        self.assertEqual(runtime.inventory(self.target)['digest'],self.digest)
        self.assertEqual((self.target/'bin/python').stat().st_mode & 0o777,0o555)
        self.assertEqual((self.target/'lib/module.py').stat().st_mode & 0o777,0o444)
        for directory,dirs,files in os.walk(self.target):
            self.assertFalse(Path(directory).stat().st_mode & 0o222)
        shutil.rmtree(self.source)
        self.assertEqual(self.publish(),receipt)

    def test_changed_digest_refused_before_journal_or_copy(self):
        with self.assertRaisesRegex(runtime.RuntimeError,'digest mismatch'):
            self.publish(digest='f'*64)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.journal.glob('*.json')))

    def test_symlink_hardlink_fifo_and_writable_directory_are_refused(self):
        with tempfile.TemporaryDirectory(dir=self.root) as scratch:
            root = Path(scratch)
            (root/'regular').write_text('test')
            for kind in ('symlink','hardlink','fifo','directory'):
                extra = root/'unsafe'
                if kind=='symlink': extra.symlink_to(root/'regular')
                if kind=='hardlink': os.link(root/'regular',extra)
                if kind=='fifo': os.mkfifo(extra)
                if kind=='directory': extra.mkdir(mode=0o777); extra.chmod(0o777)
                with self.assertRaises(runtime.RuntimeError):
                    runtime.inventory(root)
                if extra.is_dir(): extra.rmdir()
                else: extra.unlink()

    def test_entry_and_byte_admission_include_empty_directories(self):
        with self.assertRaisesRegex(runtime.RuntimeError,'oversized'):
            runtime.inventory(self.source,max_bytes=2)
        with self.assertRaisesRegex(runtime.RuntimeError,'entry budget'):
            runtime.inventory(self.source,max_files=2)
        for value in (0,True,-1):
            with self.assertRaises(runtime.RuntimeError):
                runtime.inventory(self.source,max_bytes=value)

    def test_unowned_existing_destination_and_overlapping_trees_are_refused(self):
        self.target.mkdir()
        (self.target/'unrelated').write_text('preserve')
        with self.assertRaisesRegex(runtime.RuntimeError,'unowned'):
            self.publish()
        self.assertEqual((self.target/'unrelated').read_text(),'preserve')
        with self.assertRaisesRegex(runtime.RuntimeError,'separate'):
            runtime.publish(self.source,self.source/'nested',self.journal,'nested',self.digest,
                            authorize=lambda:True)

    def test_lost_reply_after_rename_reconciles_intent_without_second_copy(self):
        original = runtime.protocol.save
        def lost(path,record):
            if record.get('receipt'):
                raise OSError('lost receipt')
            return original(path,record)
        with mock.patch.object(runtime.protocol,'save',side_effect=lost):
            with self.assertRaises(OSError): self.publish()
        self.assertTrue(self.target.exists())
        inode = (self.target/'bin/python').stat().st_ino
        with mock.patch.object(runtime,'digest_file',wraps=runtime.digest_file) as read:
            self.assertTrue(self.publish()['ok'])
            self.assertTrue(all(call.kwargs.get('output') is None for call in read.call_args_list))
        self.assertEqual((self.target/'bin/python').stat().st_ino,inode)

    def test_source_change_during_copy_preserves_unresolved_stage(self):
        original = runtime.digest_file
        def changed(path,**kwargs):
            if kwargs.get('output') is not None:
                Path(path).write_bytes(b'changed approved input')
            return original(path,**kwargs)
        with mock.patch.object(runtime,'digest_file',side_effect=changed):
            with self.assertRaises(runtime.RuntimeError): self.publish()
        self.assertFalse(self.target.exists())
        stage = list(self.root.glob('.*.pending'))
        self.assertEqual(len(stage),1)
        with self.assertRaisesRegex(runtime.RuntimeError,'inspect retained stage'):
            self.publish()
        self.assertTrue(stage[0].exists())

    def test_completed_tree_or_executable_mode_drift_is_refused(self):
        self.publish()
        program = self.target/'bin/python'
        program.chmod(0o755)
        with self.assertRaisesRegex(runtime.RuntimeError,'writable'):
            self.publish()
        program.chmod(0o444)
        with self.assertRaisesRegex(runtime.RuntimeError,'changed'):
            self.publish()

    def test_another_operation_cannot_adopt_an_existing_tree(self):
        self.publish()
        with self.assertRaisesRegex(runtime.RuntimeError,'unowned'):
            self.publish(operation='different-operation')

    def test_low_disk_space_refuses_before_persisting_intent(self):
        space = mock.Mock(f_bavail=0,f_frsize=4096)
        with mock.patch.object(runtime.os,'statvfs',return_value=space):
            with self.assertRaisesRegex(runtime.RuntimeError,'capacity'):
                self.publish()
        self.assertFalse(list(self.journal.glob('*.json')))
        self.assertFalse(list(self.root.glob('.*.pending')))

    def test_authority_required_and_revocation_after_copy_retains_stage(self):
        with self.assertRaisesRegex(runtime.RuntimeError,'authority'):
            runtime.publish(self.source,self.target,self.journal,'bootstrap-runtime',self.digest)
        self.assertFalse(self.journal.exists())
        calls = []
        def revoked():
            calls.append(True)
            return len(calls)<3
        with self.assertRaisesRegex(runtime.RuntimeError,'authority'):
            runtime.publish(self.source,self.target,self.journal,'bootstrap-runtime',self.digest,
                            authorize=revoked)
        self.assertFalse(self.target.exists())
        self.assertEqual(len(list(self.root.glob('.*.pending'))),1)
