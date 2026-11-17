# SPDX-License-Identifier: AGPL-3.0-only
"""Installed action modules: real entry-point discovery, canon policy and gate."""
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import types
import unittest
from unittest import mock

from ameesh import actions, connectors, module_actions, modules
from ameesh.modules import registry
from ameesh.modules.storage import ModuleStore
from tests.test_actions import ActionsBase, ALICE, POLICY
from tests.test_l98_modules_activation import canon_with, federation, external_points, FakeEntryPoint, sample
from tests.webauthn_soft import ORIGIN, RP_ID

FIXTURE = Path(__file__).parent / 'fixtures' / 'module_connector_bench'


class ConnectorDeclarationTest(unittest.TestCase):
    def test_core_and_other_module_names_cannot_be_taken(self):
        for name in ('shell-noop', 'git-merge'):
            module = sample(name='extension')
            module.ACTION_CONNECTORS = {name: lambda: None}
            with external_points(FakeEntryPoint(module, name='extension')):
                refused = registry.discover().refused
                self.assertTrue(any('duplicate action connector' in r['error'] for r in refused))
                with self.assertRaisesRegex(ValueError, 'duplicate action connector'):
                    modules.installed()
        first, second = sample(name='first'), sample(name='second')
        first.ACTION_CONNECTORS = second.ACTION_CONNECTORS = {'example': lambda: None}
        with external_points(FakeEntryPoint(first, 'first'), FakeEntryPoint(second, 'second')):
            self.assertTrue(any('duplicate action connector' in r['error']
                                for r in registry.discover().refused))

    def test_bare_profile_cannot_steal_the_legacy_merge_name(self):
        module = sample(name='dev')
        module.ACTION_CONNECTORS = {'git-merge': lambda: None}
        with mock.patch.dict(os.environ, AMEESH_MODULES='bare'), \
                external_points(FakeEntryPoint(module, name='dev')):
            self.assertTrue(any('duplicate action connector' in r['error']
                                for r in registry.discover().refused))

    def test_missing_dedupe_or_reconcile_is_not_a_connector(self):
        for attrs in (dict(name='example', execute=lambda *a: None, classify=lambda *a: 'reversible'),
                      dict(name='example', dedupe='guaranteed', execute=lambda *a: None,
                           classify=lambda *a: 'reversible')):
            with self.assertRaises(connectors.ConnectorError):
                connectors.check(types.SimpleNamespace(**attrs))

    def test_canon_policy_is_validated_and_not_supplied_by_descriptor(self):
        module = sample()
        module.ACTION_CONNECTORS = {'example': lambda: None}
        descriptor = module.descriptor()
        for classes in ({'unknown': {'send': 'reversible'}},
                        {'example': {'send': 'made-up'}}, [], {'example': []}):
            with self.subTest(classes=classes), self.assertRaises(ValueError):
                module_actions.compile_policy('sample', module, descriptor, classes)
        forged = dict(descriptor, rules=[dict(kind=module_actions.RULE)])
        with self.assertRaisesRegex(ValueError, 'must come from the canon'):
            module_actions.compile_policy('sample', module, forged, {})

    def test_namespaced_operation_uses_the_gate_identifier_contract(self):
        module = sample()
        module.ACTION_CONNECTORS = {'example': lambda: None}
        classes = {'example': {'message:publish/path': 'irreversible'}}
        compiled = module_actions.compile_policy('sample', module, module.descriptor(), classes)
        self.assertEqual(compiled['rules'][-1]['connectors'], classes)


