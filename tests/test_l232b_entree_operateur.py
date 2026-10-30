# SPDX-License-Identifier: AGPL-3.0-only
"""Operator entry: real disposable trees and files, simulated registry and host.

No root, SSH, systemd, PostgreSQL migration or production effect is executed:
the probe registry is simulated, the host effects are the fixture's, and no
fixed system command is run.
"""
from tests import support
from tests import test_l232b_bootstrap_files as file_fixture
from tests import test_l232b_account as account_fixture
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import stat
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]


def load(name,filename):
    spec=importlib.util.spec_from_file_location(name,ROOT/'deploy/mise-a-jour'/filename)
    loaded=importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


entry=load('operator_entry','entree-operateur.py')
installer=load('operator_installer','installateur-bootstrap.py')
factory=load('operator_factory','adaptateur-hote-release.py')
pilot=load('operator_pilot','pilote-sante.py')


class Registry:
    """Simulated agent registry: exact rows or an unreachable database."""

    def __init__(self,rows=None,unreachable=False):
        self.rows=[dict(name='fixture',host='fixture-host')] if rows is None else rows
        self.unreachable=unreachable
        self.queries=[]

    def query(self,sql,params=()):
        self.queries.append((sql,tuple(params)))
        if self.unreachable: raise OSError('fixture registry unavailable')
        return [dict(row) for row in self.rows]


class TurnBackend:
    def one_turn(self,**kwargs): return 0


class TurnDatabase:
    def query(self,sql,params=()): return []


class Host(account_fixture.Accounts):
    """Account/service effects stay simulated; real files are the fixture's."""

    def __init__(self,root):
        super().__init__(); self.root=root; self.is_ready=True; self.reloads=0

    def preflight(self,bundle): return self.machine()==bundle['machine_id']

    def layout(self,bundle):
        for logical,mode in installer.DIRECTORIES.items():
            target=self.root/logical.lstrip('/')
            target.mkdir(parents=True,mode=mode,exist_ok=True)
            # Exact reviewed mode: neither the umask nor a setgid parent
            # (offload guest job directory) may leak into the layout.
            target.chmod(mode)
        return True

    def validate_configuration(self,bundle): return True
    def validate_restored_configuration(self,bundle): return True
    def ready(self,bundle): return self.is_ready
    def activate_ssh_configuration(self,bundle): self.reloads+=1; return True


