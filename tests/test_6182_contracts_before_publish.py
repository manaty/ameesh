# SPDX-License-Identifier: AGPL-3.0-only
"""#6182: composed contracts before publication, scoped and bounded."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ameesh import mechanics
from tests.test_4304_paquet_poussee_precoce import Flux, green, paquet_auto as auto
from tests.test_4271_paquet_series import DOCUMENT, paquet_horaire as hourly


class PublicationTest(unittest.TestCase):
    def composition(self):
        return auto.Composition([auto.Unit(6179, 'a' * 40, 'b' * 40)], [], 'c' * 40)

    def test_contracts_precede_push_and_ci_and_report_is_reused(self):
        run = green('contrats')
        def unit_tests(**kwargs):
            self.assertIs(kwargs['contract_run'], run)
            return [run], [], []
        flow = Flux(self, tests=[unit_tests], compositions=[self.composition()],
                    contract_result=run)
        self.assertEqual(flow.run(), 0)
        names = flow.names()
        for before, after in (('freeze-checks', 'contracts'), ('manifest', 'contracts'),
                              ('contracts', 'push'), ('push', 'ci-full'), ('ci-full', 'tests')):
            self.assertLess(names.index(before), names.index(after))
        self.assertEqual(len(flow.calls('contracts')), 1)

    def test_regressions_A_and_B_suspend_attempt_before_publication(self):
        for module, test in (('test_2449_garde_mecanique', 'test_the_guard_keys_are_the_reader_keys'),
                             ('test_l161f_plan', 'test_always_list_sorted_and_known')):
            with self.subTest(module=module):
                run = auto.TestRun('contrats', ['tests.' + module], 1,
                                   'FAIL: ' + test + ' (tests.' + module + '.T.' + test + ')\n', 1)
                flow = Flux(self, tests=[], compositions=[self.composition()], contract_result=run)
                with self.assertRaisesRegex(auto.PaquetAutoError, 'avant publication'):
                    flow.run()
                for operation in ('push', 'pr', 'freeze', 'ci-full', 'tests', 'withdraw', 'recompose'):
                    self.assertEqual(flow.calls(operation), [])

    def test_timeout_is_published_with_full_ci_and_no_second_contract_series(self):
        run = auto.TestRun('contrats', ['tests.test_x'], None, 'slot wait exhausted', 10, timed_out=True)
        flow = Flux(self, tests=[([run], [], [])], compositions=[self.composition()], contract_result=run)
        self.assertEqual(flow.run(), 0)
        self.assertEqual(len(flow.calls('push')), 1)
        self.assertEqual(len(flow.calls('ci-full')), 1)
        self.assertFalse(run.green)
        self.assertTrue(run.inconclusive)

    def test_local_pass_does_not_reexecute_supplied_report(self):
        run = green('contrats')
        with mock.patch.object(auto, 'contract_modules', side_effect=AssertionError('duplicate plan')), \
                mock.patch.object(auto, 'run_tests', side_effect=AssertionError('duplicate tests')):
            runs, faulty, removed = auto.check_local_tests(
                Path('.'), auto.Composition([], [], 'a' * 40), timeout=10,
                select=lambda *_: ({}, ''), parallel=1, contract_run=run, log=lambda *_: None)
        self.assertEqual(runs, [run])
        self.assertEqual((faulty, removed), ([], []))


class ScopeAndBudgetTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'tests@example.invalid')
        self.git('config', 'user.name', 'Tests')
        (self.repo / 'manifest.md').write_text('final manifest')
        self.git('add', '.')
        self.git('commit', '-qm', 'final tree')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_one_budget_includes_plan_and_heavy_slot_wait(self):
        clock = [0.0]
        def plan(*args, **kwargs):
            self.assertEqual(kwargs['timeout'], 12)
            clock[0] += 3
            return ['tests.test_x']
        def execute(*args, **kwargs):
            self.assertEqual(kwargs, {'timeout': 9, 'heavy': True, 'wall': 9, 'failfast': True})
            clock[0] += 9
            return auto.TestRun('contrats', ['tests.test_x'], None, 'slot timeout', 9, timed_out=True)
        with mock.patch.object(auto.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(auto.shutil, 'which', return_value='/bin/ameesh'), \
                mock.patch.object(auto, 'contract_modules', side_effect=plan), \
                mock.patch.object(auto, 'run_tests', side_effect=execute):
            report = auto.prepublication_contracts(self.repo, base='base', budget=12)
        self.assertEqual(report.tree, self.git('rev-parse', 'HEAD^{tree}'))
        self.assertTrue(report.run.inconclusive)
        self.assertEqual(report.run.seconds, 12)

    def test_changed_tree_or_dirty_worktree_is_out_of_scope(self):
        for mutation in ('dirty', 'commit'):
            with self.subTest(mutation=mutation):
                def execute(*args, **kwargs):
                    (self.repo / 'manifest.md').write_text(mutation)
                    if mutation == 'commit':
                        self.git('commit', '-qam', 'changed tree')
                    return green('contrats')
                with mock.patch.object(auto.shutil, 'which', return_value='/bin/ameesh'), \
                        mock.patch.object(auto, 'contract_modules', return_value=['tests.test_x']), \
                        mock.patch.object(auto, 'run_tests', side_effect=execute):
                    with self.assertRaisesRegex(auto.PaquetAutoError, 'hors portée'):
                        auto.prepublication_contracts(self.repo, base='base', budget=12)
                self.git('reset', '--hard', 'HEAD')

    def test_first_negative_stops_before_a_later_module_can_time_out(self):
        tests = self.repo / 'tests'
        tests.mkdir()
        (tests / '__init__.py').write_text('')
        (tests / 'test_early.py').write_text(
            'import unittest\nclass T(unittest.TestCase):\n'
            ' def test_fail(self): self.fail("regression A/B")\n')
        (tests / 'test_late.py').write_text(
            'import unittest\nfrom pathlib import Path\nclass T(unittest.TestCase):\n'
            ' def test_later(self): Path("late-ran").write_text("ran")\n')
        with mock.patch.object(auto, 'project_python', return_value=sys.executable), \
                mock.patch.object(auto, 'save_test_log', side_effect=lambda run: run):
            run = auto.run_tests(self.repo, 'contrats', ['tests.test_early', 'tests.test_late'],
                                 timeout=10, failfast=True)
        self.assertTrue(run.real_failure, run.output)
        self.assertFalse((self.repo / 'late-ran').exists())

    def test_changed_head_before_pass_cannot_qualify_another_candidate(self):
        with mock.patch.object(auto, 'run_tests') as tests:
            with self.assertRaisesRegex(auto.PaquetAutoError, 'hors portée'):
                auto.prepublication_contracts(self.repo, base='base', budget=12,
                                              expected_head='0' * 40)
        tests.assert_not_called()

    def test_missing_slot_is_nonconclusive_and_never_runs_unisolated(self):
        with mock.patch.object(auto.shutil, 'which', return_value=None), \
                mock.patch.object(auto, 'run_tests') as tests, \
                mock.patch.object(auto, 'save_test_log', side_effect=lambda run: run):
            report = auto.prepublication_contracts(self.repo, base='base', budget=12)
        tests.assert_not_called()
        self.assertTrue(report.run.inconclusive)


class PolicyTest(unittest.TestCase):
    def test_optional_budget_is_read_without_shifting_n_minus_one_total(self):
        old = mechanics.compile_document(DOCUMENT + 'packet_tests:\n  unit_timeout_seconds: 900\n  parallel: 2\n')
        self.assertIsNone(old.packet_tests.contracts_timeout_seconds)
        doc = mechanics.compile_document(DOCUMENT + 'packet_tests:\n  unit_timeout_seconds: 900\n  parallel: 2\n  contracts_timeout_seconds: 123\n')
        self.assertEqual(doc.packet_tests.contracts_timeout_seconds, 123)
        self.assertIsNone(doc.packet_tests.total_timeout_seconds)
        self.assertEqual(doc.to_dict()['packet_tests']['contracts_timeout_seconds'], 123)
        with self.assertRaises(mechanics.MechanicsError):
            mechanics.compile_document(DOCUMENT + 'packet_tests:\n  unit_timeout_seconds: 900\n  parallel: 2\n  contracts_timeout_seconds: 0\n')

    def test_scheduler_passes_compiled_budget_and_n_minus_one_declared_fallback(self):
        for policy, expected in ((mechanics.PacketTests(900, 2, contracts_timeout_seconds=123), '123'),
                                 (mechanics.PacketTests(900, 2), '900')):
            shell = mock.Mock()
            shell.run.return_value = hourly.Result(0, '', '')
            ctx = hourly.Context(repo=Path('.'), target='main', project='ameesh', pilot='p',
                                 actor='p', shell=shell, python='python3', unit_tests=policy)
            ctx.say = mock.Mock()
            hourly.paquet_auto(ctx, 'h-20261107-25', 6182)
            argv = shell.run.call_args.args[0]
            self.assertEqual(argv[argv.index('--contracts-budget') + 1], expected)