class InstalledActionTest(ActionsBase):
    def setUp(self):
        super().setUp()
        self.bench = os.path.join(self.tmp, 'installed-module')
        shutil.copytree(FIXTURE, self.bench)
        self.addCleanup(sys.modules.pop, 'bench_actions', None)
        self.enterContext(mock.patch.dict(os.environ, AMEESH_MODULES='bare'))
        self.enterContext(mock.patch.object(sys, 'path', [self.bench, *sys.path]))
        importlib.invalidate_caches()
        self.directory = os.path.join(self.tmp, 'journal')
        self.activate()

    def activate(self, action_class='irreversible', *, declared=True):
        options = {'action_classes': {'bench-journal': {'publish': action_class}}} if declared else {}
        raw = federation(projects={'demo': {'modules': {'bench-actions': options}}})
        c = canon_with(federation_=raw)
        ModuleStore(self.db).replace('', modules.declarations(raw, c))

    def connector(self):
        return connectors.get('bench-journal')

    def propose_module(self, **kwargs):
        return self.propose(operation='publish', connector=self.connector(),
                            args={'directory': self.directory}, **kwargs)

    def test_real_installed_entry_point_and_canon_override_classify(self):
        described = registry.discover().get('bench-actions')
        self.assertEqual(described.source, 'external')
        self.assertEqual(described.action_connectors, ('bench-journal',))
        self.assertEqual(described.as_dict()['action_connectors'], ['bench-journal'])
        self.assertEqual(described.storage, ())
        self.assertIsNone(registry.discover().get('dev'))
        action = self.propose_module(canon_class='reversible')
        self.assertEqual(action['class'], 'irreversible')
        self.assertEqual(self.approve(action)['state'], 'approved')
        self.assertEqual(self.execute(action, self.connector())['state'], 'unknown')
        confirmed = actions.reconcile(self.db, action['action_id'], self.connector())
        self.assertEqual(confirmed['state'], 'confirmed')
        self.assertEqual(list(Path(self.directory).iterdir()), [Path(self.directory) / action['action_id']])

    def test_missing_operation_class_refuses_even_with_explicit_class_arguments(self):
        self.activate(declared=False)
        self.refused(actions.CLASS, self.propose_module, action_class='reversible', canon_class='reversible')
        self.assertEqual(actions.list_actions(self.db), [])

    def test_module_is_not_active_in_another_project(self):
        self.refused(actions.CONNECTOR, self.propose_module, project='other')

    def test_deactivation_and_class_change_prevent_an_approved_launch(self):
        action = self.propose_module()
        self.approve(action)
        self.activate('costly')
        self.refused(actions.CLASS, self.execute, action, self.connector())
        ModuleStore(self.db).replace('', [])
        self.refused(actions.CONNECTOR, self.execute, action, self.connector())
        self.assertFalse(Path(self.directory).exists())

    def test_withdrawn_policy_prevents_approval_and_reconciliation(self):
        action = self.propose_module()
        ModuleStore(self.db).replace('', [])
        self.refused(actions.CONNECTOR, self.approve, action)
        self.activate()
        self.approve(action)
        self.assertEqual(self.execute(action, self.connector())['state'], 'unknown')
        connector = self.connector()
        ModuleStore(self.db).replace('', [])
        self.refused(actions.CONNECTOR, actions.reconcile, self.db, action['action_id'], connector)

    def test_stale_connector_does_not_execute_after_its_module_is_removed(self):
        action = self.propose_module()
        self.approve(action)
        connector = self.connector()
        with mock.patch.object(registry, 'action_connectors', return_value={}):
            self.refused(actions.CONNECTOR, self.execute, action, connector)
        self.assertFalse(Path(self.directory).exists())

    def test_builtin_development_connector_can_opt_into_an_explicit_canon_policy(self):
        with mock.patch.dict(os.environ, AMEESH_MODULES='default'):
            raw = federation(projects={'demo': {'modules': {'dev': {
                'action_classes': {'git-merge': {'merge': 'reversible'}}}}}})
            ModuleStore(self.db).replace('', modules.declarations(raw, canon_with(federation_=raw)))
            connector = connectors.get('git-merge')
            action = self.propose(operation='merge', connector=connector)
            self.assertEqual(action['class'], 'reversible')
            self.refused(actions.CLASS, self.propose, operation='missing', connector=connector)

    def test_cli_propose_approve_execute_and_reconcile_in_bare_profile(self):
        env = self.env(AMEESH_MODULES='bare', PYTHONPATH=os.pathsep.join((str(Path(__file__).resolve().parents[1] / 'src'), self.bench)),
                       AMEESH_APPROVE_RP_ID=RP_ID, AMEESH_APPROVE_ORIGINS=ORIGIN)
        proc = self.mesh('action', 'propose', '--connector', 'bench-journal', '--operation', 'publish',
                         '--project', 'demo', '--target', 'journal', '--by', 'agent:sample',
                         '--args', json.dumps({'directory': self.directory}), '--json', env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        action = json.loads(proc.stdout)
        receipt_path = Path(self.tmp) / 'receipt.json'
        receipt_path.write_text(json.dumps(self.receipt(action)), encoding='utf-8')
        proc = self.mesh('action', 'approve', action['action_id'], '--receipt', str(receipt_path),
                         '--by', ALICE, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh('action', 'execute', action['action_id'], env=env)
        self.assertEqual(proc.returncode, 4, proc.stderr)
        proc = self.mesh('action', 'reconcile', action['action_id'], env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.get(action)['state'], 'confirmed')
