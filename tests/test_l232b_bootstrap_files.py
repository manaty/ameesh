# SPDX-License-Identifier: AGPL-3.0-only
"""Real filesystem writes only beneath a private disposable fixture root."""
from tests import support
from tests import test_l232b_bootstrap as bootstrap_fixture
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

SPEC=importlib.util.spec_from_file_location('bootstrap_files',
    Path(__file__).resolve().parents[1]/'deploy/mise-a-jour/fichiers-bootstrap.py')
files=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(files)


class BootstrapFilesTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'host'; self.root.mkdir(mode=0o700)
        original=Path.lstat; ancestors=set(Path(self.temp.name).parents)
        def trusted_ancestors(path):
            info=original(path)
            if path in ancestors and stat.S_ISDIR(info.st_mode):
                fields=list(info); fields[0]&=~0o022; fields[4]=os.geteuid(); return os.stat_result(fields)
            return info
        patch=mock.patch.object(Path,'lstat',trusted_ancestors); patch.start(); self.addCleanup(patch.stop)
        self.bundle=bootstrap_fixture.bundle()
        for logical in self.bundle['files']:
            (self.root/logical.lstrip('/')).parent.mkdir(parents=True,mode=0o700,exist_ok=True)
        (self.root/'etc/machine-id').write_text(self.bundle['machine_id'])
        self.authority=bootstrap_fixture.Authority(); self.authority.denied=set()
        self.ready=True
        self.engine=files.BootstrapFiles(Path(self.temp.name)/'journal',authority=self.authority,
                                        root=self.root,readiness=lambda request:self.ready)
    def request(self,step):
        operation='bootstrap-fixture'
        return dict(operation=operation,step=step,action_id=operation+':'+step,bundle=self.bundle,
                    scope_digest=bootstrap_fixture.bootstrap.scopes(operation,self.bundle)[step])
    def perform(self,step): return self.engine.perform(step,self.request(step))
    def path(self,logical): return self.root/logical.lstrip('/')

    def test_configuration_snapshots_are_private_and_key_enabled_separately(self):
        self.perform('configuration')
        self.assertFalse(self.path(files.KEY).exists())
        for logical,info in self.bundle['files'].items():
            if logical==files.KEY: continue
            self.assertEqual(self.path(logical).read_text(),info['content'])
            self.assertEqual(stat.S_IMODE(self.path(logical).stat().st_mode),info['mode'])
        for record in self.engine.journal.glob('*.json'):
            self.assertFalse(record.stat().st_mode & 0o077)
        self.perform('enable')
        self.assertEqual(self.path(files.KEY).read_text(),self.bundle['files'][files.KEY]['content'])

    def test_enable_without_independent_readiness_fails_before_key_write(self):
        self.ready=False
        with self.assertRaisesRegex(files.FileError,'authority'): self.perform('enable')
        self.assertFalse(self.path(files.KEY).exists())
        self.engine.readiness=None
        with self.assertRaises(files.FileError): self.perform('enable')

    def test_lost_file_journal_reply_reconciles_already_published_contents(self):
        original=files.protocol.save; lost=[False]
        def fail(path,record):
            if record.get('done') and not lost[0]:
                lost[0]=True; raise OSError('simulated lost response')
            return original(path,record)
        with mock.patch.object(files.protocol,'save',side_effect=fail):
            with self.assertRaises(OSError): self.perform('configuration')
        written={logical:self.path(logical).stat().st_ino for logical in self.bundle['files'] if self.path(logical).exists()}
        self.assertTrue(self.perform('configuration')['ok'])
        for logical,inode in written.items(): self.assertEqual(self.path(logical).stat().st_ino,inode)

    def test_existing_file_requires_exact_reviewed_digest_and_mode(self):
        logical=files.delegation.WRAPPER; path=self.path(logical)
        path.write_text('# prior wrapper\n'); path.chmod(0o700)
        with self.assertRaisesRegex(files.FileError,'not reviewed'): self.perform('configuration')
        prior=dict(self.bundle['previous_files']); prior[logical]=dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),mode=0o700)
        self.bundle=files.delegation.render(self.bundle['plan'],account_ids=dict(uid=987,gid=987),previous_files=prior)
        self.perform('configuration')
        self.assertEqual(path.read_text(),self.bundle['files'][logical]['content'])
        self.perform('revoke')
        self.assertEqual(path.read_text(),'# prior wrapper\n')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o700)

    def test_revoke_removes_own_new_files_and_preserves_account_runtime_and_journals(self):
        self.perform('configuration'); self.perform('enable')
        sentinel=self.root/'preserved'; sentinel.write_text('account/runtime/fence simulation')
        receipt=self.perform('revoke'); self.assertTrue(receipt['ok'])
        self.assertEqual(self.perform('revoke'),receipt)
        for logical in self.bundle['files']: self.assertFalse(self.path(logical).exists())
        self.assertTrue(sentinel.exists()); self.assertTrue(list(self.engine.journal.glob('*.json')))

    def test_lost_revoke_reply_resumes_without_restoring_or_deleting_unrelated_files(self):
        self.perform('configuration'); self.perform('enable')
        original=files.protocol.save; lost=[False]
        def fail(path,record):
            if record.get('request',{}).get('step')=='revoke' and record.get('done') and not lost[0]:
                lost[0]=True; raise OSError('lost revoke reply')
            return original(path,record)
        with mock.patch.object(files.protocol,'save',side_effect=fail):
            with self.assertRaises(OSError): self.perform('revoke')
        self.assertFalse(self.path(files.KEY).exists())
        self.assertTrue(self.perform('revoke')['ok'])

    def test_external_changes_host_mismatch_and_symlinks_are_preserved(self):
        self.perform('configuration'); self.perform('enable')
        wrapper=self.path(files.delegation.WRAPPER); wrapper.write_text('unrelated administrator update')
        with self.assertRaisesRegex(files.FileError,'changed'): self.perform('revoke')
        self.assertEqual(wrapper.read_text(),'unrelated administrator update')
        self.assertFalse(self.path(files.KEY).exists())
        (self.root/'etc/machine-id').write_text('b'*32)
        with self.assertRaisesRegex(files.FileError,'authority'): self.perform('configuration')

    def test_revocation_during_temp_write_never_publishes_key(self):
        request=self.request('enable'); original=self.engine.snapshot
        calls=[]
        def revoke(path):
            calls.append(path)
            if len(calls)>=4: self.authority.denied.add('enable')
            return original(path)
        with mock.patch.object(self.engine,'snapshot',side_effect=revoke):
            with self.assertRaises(files.FileError): self.engine.perform('enable',request)
        self.assertFalse(self.path(files.KEY).exists())

    def test_linked_destination_or_ancestor_never_writes_referred_file(self):
        target=self.path(files.delegation.WRAPPER)
        outside=self.root/'unrelated'; outside.write_text('preserve')
        target.symlink_to(outside)
        with self.assertRaises((files.FileError,files.runtime.RuntimeError,OSError)):
            self.perform('configuration')
        self.assertEqual(outside.read_text(),'preserve')
