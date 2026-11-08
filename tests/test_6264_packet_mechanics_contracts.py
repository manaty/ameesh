# SPDX-License-Identifier: AGPL-3.0-only
"""#6264: behavior contracts cannot depend on a measured import edge.

The seven packet regressions are the acceptance cases. No map, database or
GitHub access is needed: exercise the actual packet reader's N-1 path and
the declarative reader independently, with no impacted modules supplied.
"""
from pathlib import Path
import unittest

from ameesh import contract_selection as cs
from tests.test_contract_plan import packet

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    'h-03-16': ('test_987_relance_hosted', 'test_l161c_failures_realtime'),
    'h-03-17': ('test_1256_ci_runners',),
    'h-03-18': ('test_l100_dev_cli', 'test_1256_ci_runners', 'test_5655_action_download'),
    'h-05-23': ('test_503_agent_host',),
    'h-06-22': ('test_5035_move_lot_of_another',),
    'h-07-29': ('test_4322_paquet_fusion_pendant_tests',),
    'h-08-01': ('test_4217_paquet_attribution_prefixes',),
}


class MechanicsContractTest(unittest.TestCase):
    def assert_incidents_covered(self, modules):
        for incident, required in CASES.items():
            for module in required:
                with self.subTest(incident=incident, module=module):
                    self.assertIn(module, modules)

    def test_packet_without_map_or_canon_selects_behavior_contracts(self):
        # Empty base is the real legacy path: no planner/map/canon invoked.
        # Thus old deployed composers also receive this correction.
        modules = packet.contract_modules(ROOT)
        self.assert_incidents_covered({m.removeprefix('tests.') for m in modules})
        self.assertEqual(len(modules), len(set(modules)))

    def test_registry_keeps_behavior_contracts_without_an_impact_edge(self):
        registry = cs.compile_registry((ROOT / 'tests/parts/contrats.json').read_text())
        inventory = sorted(p.stem for p in (ROOT / 'tests').glob('test_*.py'))
        for changes in ((), (cs.Change('M', 'src/ameesh/main.py'),),
                        (cs.Change('M', 'scripts/paquet-auto.py'),)):
            with self.subTest(changes=changes):
                result = cs.select(registry, changes, inventory, commit='a' * 40)
                # Prove the contracts, rather than accidentally passing via
                # an unknown-path fallback that selects the entire inventory.
                self.assertFalse(result.full_suite_required, result.fallback_reasons)
                self.assert_incidents_covered(result.modules)
                evidence = result.to_dict()['evidence']
                for required in CASES.values():
                    for module in required:
                        self.assertIn('universal:observation-always',
                                      evidence[module]['reasons'])
