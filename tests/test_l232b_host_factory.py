# SPDX-License-Identifier: AGPL-3.0-only
"""Pinned host factory: real protected files, simulated host backends.

No root, SSH, systemd, PostgreSQL migration or production effect is executed.
The disposable fixture root is the only filesystem touched.
"""
from tests import support
from tests import test_l232b_bootstrap_files as fixture
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest import mock

SPEC=importlib.util.spec_from_file_location('host_factory',
    Path(__file__).resolve().parents[1]/'deploy/mise-a-jour/adaptateur-hote-release.py')
factory=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(factory)
SERVICE_SPEC=importlib.util.spec_from_file_location('release_service',
    Path(__file__).resolve().parents[1]/'deploy/mise-a-jour/service-release.py')
service=importlib.util.module_from_spec(SERVICE_SPEC); SERVICE_SPEC.loader.exec_module(service)
# Le pilote psql n'installe pas psycopg : le parseur libpq manque alors et le
# compagnon de sauvegarde doit refuser nommément (fail-closed), pas être sauté.
HAVE_PSYCOPG=importlib.util.find_spec('psycopg') is not None


class Authority:
    allowed=True
    def validate(self,*args): return self.allowed


class Database:
    reachable=True
    def __init__(self,dsn='postgresql://controller@localhost/fixture-db'):
        # The release connection names the database and schema the backup
        # enrollment must match (K7): the factory binds no recovery set whose
        # source is another database or an unavailable schema.
        self.cfg=SimpleNamespace(dsn=dsn,schema='public')
    def ping(self):
        if not self.reachable: raise OSError('fixture database unavailable')
    def query(self,sql,params=()):
        if not self.reachable: raise OSError('fixture database unavailable')
        return [{'fences':0}]


class System:
    """Fixed executables are real files; systemd effects stay simulated."""
    def __init__(self,systemctl,analyze):
        self.systemctl=str(systemctl); self.analyze=str(analyze); self.overrides={}
    def verify(self,files,seconds): return 0
    def reload(self,seconds): return 0
    def show(self,unit,properties,seconds):
        values=dict(FragmentPath='',DropInPaths='',LoadState='loaded',
                    NeedDaemonReload='no',ActiveState='active')
        values.update(self.overrides.get(unit,{}))
        return values


class Fence:
    def __init__(self): self.steps=[]; self.probes=[]
    def perform(self,step,request,*,drain_seconds=0,probe=None):
        self.steps.append(step); self.probes.append(probe); return dict(request,ok=True)
    def fenced(self,request): return True
    @contextmanager
    def guard(self,request):
        self.steps.append('guard:'+request['step']); yield


class Transport:
    def __init__(self): self.steps=[]
    def perform(self,step,request): self.steps.append(step); return dict(request,ok=True)


class Companion:
    def __init__(self,result=True): self.steps=[]; self.result=result
    def perform(self,step,request,release):
        self.steps.append(step)
        if isinstance(self.result,Exception): raise self.result
        return self.result


class Files:
    def __init__(self,result=True): self.steps=[]; self.result=result
    def perform(self,request,release):
        self.steps.append(request['step'])
        if isinstance(self.result,Exception): raise self.result
        return {'files_reconciled':True}


class HostConfiguration:
    def __init__(self,result=True): self.steps=[]; self.result=result
    def perform(self,request,release):
        self.steps.append(request['step'])
        if isinstance(self.result,Exception): raise self.result
        return {'units':1}


