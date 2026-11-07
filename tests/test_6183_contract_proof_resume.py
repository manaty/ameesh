# SPDX-License-Identifier: AGPL-3.0-only
"""#6183: proof-scoped publication, recovery, and project rollback."""
import json
from pathlib import Path
from unittest import TestCase, mock

from ameesh import mechanics, packet_contract_proof as proof
from tests.test_l1616_mechanics import MECHANICS
from tests.test_3845_paquet_horaire import Base, ph, carrier, HEAD, red_run
from tests.test_4304_paquet_poussee_precoce import Flux, green, paquet_auto as auto
from tests import test_4237_paquet_reprise as recovery
recovery_auto = recovery.auto

TREE = 'e' * 40
BASE = 'c' * 40


def report(run=None):
    return auto.ContractPass(HEAD, TREE, run or green('contrats'))


def event(run=None, **changes):
    value = json.loads(proof.encode(report(run), base=BASE, target='main')[len(proof.MARK):])
    value.update(changes)
    return {'note': proof.MARK + json.dumps(value)}


class PolicyTest(TestCase):
    def test_unreadable_canon_keeps_contracts_first(self):
        for error in (OSError('unreadable'), ValueError('invalid'),
                      mechanics.MechanicsError('no readable canon')):
            with self.subTest(error=error), mock.patch(
                    'ameesh.mechanics.configured', side_effect=error):
                self.assertEqual(proof.policy('demo'), 'contracts-first')

    def compile(self, extra=''):
        return mechanics.compile_document(MECHANICS.replace('projects: {}', 'projects:\n  demo:\n    contract_selection:\n      registry: tests/parts/contrats.json\n' + extra))

    def test_absent_explicit_and_rollback(self):
        self.assertEqual(proof.from_document(mechanics.compile_document(MECHANICS), 'demo'), 'contracts-first')
        for value in (None, 'contracts-first', 'legacy'):
            doc = self.compile('      publish: ' + value + '\n' if value else '')
            self.assertEqual(proof.from_document(doc, 'demo'), value or 'contracts-first')
            selected = doc.override('demo').contract_selection
            self.assertEqual(selected.publish, value or 'contracts-first')
        with self.assertRaisesRegex(mechanics.MechanicsError, 'publish'):
            self.compile('      publish: typo\n')


class EvidenceTest(TestCase):
    def read(self, *events):
        return proof.read({'events': list(events)}, head=HEAD, tree=TREE, target='main')

    def test_absent_malformed_wrong_head_tree_target_do_not_qualify(self):
        self.assertIsNone(self.read())
        self.assertIsNone(self.read({'note': proof.MARK + '{}'}))
        for changes in ({'head': 'f' * 40}, {'tree': 'f' * 40}, {'target': 'elsewhere'},
                        {'schema': 'old'}):
            self.assertIsNone(self.read(event(**changes)))
        self.assertEqual(self.read(event())['result']['state'], 'green')
        self.assertIsNone(self.read(event(), event(tree='f' * 40)))

    def test_later_unverifiable_projection_invalidates_green(self):
        for payload in ('{', 'null', '[]', '{}', '{"head": null}',
                        '{"head": []}', '{"head": "unreadable"}'):
            with self.subTest(payload=payload):
                self.assertIsNone(self.read(event(), {'note': proof.MARK + payload}))
        self.assertIsNone(self.read(event(), event(head='f' * 40)))
        self.assertEqual(self.read(event(), {'note': 'unrelated'})['result']['state'], 'green')
        self.assertEqual(self.read({'note': proof.MARK + '{'}, event())['result']['state'], 'green')

    def test_partial_negative_has_priority_and_timeout_is_not_green(self):
        red = auto.TestRun('a', ['tests.test_a'], 1,
                           'FAIL: test_a (tests.test_a.T.test_a)\n', 1)
        timeout = auto.TestRun('b', ['tests.test_b'], None, 'not played', 0, timed_out=True)
        merged = auto.ContractTestRun('contrats', red.modules + timeout.modules, 1,
                                       red.output, 1, children=[red, timeout])
        value = self.read(event(merged))['result']
        self.assertEqual(value['state'], 'red')
        self.assertEqual([child['state'] for child in value['children']], ['red', 'inconclusive'])
        self.assertEqual(self.read(event(timeout))['result']['state'], 'inconclusive')