class OperatorEntryTest(unittest.TestCase):
    def setUp(self):
        base=file_fixture.BootstrapFilesTest(); base.setUp(); self.addCleanup(base.doCleanups)
        def writable():
            for directory,dirs,names in os.walk(base.root): Path(directory).chmod(0o700)
        self.addCleanup(writable)
        self.base=base; self.root=base.root; self.temp=Path(base.temp.name)
        self.operation='bootstrap-fixture'
        stage=self.root/('var/lib/ameesh-release-bootstrap/'+self.operation)
        (stage/'runtime/bin').mkdir(parents=True,mode=0o700)
        program=stage/'runtime/bin/python'
        program.write_bytes(b'synthetic executable, never launched'); program.chmod(0o700)
        (stage/'sources').mkdir(mode=0o700)
        (stage/'sources/hote-release.py').write_text('# synthetic pinned source\n')
        # A setgid parent (offload guest job directory) is inherited by mkdir;
        # the staged trees must not carry it (runtime.inventory refuses 07000).
        for directory in (stage.parent,stage,stage/'runtime',stage/'runtime/bin',stage/'sources'):
            directory.chmod(0o700)
        self.trees={'runtime':stage/'runtime','sources':stage/'sources'}
        self.plan=dict(base.bundle['plan'],
            runtime_tree_sha256=installer.runtime.inventory(self.trees['runtime'])['digest'],
            source_tree_sha256=installer.runtime.inventory(self.trees['sources'])['digest'])
        self.host='fixture-host'; self.agent='fixture'; self.accounts=dict(uid=987,gid=987)
        self.registry=Registry()
        self.turn=entry.probe(db=self.registry,host=self.host,agent=self.agent,account='ameesh',
                              home='/home/ameesh',seconds=30,policy=self.plan['policy'])
        self.observed={path:None for path in entry.destinations(
            plan=self.plan,account_ids=self.accounts,probe_turn=self.turn)}

    def prepare(self,**changes):
        arguments=dict(operation=self.operation,plan=self.plan,account_ids=self.accounts,
                       trees=self.trees,observed=self.observed,probe_turn=self.turn,
                       expires_at=2000000000,db=self.registry)
        arguments.update(changes)
        return entry.prepare(**arguments)

    def probe(self,**changes):
        arguments=dict(db=self.registry,host=self.host,agent=self.agent,account='ameesh',
                       home='/home/ameesh',seconds=30,policy=self.plan['policy'])
        arguments.update(changes)
        return entry.probe(**arguments)

    def test_prepare_seal_reuses_signed_bundle_and_exact_destination_inventory(self):
        from tests.test_3526_runtime_seal import empty_tree
        document=dict(version=1,sha='a'*40,machine_id=self.plan['machine_id'],trees={
            'runtime':installer.runtime.inventory(self.trees['runtime']),
            'lib':installer.runtime.inventory(self.trees['sources']), 'store':empty_tree()})
        observed={path:None for path in entry.destinations(plan=self.plan,account_ids=self.accounts,
                    probe_turn=self.turn,runtime_seal=document)}
        prepared=self.prepare(runtime_seal=document,observed=observed)
        self.assertEqual(prepared.bundle['files'][entry.delegation.SEAL_DOCUMENT]['content'],entry.canonical(document))
        self.assertIn(entry.delegation.SEAL_ENTRY,prepared.bundle['files'])
        with self.assertRaises(entry.OperatorError):
            self.prepare(runtime_seal=document)  # prior inventory lacks the new protected files

    def test_bootstrap_publishes_and_verifies_the_complete_sealed_runtime(self):
        from types import SimpleNamespace
        from unittest import mock
        from tests.test_3526_runtime_seal import seal, empty_tree
        config=self.trees['runtime']/'pyvenv.cfg'
        config.write_text(f'home = {Path(sys._base_executable).resolve().parent}\ninclude-system-site-packages = false\n'); config.chmod(0o600)
        trees={name:installer.runtime.inventory(path) for name,path in self.trees.items()}
        self.plan=dict(self.plan,runtime_tree_sha256=trees['runtime']['digest'],source_tree_sha256=trees['sources']['digest'])
        document=dict(version=1,sha='a'*40,machine_id=self.plan['machine_id'],
                      trees={'runtime':trees['runtime'],'lib':trees['sources'],'store':empty_tree()})
        observed={path:None for path in entry.destinations(plan=self.plan,account_ids=self.accounts,
                    probe_turn=self.turn,runtime_seal=document)}
        prepared=self.prepare(runtime_seal=document,observed=observed)
        host=Host(self.root)
        adapter=installer.BootstrapHostAdapter(self.temp/'install-journal',authority=self.base.authority,system=host,root=self.root)
        original=installer.module
        calls=[]
        def verify(doc,base):
            calls.append(base)
            return seal.verify(doc,base,uid=os.getuid())
        with mock.patch.object(seal,'protected',side_effect=lambda path,**kw:Path(path)), \
             mock.patch.object(installer,'module',side_effect=lambda name:SimpleNamespace(verify=verify) if name=='scellement-runtime' else original(name)):
            # Lock serialization and real signature are tested independently;
            # here every publication/snapshot/measurement is a real fixture file.
            self.assertEqual(entry.bootstrap._run(self.temp/'order-journal',self.operation,prepared.bundle,
                             authority=self.base.authority,adapter=adapter),'installed')
        self.assertEqual(calls,[self.root/'opt/ameesh-release'])
        installed=self.root/entry.delegation.SEAL_DOCUMENT.lstrip('/')
        self.assertEqual(json.loads(installed.read_text()),document)

    def test_probe_is_bound_to_the_registry_row_and_the_enrolled_policy(self):
        self.assertEqual(self.turn,dict(agent='fixture',unit='ameesh-runner@fixture.service',
                                        account='ameesh',home='/home/ameesh',seconds=30))
        self.assertEqual(self.registry.queries,[(entry.REGISTRY,('fixture',))])
        for rows in ([],[dict(name='fixture',host='other-host')],
                     [dict(name='fixture',host=self.host),dict(name='fixture',host=self.host)],
                     [dict(name='other',host=self.host)]):
            with self.assertRaises(entry.OperatorError,msg=rows):
                self.probe(db=Registry(rows))
        with self.assertRaises(entry.OperatorError):
            self.probe(db=Registry(unreachable=True))
        with self.assertRaises(entry.OperatorError):
            self.probe(db=None)
        with self.assertRaises(entry.OperatorError):
            self.probe(policy=dict(self.plan['policy'],units=['ameesh-runner@other.service']))
        with self.assertRaises(entry.OperatorError):
            self.probe(policy=dict(self.plan['policy'],host='other-host'))
        with self.assertRaises(entry.OperatorError):
            self.probe(policy=dict(self.plan['policy'],restart_seconds=4))
        for broken in (dict(agent='bad agent'),dict(account='Root'),dict(home='relative'),
                       dict(home='/home/../etc'),dict(seconds=4),dict(seconds=True)):
            with self.assertRaises(entry.OperatorError,msg=broken):
                self.probe(**broken)

    def test_probe_takes_the_unit_enrolled_for_this_agent_in_either_template(self):
        # The unit comes from the enrolled policy, not from a hard-coded name:
        # the deployed user template and the system template both qualify.
        for unit in ('ameesh-runner@fixture.service', 'ameesh-runner-agent@fixture.service'):
            with self.subTest(unit=unit):
                policy=dict(self.plan['policy'],units=[unit])
                document=self.probe(policy=policy)
                self.assertEqual(document['unit'],unit)
                self.assertEqual(document['agent'],'fixture')
        # Other enrolled runners do not make this agent's unit ambiguous.
        policy=dict(self.plan['policy'],
                    units=['ameesh-runner@other.service','ameesh-runner-agent@fixture.service'])
        self.assertEqual(self.probe(policy=policy)['unit'],
                         'ameesh-runner-agent@fixture.service')
        # No unit for this agent, or both templates at once: explicit refusal,
        # never a choice or an alias.
        for units in (['ameesh-runner@other.service'],
                      ['ameesh-runner-agent@other.service'],
                      ['ameesh-runner@fixture.service','ameesh-runner-agent@fixture.service']):
            with self.subTest(units=units):
                with self.assertRaises(entry.OperatorError):
                    self.probe(policy=dict(self.plan['policy'],units=units))

    def test_prepare_accepts_the_enrolled_user_template_unit(self):
        self.plan=dict(self.plan,policy=dict(self.plan['policy'],
                     units=['ameesh-runner-agent@fixture.service']))
        self.turn=self.probe()
        self.assertEqual(self.turn['unit'],'ameesh-runner-agent@fixture.service')
        self.observed={path:None for path in entry.destinations(
            plan=self.plan,account_ids=self.accounts,probe_turn=self.turn)}
        prepared=self.prepare()
        self.assertEqual(prepared.turn,self.turn)
        self.assertEqual(json.loads(prepared.bundle['files'][entry.TURN]['content']),
                         self.turn)

    def test_prepare_measures_the_staged_trees_and_refuses_a_repin_or_a_partial_inventory(self):
        prepared=self.prepare()
        for name,field in entry.TREES.items():
            self.assertEqual(prepared.bundle['plan'][field],
                             installer.runtime.inventory(self.trees[name])['digest'])
        with self.assertRaises(entry.OperatorError):
            self.prepare(plan=dict(self.plan,runtime_tree_sha256='f'*64))
        with self.assertRaises(entry.OperatorError):
            self.prepare(plan=dict(self.plan,source_tree_sha256='f'*64))
        with self.assertRaises(entry.OperatorError):
            self.prepare(trees={'runtime':self.trees['runtime'],'sources':self.temp/'absent'})
        with self.assertRaises(entry.OperatorError):
            self.prepare(trees={'runtime':self.trees['runtime']})
        with self.assertRaises(entry.OperatorError):
            self.prepare(trees={'runtime':'relative-runtime','sources':self.trees['sources']})
        partial=dict(self.observed); partial.pop(sorted(partial)[0])
        with self.assertRaises(entry.OperatorError):
            self.prepare(observed=partial)
        extra=dict(self.observed); extra['/etc/ameesh-release/extra.json']=None
        with self.assertRaises(entry.OperatorError):
            self.prepare(observed=extra)
        for item in (dict(sha256='a'*64,mode=0o666),dict(sha256='A'*64,mode=0o600),
                     dict(sha256='a'*64),dict(sha256='a'*64,mode=0o600,extra=1),'a'*64):
            broken=dict(self.observed); broken[entry.TURN]=item
            with self.assertRaises(entry.OperatorError,msg=item):
                self.prepare(observed=broken)
        with self.assertRaises(entry.OperatorError):
            self.prepare(expires_at=0)
        with self.assertRaises(entry.OperatorError):
            self.prepare(operation='')
        with self.assertRaises(entry.OperatorError):
            self.prepare(plan=dict(self.plan,controller_source='192.0.2.17'))

    def test_prepare_binds_the_probe_turn_and_the_prior_files_in_the_signed_question(self):
        prepared=self.prepare()
        info=prepared.bundle['files'][entry.TURN]
        self.assertEqual(info['content'],entry.canonical(self.turn))
        self.assertEqual(info['mode'],0o600)
        self.assertEqual(info['owner'],'root')
        self.assertEqual(info['sha256'],hashlib.sha256(entry.canonical(self.turn).encode()).hexdigest())
        self.assertIsNone(prepared.bundle['previous_files'][entry.TURN])
        entry.delegation.question(prepared.bundle)
        self.assertLessEqual(len(prepared.question.encode()),entry.MAX_QUESTION)
        self.assertIn(entry.TURN,prepared.question)
        self.assertIn(self.operation,prepared.question)
        prior=dict(self.observed); prior[entry.TURN]=dict(sha256='b'*64,mode=0o600)
        replaced=self.prepare(observed=prior)
        self.assertEqual(replaced.bundle['previous_files'][entry.TURN],dict(sha256='b'*64,mode=0o600))
        self.assertNotEqual(prepared.bundle['digest'],replaced.bundle['digest'])
        other=self.prepare(probe_turn=dict(self.turn,seconds=60))
        self.assertNotEqual(prepared.bundle['digest'],other.bundle['digest'])
        self.assertNotEqual(prepared.question,other.question)
        changed=json.loads(json.dumps(prepared.bundle))
        changed['files'][entry.TURN]['content']='{"agent":"other"}'
        with self.assertRaises(entry.delegation.DelegationError):
            entry.delegation.question(changed)
        without={path:None for path in entry.destinations(plan=self.plan,account_ids=self.accounts)}
        offline=self.prepare(probe_turn=None,observed=without)
        self.assertNotIn(entry.TURN,offline.bundle['files'])
        with self.assertRaises(entry.OperatorError):
            self.prepare(probe_turn=None)
        with self.assertRaises(entry.delegation.DelegationError):
            entry.delegation.render(self.plan,account_ids=self.accounts,
                                    enrollment={entry.TURN:json.dumps(self.turn)})
        with self.assertRaises(entry.delegation.DelegationError):
            entry.delegation.render(self.plan,account_ids=self.accounts,
                                    enrollment={'/etc/ameesh-release/other.json':'{}'})
        with self.assertRaises(entry.OperatorError):
            self.prepare(probe_turn=dict(self.turn,unit='ameesh-runner@other.service'))

    def test_prepare_re_attests_the_probe_document_on_the_plan_host(self):
        """B1 de la relecture cx3 : un dict fourni ne suffit pas, prepare()
        doit interroger le registre enrole sur l'hote du plan."""
        self.registry.queries.clear()
        attested=self.prepare()
        self.assertEqual(self.registry.queries,[(entry.REGISTRY,('fixture',))])
        self.assertEqual(attested.bundle['files'][entry.TURN]['content'],
                         entry.canonical(self.turn))
        for broken in (None,Registry([]),Registry(unreachable=True),
                       Registry([dict(name='fixture',host='other-host')]),
                       Registry([dict(name='other',host=self.host)])):
            with self.assertRaises(entry.OperatorError,msg=broken):
                self.prepare(db=broken)
        # un document complet et bien forme d'un agent absent de cet hote refuse
        absent=dict(self.turn,agent='other',unit='ameesh-runner@other.service')
        with self.assertRaises(entry.OperatorError):
            self.prepare(probe_turn=absent,db=Registry([dict(name='other',host='other-host')]))
        # sans sonde enrolee, aucun registre n'est exige ni consulte
        without={path:None for path in entry.destinations(plan=self.plan,
                                                          account_ids=self.accounts)}
        self.registry.queries.clear()
        offline=self.prepare(probe_turn=None,observed=without,db=None)
        self.assertNotIn(entry.TURN,offline.bundle['files'])
        self.assertEqual(self.registry.queries,[])
        # la forme reste refusee avant tout registre
        with self.assertRaises(entry.OperatorError):
            self.prepare(probe_turn=dict(self.turn,unit='ameesh-runner@other.service'))

    def test_install_executes_through_the_enrolled_backend_and_lands_the_turn_enrollment(self):
        prepared=self.prepare()
        system=Host(self.root); authority=self.base.authority
        adapter=installer.BootstrapHostAdapter(self.temp/'effects',authority=authority,
                                               system=system,root=self.root)
        journal=self.temp/'operator-journal'
        self.assertEqual(entry.install(prepared,authority=authority,adapter=adapter,
                                       journal=journal),'installed')
        target=self.root/entry.TURN.lstrip('/')
        self.assertEqual(target.read_text(),entry.canonical(self.turn))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o600)
        self.assertEqual(target.stat().st_uid,os.geteuid())
        document=factory.Documents(self.root/'etc/ameesh-release').json(
            'turn',exact=('agent','unit','account','home','seconds'))
        self.assertEqual(document,self.turn)
        turn=pilot.HealthTurn(TurnDatabase(),enrollment=document,host=self.host,system=TurnBackend())
        self.assertEqual((turn.agent,turn.unit,turn.account,turn.home,turn.seconds),
                         ('fixture','ameesh-runner@fixture.service','ameesh','/home/ameesh',30))
        self.assertEqual(entry.install(prepared,authority=authority,adapter=adapter,
                                       journal=journal),'installed')
        self.assertEqual(system.calls,['group','user'])
        self.assertEqual(system.reloads,1)
        request=dict(operation=self.operation,step='revoke',
                     action_id=self.operation+':revoke',bundle=prepared.bundle,
                     scope_digest=installer.bootstrap.scopes(self.operation,prepared.bundle)['revoke'])
        self.assertTrue(adapter.perform('revoke',request)['ok'])
        self.assertFalse(target.exists())
        self.assertFalse((self.root/'etc/ameesh-release/authorized_keys').exists())
        self.assertTrue((self.root/'opt/ameesh-release/runtime/bin/python').exists())
        self.assertIsNotNone(system.account_value)
        for broken in ({},None,object()):
            with self.assertRaises(entry.OperatorError):
                entry.install(broken,authority=authority,adapter=adapter,journal=journal)
        with self.assertRaises(entry.OperatorError):
            entry.install(prepared,authority=object(),adapter=adapter,journal=journal)
        with self.assertRaises(entry.OperatorError):
            entry.install(prepared,authority=authority,adapter=object(),journal=journal)

    def test_the_entry_exposes_no_cli_and_no_caller_selected_root(self):
        self.assertFalse(hasattr(entry,'main'))
        self.assertNotIn('argparse',vars(entry))
        self.assertEqual(set(inspect.signature(entry.install).parameters),
                         {'prepared','authority','adapter','journal'})
        self.assertEqual(set(inspect.signature(entry.prepare).parameters),
                         {'operation','plan','account_ids','trees','observed','probe_turn',
                          'expires_at','db','runtime_seal'})
        source=(ROOT/'deploy/mise-a-jour/entree-operateur.py').read_text()
        self.assertNotIn('__main__',source)
        self.assertNotIn('argparse',source)


if __name__=='__main__':
    unittest.main()