class HostFactoryTest(unittest.TestCase):
    def setUp(self):
        base=fixture.BootstrapFilesTest(); base.setUp(); self.addCleanup(base.doCleanups)
        self.base=base; self.temp=Path(base.temp.name); self.host='fixture-host'
        self.policy=dict(host=self.host,units=['ameesh-runner@one.service'],
                         controller_unit='ameesh-release-controller.service',manager={'kind':'system'},restart_seconds=30)
        self.enrollment_root=self.temp/'enrolled'; self.enrollment_root.mkdir(mode=0o700)
        self.store=self.temp/'store'; self.store.mkdir(mode=0o700); self.store.chmod(0o711)
        self.journal=self.temp/'configuration'; self.journal.mkdir(mode=0o700)
        self.backup=self.temp/'backup'; self.backup.mkdir(mode=0o700)
        self.retention=self.temp/'retention'; self.retention.mkdir(mode=0o700)
        self.bindir=self.temp/'bin'; self.bindir.mkdir(mode=0o700)
        for name in ('systemctl','systemd-analyze'):
            path=self.bindir/name; path.write_text('#!/bin/sh\nexit 0\n'); path.chmod(0o700)
        self.system=System(self.bindir/'systemctl',self.bindir/'systemd-analyze')
        self.db=Database(); self.authority=Authority()
        self.destinations=dict(unit='/etc/systemd/system/fixture.service',
                               guard='/etc/ameesh/guard.json')
        self.systemroot=self.temp/'system'
        for logical in self.destinations.values():
            (self.systemroot/logical.lstrip('/')).parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.card=dict(units=['ameesh-runner@one.service'],properties={},guard=None,seconds=30)
        self.known_hosts=self.temp/'known_hosts'; self.known_hosts.write_text('host ssh-ed25519 AAAA\n')
        self.identity=self.temp/'identity'; self.identity.write_text('PRIVATE-KEY-CANARY\n')
        for path in (self.known_hosts,self.identity): path.chmod(0o600)
        self.transport=dict(host=self.host,address='192.0.2.10',port=22,user='ameesh-release',
            host_key_alias='fixture-host-key',known_hosts=str(self.known_hosts),
            known_hosts_sha256=hashlib.sha256(self.known_hosts.read_bytes()).hexdigest(),
            identity_file=str(self.identity),seconds=30)
        self.turn=dict(agent='one',unit='ameesh-runner@one.service',account='ameesh',
                       home='/home/ameesh',seconds=30)
        self.documents=dict(card=self.card,destinations=self.destinations,transport=self.transport,
                            turn=self.turn)
        for name,value in self.documents.items():
            path=self.enrollment_root/(name+'.json')
            path.write_text(json.dumps(value,sort_keys=True)); path.chmod(0o600)
        self.enrollment=dict(host=self.host,machine_id='a'*32,policy=self.policy,database='fixture-db')
        self.fence=Fence(); self.transport_fake=Transport()
        self.recovery=Companion(); self.health=Companion()
        self.releases=self._completed_releases()

    def document(self,name,value=None,*,mode=None):
        path=self.enrollment_root/(name+'.json')
        path.write_text(json.dumps(self.documents[name] if value is None else value,sort_keys=True))
        path.chmod(0o600 if mode is None else mode)
        return path

    def _release(self,commit,configuration=None):
        document=dict(commit=commit,python='/fixture/python',python_sha256='f'*64,wheels=[],
                      configuration=configuration or {
                          name:dict(content=('{"guard": true}\n' if name=='guard' else name+'\n'),
                                    mode=0o644)
                          for name in self.destinations})
        identity=hashlib.sha256(json.dumps(document,sort_keys=True).encode()).hexdigest()
        return document,commit+'-'+identity

    def _install(self,document,release_id):
        root=self.store/release_id; (root/'source/src/ameesh/migrations').mkdir(parents=True,mode=0o700)
        (root/'venv/bin').mkdir(parents=True,mode=0o700)
        (root/'venv/bin/python').write_text('fixture'); (root/'venv/bin/python').chmod(0o755)
        (root/'ready.json').write_text(json.dumps(document,sort_keys=True))
        # Same traversable modes as environnement-versionne's store: the pinned
        # factory runs this release's own runner as the enrolled executor.
        for directory in (root,root/'venv',root/'venv/bin'): directory.chmod(0o711)
        return root

    def _completed_releases(self):
        previous,previous_id=self._release('b'*40)
        target,target_id=self._release('a'*40)
        for document,release_id in ((previous,previous_id),(target,target_id)):
            self._install(document,release_id)
        current=self.store/'current'
        if not current.is_symlink(): current.symlink_to(previous_id)
        factory.environnement.pin_switch(self.store,'release-fixture',target_id)
        return dict(previous=previous_id,target=target_id,target_release=target)

    def manifest(self,release_id=None,units=None):
        release_id=release_id or self.releases['target']
        return dict(host=self.host,commit=release_id[:40],release=release_id,database='fixture-db',
                    units=units or list(self.policy['units']),
                    policy=factory.restart.policy_id(self.policy),rollback_until=2000000000)

    def request(self,step,manifest=None):
        manifest=manifest or self.manifest()
        return dict(operation='release-fixture',manifest_digest=factory.protocol.identity(manifest),
                    action_id='release-fixture:'+step,manifest=manifest,step=step)

    def build(self,*,capabilities=None,root=None):
        return factory.HostFactory(self.db,self.enrollment,documents=factory.Documents(self.enrollment_root),
            store=self.store,configuration_journal=self.journal,backup_root=self.backup,
            retention_root=self.retention,system=self.system,
            root=self.systemroot if root is None else root,
            capabilities=capabilities if capabilities is not None else
                {'sauvegarde-release':self.recovery,'sante-release':self.health})

    def adapter(self,factory_value=None):
        value=factory_value or self.build()
        value.admit(self.enrollment)
        adapter=value.for_operation(dict(operation='release-fixture'),self.authority)
        adapter.fence=self.fence; adapter.transport=self.transport_fake
        adapter.files=Files(); adapter.host_configuration=HostConfiguration()
        return adapter

    def test_admission_binds_every_step_and_returns_exactly_the_required_set(self):
        value=self.build()
        self.assertEqual(value.admit(self.enrollment),factory.REQUIRED)
        self.assertEqual(set(factory.REQUIRED),set(factory.protocol.STEPS+factory.protocol.RECOVERY))

    def test_admission_refuses_missing_unsafe_or_inexact_enrolled_documents(self):
        path=self.enrollment_root/'card.json'; path.unlink()
        with self.assertRaisesRegex(factory.FactoryError,'unavailable'): self.build()
        self.document('card'); self.document('card',mode=0o644)
        with self.assertRaisesRegex(factory.FactoryError,'private enrolled document'): self.build()
        self.document('card'); self.document('card',dict(self.card,shell='/bin/sh'))
        with self.assertRaises(factory.FactoryError): self.build()
        self.document('card')
        self.document('destinations',dict(self.destinations,escape='/etc/passwd'))
        with self.assertRaises(factory.configuration.ConfigurationError): self.build()

    def test_admission_refuses_inconsistent_policy_card_and_transport(self):
        self.document('card',dict(self.card,units=['other.service']))
        with self.assertRaisesRegex(factory.FactoryError,'outside enrolled host card'): self.build()
        self.document('card')
        changed=self.transport['known_hosts_sha256']
        self.document('transport',dict(self.transport,known_hosts_sha256='0'*64))
        value=self.build()
        with self.assertRaisesRegex(factory.FactoryError,'transport'): value.admit(self.enrollment)
        self.document('transport',dict(self.transport,known_hosts_sha256=changed))
        self.db.reachable=False
        value=self.build()
        with self.assertRaisesRegex(factory.FactoryError,'database'): value.admit(self.enrollment)

    def test_missing_pinned_companions_name_every_unbound_step(self):
        value=factory.HostFactory(self.db,self.enrollment,documents=factory.Documents(self.enrollment_root),
            store=self.store,configuration_journal=self.journal,backup_root=self.backup,
            retention_root=self.retention,system=self.system,
            capabilities={'sauvegarde-release':None,'sante-release':None})
        with self.assertRaisesRegex(factory.FactoryError,'accept,backup,health,pin,recovery_health'):
            value.admit(self.enrollment)

    def test_real_companions_admit_only_with_their_enrolled_backup_document(self):
        # The pinned modules now exist: an unenrolled recovery document must
        # refuse admission instead of binding a backend without credentials.
        value=factory.HostFactory(self.db,self.enrollment,documents=factory.Documents(self.enrollment_root),
            store=self.store,configuration_journal=self.journal,backup_root=self.backup,
            retention_root=self.retention,system=self.system,capabilities={})
        with self.assertRaisesRegex(factory.FactoryError,'sauvegarde-release'):
            value.admit(self.enrollment)
        self.document('backup',dict(dsn='postgresql://backup@localhost/fixture-db',
            restore_dsn='postgresql://restore@localhost/fixture-db',
            retention_seconds=1209600,remote=None,
            restore_capacity='/etc/ameesh-release/restore-capacity.json'))
        value=factory.HostFactory(self.db,self.enrollment,documents=factory.Documents(self.enrollment_root),
            store=self.store,configuration_journal=self.journal,backup_root=self.backup,
            retention_root=self.retention,system=self.system,capabilities={})
        if not HAVE_PSYCOPG:
            # Le pilote psql n'installe pas psycopg : le parseur libpq manque,
            # la cible ne peut pas être recoupée. L'admission doit REFUSER
            # nommément (fail-closed), jamais accepter une cible non vérifiée.
            with self.assertRaisesRegex(factory.FactoryError,
                                        'libpq parser unavailable'):
                value.admit(self.enrollment)
            return
        self.assertEqual(value.admit(self.enrollment),factory.REQUIRED)
        adapter=value.for_operation(dict(operation='release-fixture'),self.authority)
        self.assertTrue(callable(getattr(adapter.recovery_set,'perform',None)))
        self.assertTrue(callable(getattr(adapter.health,'perform',None)))

    def test_admission_refuses_a_backup_document_without_the_target_volume(self):
        # The restore cluster's volume must be attested; an enrollment that
        # omits it binds no recovery backend (fail closed before any copy).
        self.document('backup',dict(dsn='postgresql://backup@localhost/fixture-db',
            restore_dsn='postgresql://restore@localhost/fixture-db',
            retention_seconds=1209600,remote=None))
        value=factory.HostFactory(self.db,self.enrollment,documents=factory.Documents(self.enrollment_root),
            store=self.store,configuration_journal=self.journal,backup_root=self.backup,
            retention_root=self.retention,system=self.system,capabilities={})
        with self.assertRaisesRegex(factory.FactoryError,'sauvegarde-release'):
            value.admit(self.enrollment)

    def test_every_controller_step_returns_exactly_request_plus_ok(self):
        adapter=self.adapter()
        steps=list(factory.protocol.STEPS+factory.protocol.RECOVERY)
        with mock.patch('ameesh.migrations.migrate',return_value=[]):
            for step in steps:
                request=self.request(step)
                receipt=adapter.perform(step,request)
                self.assertEqual(receipt,dict(request,ok=True),step)
        self.assertEqual(self.fence.steps,['fence','unfence','recovery_fence','recovery_unfence'])
        # B2 witness: the host's enrolled probe agent reaches every fence step,
        # and never leaks into the signed request or its receipt.
        self.assertEqual(self.fence.probes,[self.turn['agent']]*4)
        self.assertEqual(self.transport_fake.steps,['restart','recovery_restart'])
        # activate switched to the signed target, rollback restored the pinned prior pair
        self.assertEqual(os.readlink(self.store/'current'),self.releases['previous'])

    def test_real_pin_binds_configuration_and_switch_before_configure(self):
        # B1 witness: with the real files/host_configuration adapter, the pin
        # step must create the configuration journal and the release-switch
        # pair. A pin that only reaches the recovery set leaves configure
        # definitively refused and activate/rollback without their file.
        value=self.build()
        value.admit(self.enrollment)
        adapter=value.for_operation(dict(operation='release-fixture'),self.authority)
        adapter.fence=self.fence; adapter.files.gate=self.fence
        adapter.transport=self.transport_fake
        for logical in ('/etc/systemd/system/ameesh-runner@.service',
                        '/etc/systemd/system/ameesh-runner@.service.d/10-plafonds.conf'):
            path=self.systemroot/logical.lstrip('/')
            path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            path.write_text('[Unit]\n'); path.chmod(0o644)
        self.system.overrides['ameesh-runner@one.service']=dict(
            FragmentPath='/etc/systemd/system/ameesh-runner@.service',
            DropInPaths='/etc/systemd/system/ameesh-runner@.service.d/10-plafonds.conf')
        # setUp pinned the switch by hand; the real pin must recreate it.
        (self.store/'.operations'/'release-fixture.json').unlink()
        self.assertFalse((self.journal/'release-fixture.configuration.json').exists())
        self.assertEqual(adapter.perform('pin',self.request('pin')),
                         dict(self.request('pin'),ok=True))
        self.assertTrue((self.journal/'release-fixture.configuration.json').is_file())
        self.assertTrue((self.store/'.operations'/'release-fixture.json').is_file())
        self.assertEqual(self.recovery.steps,['pin'])
        self.assertEqual(adapter.perform('configure',self.request('configure')),
                         dict(self.request('configure'),ok=True))
        self.assertEqual((self.systemroot/'etc/systemd/system/fixture.service').read_text(),'unit\n')
        self.assertEqual(adapter.perform('activate',self.request('activate')),
                         dict(self.request('activate'),ok=True))
        self.assertEqual(os.readlink(self.store/'current'),self.releases['target'])
        self.assertEqual(adapter.perform('rollback',self.request('rollback')),
                         dict(self.request('rollback'),ok=True))
        self.assertEqual(os.readlink(self.store/'current'),self.releases['previous'])

    def test_unknown_release_and_partial_capability_are_definitive_not_effects(self):
        adapter=self.adapter()
        foreign='c'*40+'-'+'d'*64
        self.assertEqual(adapter.perform('prepare',self.request('prepare',self.manifest(foreign))),
                         dict(self.request('prepare',self.manifest(foreign)),ok=False))
        adapter.files=Files(factory.verification.VerificationError('unit syntax verification failed'))
        receipt=adapter.perform('configure',self.request('configure'))
        self.assertEqual(receipt,dict(self.request('configure'),ok=False))

    def test_ambiguous_or_unauthorized_actions_never_fabricate_a_receipt(self):
        adapter=self.adapter()
        adapter.files=Files(OSError('private-canary'))
        with self.assertRaises(factory.FactoryError) as failure:
            adapter.perform('configure',self.request('configure'))
        self.assertNotIn('canary',str(failure.exception))
        self.authority.allowed=False
        with self.assertRaisesRegex(factory.FactoryError,'authority denied'):
            adapter.perform('configure',self.request('configure'))
        self.assertEqual(self.fence.steps,[])

    def test_delegated_backup_and_health_receive_the_signed_release_document(self):
        adapter=self.adapter()
        for step in ('backup','pin','accept'):
            adapter.perform(step,self.request(step))
        for step in ('health','recovery_health'):
            adapter.perform(step,self.request(step))
        self.assertEqual(self.recovery.steps,['backup','pin','accept'])
        self.assertEqual(self.health.steps,['health','recovery_health'])

    def test_migrate_refuses_absent_sources_and_non_additive_passes(self):
        adapter=self.adapter()
        with mock.patch('ameesh.migrations.migrate',return_value=[]) as migrate:
            self.assertEqual(adapter.perform('migrate',self.request('migrate')),
                             dict(self.request('migrate'),ok=True))
            self.assertTrue(migrate.call_args.kwargs['additive_only'])
        from ameesh import migrations as ameesh_migrations
        with mock.patch('ameesh.migrations.migrate',
                side_effect=ameesh_migrations.NonAdditiveMigration('x')):
            self.assertEqual(adapter.perform('migrate',self.request('migrate')),
                             dict(self.request('migrate'),ok=False))

    def test_real_factory_admits_the_independent_service_and_publishes_readiness(self):
        value=self.build()
        operations=self.temp/'operations'; operations.mkdir(mode=0o700)
        manifest=self.manifest()
        service.protocol.save(operations/'release-fixture.json',
            dict(operation='release-fixture',decision_id=123,manifest=manifest))
        engine=service.IndependentService(self.temp/'controller',operations,self.enrollment,
            dict(runtime_tree_sha256='c'*64,source_tree_sha256='d'*64),
            documents=service.Documents(),system=ServiceSystem(),factory=value,
            authority_factory=lambda job:self.authority)
        receipt=engine.admit()
        self.assertTrue(receipt['adapter_ready'])
        self.assertEqual(engine.documents.load(self.temp/'controller/readiness.json'),receipt)


class ServiceSystem:
    def identity(self): return dict(machine_id='a'*32,boot_id='fixture-boot',invocation_id='b'*32)
    def verify_trees(self,pins): return True


if __name__=='__main__':
    unittest.main()