class HourlyProofTest(Base):
    def setUp(self):
        super().setUp()
        self.ctx.contract_publication = 'contracts-first'
        self.item = carrier()
        self.shell.items[4000] = self.item
        patch = mock.patch.object(self.ctx, 'git', return_value=ph.Result(0, TREE + '\n'))
        patch.start()
        self.addCleanup(patch.stop)
        merged = mock.patch.object(ph, 'merged_head', return_value=False)
        merged.start()
        self.addCleanup(merged.stop)

    def test_missing_or_red_requires_fresh_verification_without_ci_exemption(self):
        red = auto.TestRun('contrats', ['tests.test_a'], 1,
                           'FAIL: test_a (tests.test_a.T.test_a)\n', 1)
        for events in ([], [event(red)]):
            self.item['events'] = events
            with mock.patch.object(ph, 'attempt', return_value='reverify') as attempt:
                self.assertEqual(ph.publish(self.ctx, self.item), 'reverify')
            self.assertEqual(attempt.call_args.args[3].publish,
                             self.item['branch'] + '@' + HEAD)
        self.assertFalse(self.shell.ran('ameesh', 'ci', 'run'))
        self.assertFalse(self.shell.ran('ameesh', 'work', 'reviewer'))

    def test_refused_recovery_keeps_original_members_even_from_composition_step(self):
        current = ph.Attempt(ph.COMPOSITION, 1, BASE,
                             publish=self.item['branch'] + '@' + HEAD)
        result = ph.Result(ph.auto.PUBLISH_REFUSED, '', 'contrats rouges : recomposer')
        with mock.patch.object(ph, 'attempt', return_value='recomposed') as attempt:
            self.assertEqual(ph.after_compose(self.ctx, 4000, 'h-20261027-04',
                                             result, current), 'recomposed')
        retry = attempt.call_args.args[3]
        self.assertEqual(retry.step, ph.RECOMPOSITION)
        self.assertEqual(retry.base, HEAD)
        self.assertEqual(retry.run_id, ph.CONTRACT_PROOF_RUN)
        self.assertFalse(retry.publish)

    def test_inconclusive_and_green_publish_full_ci_but_only_green_is_green(self):
        timeout = auto.TestRun('contrats', ['tests.test_x'], None, 'timeout', 10, timed_out=True)
        for run in (timeout, green('contrats')):
            self.item['events'] = [event(run)]
            self.assertIn('ci-full demandée', ph.publish(self.ctx, self.item))
        self.assertEqual(len(self.shell.ran('ameesh', 'ci', 'run')), 2)
        self.assertEqual(proof.read({'events': [event(timeout)]}, head=HEAD,
                                   tree=TREE, target='main')['result']['state'], 'inconclusive')

    def test_ci_resume_reads_evidence_and_does_not_upgrade_timeout(self):
        timeout = auto.TestRun('contrats', ['tests.test_x'], None, 'timeout', 10, timed_out=True)
        self.item['events'] = [event(timeout), {'note': ph.MARK_CI + HEAD}]
        self.shell.ci[4000] = {'runs': [dict(red_run(), status='success')]}
        with mock.patch.object(proof, 'read', wraps=proof.read) as read:
            self.assertIn('ci-full verte', ph.follow(self.ctx, self.item))
        read.assert_called_once()
        self.assertEqual(proof.read(self.item, head=HEAD, tree=TREE,
                                   target='main')['result']['state'], 'inconclusive')

    def test_successful_publish_recovery_follows_same_head_ci(self):
        for status in ('success', 'failure'):
            with self.subTest(status=status):
                self.item['events'] = []
                self.shell.calls.clear()
                self.shell.auto_out = proof.encode(report(), base=BASE, target='main') + '\n'
                self.assertIn('ci-full demandée', ph.publish(self.ctx, self.item))
                current = ph.attempts(self.item)[-1]
                self.assertEqual(current.base, HEAD)
                self.assertEqual(current.publish, self.item['branch'] + '@' + HEAD)
                self.assertIsNone(ph.pending(self.ctx, self.item, HEAD))
                self.shell.ci[4000] = {'runs': [dict(red_run(), status=status)]}
                self.shell.calls.clear()
                with mock.patch.object(ph, 'resume', side_effect=AssertionError('already published')), \
                        mock.patch.object(ph, 'recompose', return_value='red classified') as recompose:
                    out = ph.follow(self.ctx, self.item)
                if status == 'success':
                    self.assertIn('ci-full verte', out)
                    recompose.assert_not_called()
                else:
                    self.assertEqual(out, 'red classified')
                    recompose.assert_called_once()
                self.assertFalse(self.shell.ran(str(Path(ph.SCRIPT_DIR) / 'paquet-auto.py')))
                self.assertFalse(self.shell.ran('ameesh', 'ci', 'run'))

    def test_unfinished_recomposition_stays_pending_despite_old_base_ci(self):
        self.item['events'] = [{'note': ph.MARK_CI + HEAD, 'created_ts': 1},
                               {'note': ph.MARK_ATTEMPT + 'recomposition n=1 base=' + HEAD
                                + ' par=old:1 run=failed-ci sans=3801', 'created_ts': 2}]
        self.assertIsNotNone(ph.pending(self.ctx, self.item, HEAD))

    def test_ci_red_recomposition_preserves_exclusions_and_original_members(self):
        self.shell.items[3801] = {'id': 3801, 'assignee': 'author'}
        with mock.patch.object(ph, 'after_compose', return_value='composed'):
            result = ph.recompose(self.ctx, self.item, {'run_id': 'h-21'},
                                  ph.Verdict(True, [3801], ['tests.test_x.T.test_a'], ''))
        self.assertEqual(result, 'composed')
        cmd = self.shell.ran(str(Path(ph.SCRIPT_DIR) / 'paquet-auto.py'))[0]
        self.assertEqual(cmd[cmd.index('--exclude') + 1], '3801')
        self.assertEqual(cmd[cmd.index('--keep-composition') + 1], HEAD)
        self.assertEqual(cmd[cmd.index('--contract-publication') + 1], 'contracts-first')
        self.assertNotIn('--publish', cmd)

    def test_legacy_does_not_read_contract_evidence(self):
        self.ctx.contract_publication = 'legacy'
        with mock.patch.object(proof, 'read', side_effect=AssertionError('reuse')):
            self.assertIn('ci-full demandée', ph.publish(self.ctx, self.item))


