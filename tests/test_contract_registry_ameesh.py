# SPDX-License-Identifier: AGPL-3.0-only
"""#6180: initial registry in observation, additive to the N-1 contract list.

These are selection witnesses, not executions of the selected SQL/CLI tests.
The plan adapter (unit C) must keep reading toujours.txt and union its results.
Until it lands, toujours.txt also carries the new witnesses for old readers.
"""
from __future__ import annotations

from fnmatch import fnmatchcase
import json
from pathlib import Path
import unittest

from ameesh import contract_selection as cs, mechanics
from tests.test_l1616_mechanics import MECHANICS

REPO = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO / 'tests/parts/contrats.json'
SHA = 'a' * 40


def always_contracts():
    return {line.split('#', 1)[0].strip()
            for line in (REPO / 'tests/parts/toujours.txt').read_text().splitlines()
            if line.split('#', 1)[0].strip()}


class AmeeshRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = cs.compile_registry(REGISTRY_PATH.read_text())
        cls.inventory = tuple(sorted(p.stem for p in (REPO / 'tests').glob('test_*.py')))

    def select(self, *changes, registry=None):
        return cs.select(registry or self.registry, changes, self.inventory, commit=SHA)

    def assert_contract(self, path, module, rule, operation='M'):
        result = self.select(cs.Change(operation, path))
        self.assertFalse(result.full_suite_required, result.fallback_reasons)
        evidence = result.to_dict()['evidence'][module]
        self.assertIn('rule:' + rule, evidence['reasons'])
        self.assertIn(path, evidence['paths'])
        return result

    def test_6090_h12_additive_migration_and_n_minus_one_contract(self):
        path = 'src/ameesh/migrations/0555_work_add_unique_ref.sql'
        for module in ('test_migrations_additives', 'test_migrations',
                       'test_6090_work_add_unique_ref', 'test_cluster'):
            with self.subTest(module=module):
                self.assert_contract(path, module, 'migrations', 'A')

    def test_6090_h13_mediated_storage_contract(self):
        for path in ('src/ameesh/storage/interface.py',
                     'src/ameesh/storage/postgres/work.py',
                     'src/ameesh/mediated_executor/contract.json'):
            with self.subTest(path=path):
                self.assert_contract(path, 'test_l107_executor_contract', 'mediated-storage')

    def test_6091_h09_cli_literals_and_command_contract(self):
        for module in ('test_4259_client_literals', 'test_l102_cli_vocab'):
            self.assert_contract('src/ameesh/mesh_cli.py', module, 'cli')
        # The same incident touched work.py: it has no explicit rule yet,
        # so its safety comes from the named full fallback, not guesswork.
        result = self.select(cs.Change('M', 'src/ameesh/work.py'))
        self.assertIn('unknown-path:src/ameesh/work.py', result.fallback_reasons)
        self.assertEqual(result.modules, self.inventory)

    def test_sql_roles_keep_column_and_reader_rights_contracts(self):
        for module in ('test_cluster', 'test_2340_auditeur_role',
                       'test_6011_contrat_ecrivains_isoles'):
            self.assert_contract('deploy/sql/role-module-writers.sql', module, 'sql-roles')

    def test_registry_and_engine_select_all_declared_contracts(self):
        known = {m for r in self.registry.rules for m in r.modules}
        for path in ('tests/parts/contrats.json', 'tests/parts/toujours.txt',
                     'src/ameesh/contract_selection.py', 'scripts/test-plan.py',
                     'src/ameesh/mechanics.py', 'scripts/paquet-auto.py',
                     'tests/test_l161f_plan.py', 'src/ameesh/contract_plan.py',
                     'src/ameesh/prevalidation_calls.py'):
            result = self.select(cs.Change('M', path))
            self.assertFalse(result.full_suite_required, result.fallback_reasons)
            self.assertEqual(set(result.modules), known)
            for module in known:
                self.assertIn('rule:registry-and-selection',
                              result.to_dict()['evidence'][module]['reasons'])

    def test_observation_preserves_legacy_union_and_inventory(self):
        declared = {m for r in self.registry.rules for m in r.modules}
        self.assertLessEqual(declared, set(self.inventory))
        legacy = always_contracts()
        universal = {m for r in self.registry.rules if not r.paths for m in r.modules}
        self.assertEqual(universal, legacy)
        self.assertEqual(declared, legacy | {"test_l161f_plan", "test_contract_plan",
                                                  "test_6182_contracts_before_publish"})
        for changes in ((), (cs.Change('M', 'src/ameesh/storage/interface.py'),),
                        (cs.Change('A', 'unknown/new.file'),)):
            result = self.select(*changes)
            self.assertLessEqual(legacy, set(result.modules))
            self.assertEqual(result.modules, tuple(sorted(set(result.modules) | legacy)))

    def test_renames_both_sides_and_deletion(self):
        old = 'src/ameesh/storage/postgres/work.py'
        new = 'src/ameesh/storage/postgres/work_new.py'
        renamed = self.select(cs.Change('R', new, old))
        self.assertFalse(renamed.full_suite_required)
        self.assertEqual(renamed.to_dict()['evidence']['test_l107_executor_contract']['paths'],
                         sorted([old, new]))
        deleted = self.select(cs.Change('D', old))
        self.assertFalse(deleted.full_suite_required)
        self.assertIn(old, deleted.to_dict()['evidence']['test_l107_executor_contract']['paths'])
        moved = self.select(cs.Change('R', 'new/unknown.py', old))
        self.assertIn('unknown-path:new/unknown.py', moved.fallback_reasons)
        self.assertEqual(moved.modules, self.inventory)

    def test_no_overlapping_rules_on_tracked_and_future_paths(self):
        # Existing files plus future additions in nested storage/migration dirs:
        # fnmatch '*' spans '/', so broad src/ameesh/* rules would be unsafe.
        paths = {p.relative_to(REPO).as_posix()
                 for root in ('src/ameesh', 'scripts', 'deploy/sql', 'tests/parts')
                 for p in (REPO / root).rglob('*') if p.is_file()}
        paths.update(('src/ameesh/storage/postgres/future_cli.py',
                      'src/ameesh/migrations/9999_future.sql',
                      'src/ameesh/approve/cli.py'))
        for path in sorted(paths):
            for operation in cs.OPERATIONS:
                matches = [r.id for r in self.registry.rules if r.paths
                           and operation in r.operations
                           and any(fnmatchcase(path, glob) for glob in r.paths)]
                self.assertLessEqual(len(matches), 1, (path, operation, matches))

    def test_order_is_deterministic_and_ambiguous_rules_fall_back(self):
        changes = [cs.Change('M', 'src/ameesh/storage/interface.py'),
                   cs.Change('A', 'src/ameesh/migrations/9999_future.sql')]
        self.assertEqual(self.select(*changes).to_dict(),
                         self.select(*reversed(changes)).to_dict())
        doc = json.loads(REGISTRY_PATH.read_text())
        doc['rules'].append(dict(id='accidental-overlap', paths=['src/ameesh/*'],
                                 operations=list('AMDRCTU'), modules=['test_cluster']))
        result = self.select(changes[0], registry=cs.compile_registry(json.dumps(doc)))
        self.assertIn('ambiguous-path:src/ameesh/storage/interface.py', result.fallback_reasons)
        self.assertEqual(result.modules, self.inventory)

    def test_n_minus_one_canon_remains_without_policy(self):
        compiled = mechanics.compile_document(MECHANICS)
        self.assertIsNone(compiled.override('ameesh'))
        # Adding registry data here must not force a policy on old canon.
        # Deployment order is compatible reader A, data B, policy/adapter C.
        result = cs.select(None, [], self.inventory, commit=SHA,
                           registry_error='policy-absent')
        self.assertEqual(result.modules, self.inventory)
        self.assertIn('policy-absent', result.fallback_reasons)
