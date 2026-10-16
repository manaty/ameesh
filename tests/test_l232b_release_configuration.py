# SPDX-License-Identifier: AGPL-3.0-only
"""Real disposable file effects; signed authority and DB gate simulated here."""
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock
from tests import test_l232b_bootstrap_files as fixture

SPEC=importlib.util.spec_from_file_location('release_configuration',
    Path(__file__).resolve().parents[1]/'deploy/mise-a-jour/configuration-release.py')
config=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(config)


class Authority:
    allowed=True
    def validate(self,*args): return self.allowed


class Gate:
    active=False
    allowed=True
    @contextmanager
    def guard(self,request):
        if not self.allowed: raise ValueError('no live fence')
        self.active=True
        try: yield
        finally: self.active=False


class ReleaseConfigurationTest(unittest.TestCase):
    def setUp(self):
        base=fixture.BootstrapFilesTest(); base.setUp(); self.addCleanup(base.doCleanups)
        self.base=base; self.authority=Authority(); self.gate=Gate()
        self.destinations=dict(unit='/etc/systemd/system/fixture.service',guard='/etc/ameesh/guard.json')
        for logical in self.destinations.values():
            (base.root/logical.lstrip('/')).parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.unit=base.root/self.destinations['unit'].lstrip('/')
        self.guard=base.root/self.destinations['guard'].lstrip('/')
        self.unit.write_text('prior unit\n'); self.unit.chmod(0o640)
        self.release=dict(commit='a'*40,configuration=dict(
            unit=dict(content='next unit\n',mode=0o644),guard=dict(content='{"guard":true}',mode=0o600)))
        self.manifest=dict(host='fixture-host',commit='a'*40,release='a'*40+'-'+hashlib.sha256(
            json.dumps(self.release,sort_keys=True).encode()).hexdigest(),database='fixture-db',
            units=['fixture.service'],policy='b'*64,rollback_until=2000000000)
        self.engine=config.ConfigurationFiles(Path(base.temp.name)/'configuration',self.destinations,
            authority=self.authority,gate=self.gate,root=base.root,clock=lambda:1900000000)
        self.op='release-fixture'
    def request(self,step):
        return dict(operation=self.op,manifest_digest=config.protocol.identity(self.manifest),
                    action_id=self.op+':'+step,manifest=self.manifest,step=step)
    def perform(self,step): return self.engine.perform(self.request(step),self.release)

    def test_pin_is_private_and_rollback_restores_exact_prior_bytes_and_mode(self):
        self.perform('pin'); self.assertFalse(self.guard.exists())
        record=self.engine.journal/(self.op+'.configuration.json')
        self.assertFalse(record.stat().st_mode & 0o077)
        self.perform('configure'); self.assertEqual(self.unit.read_text(),'next unit\n')
        self.assertTrue(self.guard.exists())
        self.perform('rollback'); self.assertEqual(self.unit.read_text(),'prior unit\n')
        self.assertEqual(self.unit.stat().st_mode & 0o777,0o640)
        self.assertFalse(self.guard.exists())
        self.assertTrue(self.perform('rollback')['files_reconciled'])
        with self.assertRaisesRegex(config.ConfigurationError,'recovering'): self.perform('configure')

    def test_no_effect_without_pin_live_gate_and_fresh_authority(self):
        with self.assertRaisesRegex(config.ConfigurationError,'pin required'): self.perform('configure')
        self.perform('pin'); self.gate.allowed=False
        with self.assertRaisesRegex(ValueError,'fence'): self.perform('configure')
        self.assertEqual(self.unit.read_text(),'prior unit\n'); self.assertFalse(self.guard.exists())
        self.gate.allowed=True; self.authority.allowed=False
        with self.assertRaisesRegex(config.ConfigurationError,'authority'): self.perform('configure')
        self.assertFalse(self.guard.exists())

    def test_target_bytes_bound_to_signed_release_and_enrolled_inventory(self):
        self.perform('pin'); self.release['configuration']['guard']['content']='different'
        with self.assertRaisesRegex(config.ConfigurationError,'identity'): self.perform('configure')
        self.assertFalse(self.guard.exists())
        with self.assertRaises(config.ConfigurationError):
            config.ConfigurationFiles(self.engine.journal,dict(injected='/etc/sudoers'),
                authority=self.authority,gate=self.gate,root=self.base.root)

    def test_lost_reply_after_replace_reconciles_without_rewriting_and_restores(self):
        self.perform('pin'); original=config.protocol.save; lost=[False]
        def save(path,record):
            if record.get('phase')=='configure' and record['done'] and not lost[0]:
                lost[0]=True; raise OSError('lost filesystem receipt')
            return original(path,record)
        with mock.patch.object(config.protocol,'save',side_effect=save):
            with self.assertRaises(OSError): self.perform('configure')
        inode=self.guard.stat().st_ino
        self.perform('configure'); self.assertEqual(self.guard.stat().st_ino,inode)
        self.perform('rollback'); self.assertFalse(self.guard.exists())

    def test_partial_configuration_failure_can_restore_unmodified_and_written_files(self):
        self.perform('pin'); original=self.engine.replace
        def replace(path,*args):
            self.assertTrue(self.gate.active)
            if path==self.unit: raise OSError('publication unavailable')
            return original(path,*args)
        with mock.patch.object(self.engine,'replace',side_effect=replace):
            with self.assertRaises(OSError): self.perform('configure')
        self.assertTrue(self.guard.exists()); self.assertEqual(self.unit.read_text(),'prior unit\n')
        self.perform('rollback'); self.assertFalse(self.guard.exists())
        self.assertEqual(self.unit.read_text(),'prior unit\n')

    def test_rollback_preserves_external_change_and_enforces_deadline(self):
        self.perform('pin'); self.perform('configure'); self.unit.write_text('external unit\n')
        with self.assertRaisesRegex(config.ConfigurationError,'outside operation'): self.perform('rollback')
        self.assertEqual(self.unit.read_text(),'external unit\n')
        self.engine.clock=lambda:2000000001
        with self.assertRaisesRegex(config.ConfigurationError,'authority'): self.perform('rollback')

    def test_repin_keeps_original_snapshot_and_changed_destination_is_refused(self):
        self.perform('pin'); self.perform('configure'); self.perform('pin'); self.perform('rollback')
        self.assertEqual(self.unit.read_text(),'prior unit\n')
        self.engine.destinations['unit']='/etc/systemd/system/other.service'
        with self.assertRaisesRegex(config.ConfigurationError,'journal mismatch'): self.perform('pin')

    def test_signature_revoked_during_copy_prevents_publication(self):
        self.perform('pin'); original=config.os.fchmod
        def revoke(fd,mode):
            original(fd,mode); self.authority.allowed=False
        with mock.patch.object(config.os,'fchmod',side_effect=revoke):
            with self.assertRaisesRegex(config.ConfigurationError,'authority'): self.perform('configure')
        self.assertFalse(self.guard.exists()); self.assertEqual(self.unit.read_text(),'prior unit\n')

    def test_symlink_hardlink_and_completed_file_drift_fail_closed(self):
        self.perform('pin'); self.guard.symlink_to(self.unit)
        with self.assertRaises(Exception): self.perform('configure')
        self.guard.unlink(); self.perform('configure'); self.guard.chmod(0o640)
        with self.assertRaisesRegex(config.ConfigurationError,'completed'): self.perform('configure')

    def test_hardlinked_destination_is_rejected_before_snapshot(self):
        import os
        os.link(self.unit,self.unit.with_name('other-link'))
        with self.assertRaisesRegex(config.ConfigurationError,'untrusted'): self.perform('pin')
        self.assertFalse((self.engine.journal/(self.op+'.configuration.json')).exists())

    def test_authorization_wait_cannot_hide_external_edit_before_removal(self):
        self.perform('pin'); self.perform('configure')
        original=self.engine.approved; calls=[0]
        def approved(request):
            original(request)
            # Removal checks authority after its first read. An independently
            # changed file during that wait must be retained.
            if self.gate.active:
                calls[0]+=1
                if calls[0]==2:
                    self.guard.write_text('external guard')
        with mock.patch.object(self.engine,'approved',side_effect=approved):
            with self.assertRaisesRegex(config.ConfigurationError,'before removal'):
                self.perform('rollback')
        self.assertEqual(self.guard.read_text(),'external guard')

    def test_lost_removal_receipt_reconciles_already_absent_file(self):
        self.perform('pin'); self.perform('configure')
        original=config.protocol.save; lost=[False]
        def save(path,record):
            if record.get('phase')=='rollback' and record['done'] and not lost[0]:
                lost[0]=True; raise OSError('lost removal receipt')
            return original(path,record)
        with mock.patch.object(config.protocol,'save',side_effect=save):
            with self.assertRaises(OSError): self.perform('rollback')
        self.assertFalse(self.guard.exists())
        self.perform('rollback'); self.assertEqual(self.unit.read_text(),'prior unit\n')
