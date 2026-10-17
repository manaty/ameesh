# SPDX-License-Identifier: AGPL-3.0-only
"""Independent coordinator with simulated host adapters, no root/systemd/SSH."""
from tests import support
from tests import test_l232b_bootstrap_files as filesystem_fixture
from tests import test_l232b_release_controller as controller_fixture
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

SPEC=importlib.util.spec_from_file_location('release_service',
    Path(__file__).resolve().parents[1]/'deploy/mise-a-jour/service-release.py')
service=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(service)


class System:
    valid=True
    def identity(self): return dict(machine_id='a'*32,boot_id='fixture-boot',invocation_id='b'*32)
    def verify_trees(self,pins): return self.valid


class Factory:
    def __init__(self,host): self.host=host; self.capabilities=set(service.REQUIRED); self.calls=[]
    def admit(self,enrollment): return self.capabilities
    def for_operation(self,job,authority): self.calls.append(job['operation']); return self.host


class ReleaseServiceTest(unittest.TestCase):
    def setUp(self):
        base=filesystem_fixture.BootstrapFilesTest(); base.setUp(); self.addCleanup(base.doCleanups)
        self.root=Path(base.temp.name)/'controller'; self.root.mkdir(mode=0o700)
        self.operations=Path(base.temp.name)/'operations'; self.operations.mkdir(mode=0o700)
        policy=base.bundle['plan']['policy']
        self.enrollment=dict(host=policy['host'],machine_id='a'*32,policy=policy,database='fixture-db')
        self.pins=dict(runtime_tree_sha256='c'*64,source_tree_sha256='d'*64)
        manifest=dict(host=policy['host'],commit='a'*40,release='a'*40+'-'+'b'*64,database='fixture-db',
                      units=policy['units'],policy=service.restart.policy_id(policy),rollback_until=2000000000)
        self.job=dict(operation='release-fixture',decision_id=123,manifest=manifest)
        service.protocol.save(self.operations/'release-fixture.json',self.job)
        self.host=controller_fixture.Host(); self.factory=Factory(self.host); self.system=System()
        self.authority=controller_fixture.Authority(); self.now=0
        self.engine=service.IndependentService(self.root,self.operations,self.enrollment,self.pins,
            documents=service.Documents(),system=self.system,factory=self.factory,
            authority_factory=lambda job:self.authority,clock=lambda:self.now)

    def test_admission_publishes_exact_bootstrap_readiness_and_runs_signed_protocol(self):
        results=self.engine.tick()
        expected=dict(host='fixture-host',machine_id='a'*32,boot_id='fixture-boot',invocation_id='b'*32,
                      adapter_ready=True,**self.pins)
        self.assertEqual(self.engine.documents.load(self.root/'readiness.json'),expected)
        self.assertEqual(results[0]['outcome'],'accepted')
        self.assertEqual(self.host.effects,list(service.protocol.STEPS))
        self.assertEqual(self.engine.tick(),[])

    def test_partial_or_boolean_admission_never_publishes_ready_or_runs_effects(self):
        for capabilities in (True,{'restart'},set(service.REQUIRED)|{'shell'}):
            self.factory.capabilities=capabilities
            with self.assertRaises(service.ServiceError): self.engine.tick()
            self.assertFalse((self.root/'readiness.json').exists())
        self.assertEqual(self.host.effects,[])

    def test_runtime_drift_withdraws_stale_readiness_before_any_job(self):
        self.engine.admit(); self.system.valid=False
        with self.assertRaisesRegex(service.ServiceError,'drift'): self.engine.tick()
        self.assertFalse((self.root/'readiness.json').exists())
        self.assertEqual(self.host.effects,[])

    def test_lost_reply_keeps_fence_and_resumes_same_action_after_backoff(self):
        self.host.lost.add('restart')
        self.assertEqual(self.engine.tick()[0]['outcome'],'held')
        self.assertTrue(self.host.has_fence)
        record=self.engine.documents.load(self.root/'release-fixture.json')
        self.assertEqual(record['intent'],'restart')
        self.assertEqual(self.engine.tick(),[])
        self.now=31
        self.assertEqual(self.engine.tick()[0]['outcome'],'accepted')
        self.assertEqual(self.host.effects.count('restart'),1)

    def test_process_restart_reconciles_existing_job_without_repeating_effects(self):
        self.engine.tick()
        replacement=service.IndependentService(self.root,self.operations,self.enrollment,self.pins,
            documents=service.Documents(),system=self.system,factory=self.factory,
            authority_factory=lambda job:self.authority)
        self.assertEqual(replacement.tick()[0]['outcome'],'accepted')
        self.assertEqual(self.host.effects,list(service.protocol.STEPS))

    def test_protected_job_scope_and_typed_decision_are_checked_before_factory(self):
        for job in (dict(self.job,decision_id=True),dict(self.job,operation='other'),
                    dict(self.job,manifest=dict(self.job['manifest'],host='other-host')),
                    dict(self.job,command='arbitrary shell')):
            service.protocol.save(self.operations/'release-fixture.json',job)
            self.assertEqual(self.engine.tick()[0]['outcome'],'unavailable')
            self.now+=31
        self.assertEqual(self.factory.calls,[]); self.assertEqual(self.host.effects,[])

    def test_held_reservation_prioritizes_owner_over_another_queued_operation(self):
        self.host.fail.add('backup'); self.engine.tick()
        other=dict(self.job,operation='another-release')
        service.protocol.save(self.operations/'another-release.json',other)
        self.now=31; self.engine.tick()
        self.assertEqual(self.factory.calls,['release-fixture','release-fixture'])

    def test_newly_held_reservation_stops_remaining_jobs_in_same_batch(self):
        first=dict(self.job,operation='a-first-release')
        service.protocol.save(self.operations/'a-first-release.json',first)
        self.host.fail.add('backup')
        self.engine.tick()
        self.assertEqual(self.factory.calls,['a-first-release'])

    def test_withdraw_cannot_remove_later_controller_instance_readiness(self):
        first=self.engine.admit()
        later=dict(first,invocation_id='e'*32)
        service.protocol.save(self.root/'readiness.json',later)
        self.engine.withdraw()
        self.assertEqual(self.engine.documents.load(self.root/'readiness.json'),later)

    def test_factory_and_authority_errors_never_log_private_provider_text(self):
        with mock.patch.object(self.factory,'for_operation',side_effect=OSError('private-canary')):
            results=self.engine.tick()
        self.assertEqual(results,[dict(operation='release-fixture',outcome='unavailable')])
        self.assertNotIn('canary',json.dumps(results))
        self.assertEqual(self.engine.documents.load(self.root/'release-fixture.result.json'),results[0])

    def test_invalid_private_reservation_never_echoes_raw_operation_content(self):
        service.protocol.save(self.root/'fixture-host.active.json',dict(operation='private-canary; untyped',digest='f'*64))
        with self.assertRaises(service.ServiceError) as failure: self.engine.tick()
        self.assertNotIn('canary',str(failure.exception))
        self.assertEqual(self.host.effects,[])

    def test_fixed_unit_is_independent_and_binds_exact_isolated_entrypoint(self):
        unit=service.module('unite-controleur').UNIT
        self.assertIn('Slice=system.slice',unit)
        self.assertIn('ExecStart=/opt/ameesh-release/runtime/bin/python -I -B /opt/ameesh-release/lib/service-release.py',unit)
        self.assertNotIn('ameesh-agents.slice',unit)
        self.assertNotIn('EnvironmentFile=',unit)
        self.assertNotIn('%i',unit)

    def test_unenrolled_cli_refuses_before_touching_other_instance_readiness(self):
        self.engine.admit()
        with mock.patch.object(service.sys,'argv',['service-release.py']),mock.patch.object(service.os,'geteuid',return_value=1000),mock.patch('builtins.print'):
            self.assertEqual(service.main(),1)
        self.assertTrue((self.root/'readiness.json').exists())