class LegacyCompositionTest(TestCase):
    def test_rollback_restores_4304_order_without_prepublication_reuse(self):
        composition = auto.Composition([auto.Unit(1, 'a' * 40, 'b' * 40)], [], 'c' * 40)
        def unit_tests(**kwargs):
            self.assertIsNone(kwargs['contract_report'])
            self.assertTrue(kwargs['legacy_contracts'])
            return [green()], [], []
        flow = Flux(self, tests=[unit_tests], compositions=[composition], contract_publication='legacy')
        self.assertEqual(flow.run(), 0)
        self.assertFalse(flow.calls('contracts'))
        self.assertLess(flow.names().index('ci-full'), flow.names().index('tests'))


class RecoveryProofTest(recovery.Chain):
    args = recovery.PublishTest.args
    finishing = recovery.PublishTest.finishing
    push = recovery.PublishTest.push
    publish = recovery.PublishTest.publish
    def test_recovery_reexecutes_contracts_even_after_old_tested_marker(self):
        args = self.args(contract_publication='contracts-first')
        with mock.patch.object(recovery_auto, 'prepublication_contracts', wraps=recovery_auto.prepublication_contracts) as execute:
            rc, _ = self.publish(args)
        self.assertEqual(rc, 0)
        execute.assert_called_once()

    def test_regelled_source_refuses_before_any_contract_reuse(self):
        args = self.args(contract_publication='contracts-first')
        candidates = [recovery_auto.Candidate(1, self.sources[1]),
                      recovery_auto.Candidate(2, self.base)]
        with mock.patch.object(recovery_auto, 'prepublication_contracts') as execute:
            with self.assertRaisesRegex(recovery_auto.ChainRefused, '#2 : source enregistrée'):
                self.publish(args, candidates=candidates)
        execute.assert_not_called()

    def test_recovery_red_refuses_then_scheduler_recomposes(self):
        args = self.args(contract_publication='contracts-first')
        red = recovery_auto.TestRun('contrats', ['tests.test_a'], 1,
                                    'FAIL: test_a (tests.test_a.T.test_a)\n', 1)
        evidence = recovery_auto.ContractPass(self.head, recovery_auto.tree_of(self.repo, self.head), red)
        with mock.patch.object(recovery_auto, 'prepublication_contracts', return_value=evidence):
            with self.assertRaisesRegex(recovery_auto.ChainRefused, 'recomposer pour attribution'):
                self.publish(args)
