# SPDX-License-Identifier: AGPL-3.0-only
"""#6181: one serialized union at CI, prevalidation and packet entry points."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ameesh import contract_plan as cp, mechanics, prevalidation as pv
from ameesh import prevalidation_calls as calls

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


planner = load('contract_plan_planner', 'test-plan.py')
packet = load('contract_plan_packet', 'paquet-auto.py')


class SharedPlanTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'tests@example.invalid')
        self.git('config', 'user.name', 'Tests')
        self.inventory = ['test_changed', 'test_cluster', 'test_declared',
                          'test_l161f_plan', 'test_map', 'test_universal', 'test_unused']
        for module in self.inventory:
            self.write('tests/' + module + '.py', '')
        self.write('src/ameesh/contract_plan.py', '')
        self.write('tests/parts/toujours.txt', 'test_cluster\n')
        self.registry = {'schema': 'ameesh-contracts/1', 'rules': [
            {'id': 'global', 'paths': [], 'operations': list('ACDMRTU'),
             'modules': ['test_universal']},
            {'id': 'changed', 'paths': ['watched.py'], 'operations': list('ACDMRTU'),
             'modules': ['test_declared']}]}
        self.write('tests/parts/registry.json', json.dumps(self.registry))
        self.write('watched.py', '# before\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.write('watched.py', '# candidate\n')
        self.git('commit', '-qam', 'candidate')
        self.head = self.git('rev-parse', 'HEAD')
        self.policy = mechanics.ContractSelection(registry='tests/parts/registry.json')
        self.params = SimpleNamespace(classes=('P0', 'P1'),
                                      contracts=('tests/parts/toujours.txt',))

    def git(self, *args):
        return cp.git(self.repo, *args).strip()

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def plan(self, *, used=True, stale=False, policy=True):
        classes = {m: ['P3', 'unchanged'] for m in self.inventory}
        classes['test_changed'] = ['P0', 'changed test']
        classes['test_map'] = ['P1', 'map reader']
        state = {'classes': classes, 'contract': ['test_cluster'], 'always': [],
                 'map': {'used': used, 'stale': stale, 'commit': self.base},
                 'sure': used, 'phase2_safe': used and not stale,
                 'lot_safe': used, 'unsure': [], 'phase2_why': '', 'changed': 1}
        with patch.object(planner.parts, 'modules', return_value=self.inventory), \
                patch.object(planner, 'classify', return_value=state), \
                patch.object(planner.transverse_contracts, 'problem', return_value=''), \
                patch.object(planner, 'read_list', return_value=['test_cluster']), \
                patch.object(planner, 'weights', side_effect=lambda modules, *_: {m: 1 for m in modules}):
            return planner.make_plan('workflow_dispatch', None, repo=self.repo,
                                     head=self.head, base=self.base, mode='lot',
                                     contract_policy=self.policy if policy else None)

    def packet_modules(self, plan):
        def generate(argv, **kwargs):
            Path(argv[argv.index('--out') + 1]).write_text(json.dumps(plan))
            return SimpleNamespace(returncode=0)
        with patch.object(packet, 'run_command', side_effect=generate), \
                patch.object(packet, 'project_python', return_value=sys.executable):
            return {m.removeprefix('tests.') for m in packet.contract_modules(
                self.repo, base=self.base)}

    def test_same_serialized_union_at_three_entries_and_digest(self):
        plan = json.loads(json.dumps(self.plan()))
        expected = {'test_cluster', 'test_declared', 'test_universal',
                    'test_changed', 'test_map'}
        shared = plan['contract_plan']
        self.assertEqual(set(cp.consume(shared)), expected)
        self.assertEqual(set(pv.planned_modules(plan)), expected)
        self.assertEqual(self.packet_modules(plan), expected | {'test_l161f_plan'})
        self.assertEqual(pv.meets_canon(plan, self.params, str(self.repo), head=self.head,
                                       contract_policy=self.policy), (True, ''))
        self.assertEqual(len(shared['registry_digest']), 64)
        self.assertIn('rule:changed', shared['evidence']['test_declared']['reasons'])

    def test_worktree_registry_does_not_change_candidate_plan(self):
        before = self.plan()['contract_plan']
        self.write('tests/parts/registry.json', 'broken local edit')
        self.assertEqual(self.plan()['contract_plan'], before)

    def test_absent_map_full_ci_contract_fallback_keeps_declared(self):
        plan = self.plan(used=False)
        self.assertEqual(plan['selection']['mode'], 'full')
        self.assertIn('test_declared', self.packet_modules(plan))
        self.assertNotIn('test_unused', self.packet_modules(plan))
        prepared = SimpleNamespace(plan=plan, path=str(self.repo / 'plan.json'),
                                   tree=str(self.repo))
        with patch.object(calls, '_tree_modules', return_value=set(self.inventory)), \
                patch.object(calls, '_changed_paths', return_value=['watched.py']), \
                patch.object(calls.transverse_contracts, 'problem', return_value=''):
            result = calls.contract_fallback(prepared, self.params, head=self.head, base=self.base)
        self.assertIsNotNone(result)
        fallback = json.loads((self.repo / 'plan.json').read_text())
        self.assertIn('test_declared', pv.planned_modules(fallback))
        self.assertIn('test_universal', pv.planned_modules(fallback))
        self.assertEqual(fallback['contract_plan'], plan['contract_plan'])
        self.assertEqual(pv.meets_canon(fallback, self.params, str(self.repo), head=self.head,
                                       contract_policy=self.policy), (True, ''))

    def test_stale_map_packet_takes_contracts_only(self):
        # #7091 : une carte périmée vaut une carte absente pour la passe des
        # contrats du paquet ; la sélection d'impact (test_changed, test_map)
        # n'y entre pas.
        expected = self.packet_modules(self.plan(used=False))
        stale = self.packet_modules(self.plan(stale=True))
        self.assertEqual(stale, expected)
        self.assertNotIn('test_map', stale)
        self.assertNotIn('test_changed', stale)
        self.assertIn('test_map', self.packet_modules(self.plan()))

    def test_stale_map_rules_unchanged(self):
        plan = self.plan(stale=True)
        self.assertEqual(plan['selection']['mode'], 'impacted')
        self.assertIn('card-stale', pv.selection(plan, head=self.head).causes)
        self.assertIn('test_declared', pv.planned_modules(plan))

    def test_n_minus_one_does_not_add_registry_obligations(self):
        plan = self.plan(policy=False)
        self.assertFalse(plan['contract_plan']['active'])
        self.assertIsNone(plan['contract_plan']['registry_digest'])
        self.assertNotIn('test_declared', pv.planned_modules(plan))

    def test_unknown_path_and_deleted_module_request_full_suite(self):
        self.write('unknown.file', 'new')
        self.git('add', '.')
        self.git('commit', '-qm', 'unknown')
        self.head = self.git('rev-parse', 'HEAD')
        plan = self.plan()
        self.assertTrue(plan['contract_plan']['full_suite_required'])
        self.assertEqual(plan['selection']['mode'], 'full')
        self.assertIn('unknown-path:unknown.file', plan['contract_plan']['fallback_reasons'])

    def test_unknown_paths_keep_contracts_bounded_at_local_entries(self):
        for path in ('docs/new.md', 'src/other.py'):
            self.write(path, 'outside registry')
        self.git('add', '.')
        self.git('commit', '-qm', 'unknown paths and matched rule')
        self.head = self.git('rev-parse', 'HEAD')
        for used in (False, True):
            with self.subTest(map_used=used):
                plan = self.plan(used=used)
                shared = plan['contract_plan']
                self.assertEqual(set(shared['contracts']),
                                 {'test_cluster', 'test_declared', 'test_universal'})
                self.assertTrue(shared['full_suite_required'])
                self.assertEqual(plan['selection']['mode'], 'full')
                ci_modules = {m for parts in plan['parts'].values()
                              for part in parts for m in part['modules']}
                self.assertEqual(ci_modules, set(self.inventory))
                self.assertIn('test_declared', self.packet_modules(plan))
                self.assertIn('test_universal', self.packet_modules(plan))
                self.assertNotIn('test_unused', self.packet_modules(plan))
                prepared = SimpleNamespace(plan=plan, path=str(self.repo / 'plan.json'),
                                           tree=str(self.repo))
                with patch.object(calls, '_tree_modules', return_value=set(self.inventory)), \
                        patch.object(calls, '_changed_paths',
                                     return_value=['watched.py', 'docs/new.md', 'src/other.py']), \
                        patch.object(calls.transverse_contracts, 'problem', return_value=''):
                    result = calls.contract_fallback(prepared, self.params,
                                                     head=self.head, base=self.base)
                self.assertIsNotNone(result)
                fallback = json.loads((self.repo / 'plan.json').read_text())
                self.assertNotIn('test_unused', pv.planned_modules(fallback))
                self.assertIn('test_declared', pv.planned_modules(fallback))
                self.assertIn('test_universal', pv.planned_modules(fallback))
                self.assertEqual(pv.meets_canon(
                    fallback, self.params, str(self.repo), head=self.head,
                    contract_policy=self.policy), (True, ''))

    def test_unreadable_registry_keeps_legacy_without_inventory_expansion(self):
        self.write('tests/parts/registry.json', 'broken')
        self.git('commit', '-qam', 'invalid registry')
        self.head = self.git('rev-parse', 'HEAD')
        shared = self.plan()['contract_plan']
        self.assertEqual(shared['contracts'], ['test_cluster'])
        self.assertTrue(shared['full_suite_required'])

    def test_prevalidation_rejects_forged_digest_or_removed_contract(self):
        for field, value in (('registry_digest', '0' * 64),
                             ('contracts', ['test_cluster'])):
            with self.subTest(field=field):
                plan = self.plan()
                plan['contract_plan'][field] = value
                self.assertFalse(pv.meets_canon(
                    plan, self.params, str(self.repo), head=self.head,
                    contract_policy=self.policy)[0])
        plan = self.plan()
        for parts in plan['parts'].values():
            for part in parts:
                part['modules'] = [m for m in part['modules'] if m != 'test_declared']
        self.assertFalse(pv.meets_canon(plan, self.params, str(self.repo), head=self.head,
                                       contract_policy=self.policy)[0])

    def test_active_policy_cannot_be_downgraded_to_legacy_plan(self):
        plan = self.plan(policy=False)
        self.assertFalse(pv.meets_canon(plan, self.params, str(self.repo), head=self.head,
                                       contract_policy=self.policy)[0])

    def test_unavailable_packet_plan_is_nonconclusive_without_qualification(self):
        composition = packet.Composition(units=[], discarded=[], head=self.head)
        with patch.object(packet, 'contract_modules', side_effect=packet.PaquetAutoError('timeout')):
            runs, faulty, removals = packet.check_local_tests(
                self.repo, composition, timeout=10, base_sha=self.base,
                select=lambda *_: ({}, ''), parallel=1, log=lambda *_: None)
        self.assertEqual((faulty, removals), ([], []))
        self.assertEqual(len(runs), 1)
        self.assertFalse(runs[0].green)
        self.assertFalse(runs[0].finished)
        self.assertEqual(runs[0].modules, [])

    def test_consumer_refuses_incomplete_union(self):
        shared = copy.deepcopy(self.plan()['contract_plan'])
        shared['modules'].remove('test_declared')
        with self.assertRaises(ValueError):
            cp.consume(shared)
