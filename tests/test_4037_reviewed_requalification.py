# SPDX-License-Identifier: AGPL-3.0-only
"""#4037: reviewed host-local pin moves, with exact opinions and rollback."""
import contextlib
import io
import json
import os
import subprocess
import types
import unittest
from unittest import mock

from ameesh import identity, train_cli, train_requalify as rq
from ameesh import train_requalify_review as review
from ameesh.train import TrainError
from . import test_3483_train_canon_auto as fixture


class ReviewedMoveTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.AutomaticCanonTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.repo
        self.pin = self.fixture.pin
        self.repo.write('processus/mecanique.yaml',
                        'prevalidation:\n  hosts: [agents-3, agents-1]\n')
        self.gate = self.repo.commit('27 October: prevalidation hosts')
        self.cfg, self.root, self.base, self.profile = self.fixture.host('reviewed')
        self.db = object()
        self.marks = [dict(id=1, kind='verdict', actor='pilot', sha=self.gate,
                           verdict='ok', at_ts=1),
                      dict(id=2, kind='verdict', actor='reader', sha=self.gate,
                           verdict='ok', at_ts=2)]
        self.receipts = {
            mark['id']: dict(subject='milestone:%s' % mark['id'], commit=self.gate,
                             actor=mark['actor'], harness=family, session='s', turn='t',
                             record='proof:%s' % mark['id'], milestone=mark)
            for mark, family in zip(self.marks, ('claude', 'codex'))}
        self.rules = types.SimpleNamespace(
            families=('claude', 'codex', 'deepseek'), exclude_author_family=True,
            distinct_families=True,
            reviews_for=lambda paths: types.SimpleNamespace(reviews=2))
        self.stack = self.enterContext(contextlib.ExitStack())
        self.stack.enter_context(mock.patch.object(
            rq.config, '_config_dir_default',
            return_value=os.path.join(self.root, 'runner-profiles')))
        self.stack.enter_context(mock.patch.object(review.mechanics, 'load',
                                                   return_value=self.rules))
        self.stack.enter_context(mock.patch.object(
            review.review_provenance, 'receipt_for_milestone',
            side_effect=lambda db, number: self.receipts.get(number)))
        self.commit_proof = self.stack.enter_context(mock.patch.object(
            review.review_provenance, 'receipt_for_commit',
            return_value=dict(actor='author', harness='deepseek')))
        self.historical_authors = self.stack.enter_context(mock.patch.object(
            review.review_authors, '_unreceived_authors', return_value=([], ['unknown'])))
        self.origins = mock.Mock(return_value=[])
        self.items = {4037: {'id': 4037}, 4011: {'id': 4011}}
        store = types.SimpleNamespace(work=types.SimpleNamespace(
            verdicts_for_commit=lambda commit: self.marks), provenance=types.SimpleNamespace(
            get=lambda reference: dict(work_item_id=4011), origins=self.origins))
        self.stack.enter_context(mock.patch.object(review.storage, 'of', return_value=store))
        self.stack.enter_context(mock.patch.object(review.work, 'milestones',
                                                   side_effect=lambda *a, **k: self.marks))
        self.stack.enter_context(mock.patch.object(
            review.work, 'get',
            side_effect=lambda db, lot: self.items.get(int(lot))))
        self.note = self.stack.enter_context(mock.patch.object(review.work, 'note'))
        self.stack.enter_context(mock.patch.object(
            review.assignments, 'is_orchestrator',
            side_effect=lambda cfg, db, name, **kwargs: name == 'pilot'))

    def execute(self, **kwargs):
        values = dict(lot=4037, actor='bureau', reviewed=True,
                      reviews=['milestone:1', 'milestone:2'],
                      gate_reader=lambda *args: (self.gate, ''))
        values.update(kwargs)
        return rq.execute(self.cfg, self.db, 'ameesh', **values)

    def assert_unchanged(self):
        for path in (self.base, self.profile):
            with open(path) as source:
                self.assertEqual(json.load(source)['train']['projects']['ameesh']
                                 ['canon_commit'], self.pin)
        journal = os.path.join(self.root, 'train-canon.jsonl')
        if os.path.exists(journal):
            with open(journal) as source:
                self.assertEqual(source.read(), '')

    def test_hosts_change_with_two_opinions_is_applied_and_traced(self):
        result = self.execute()
        self.assertEqual(result['mode'], 'reviewed')
        self.assertEqual(result['actor'], 'bureau')
        for path in (self.base, self.profile):
            with open(path) as source:
                self.assertEqual(json.load(source)['train']['projects']['ameesh']
                                 ['canon_commit'], self.gate)
        with open(os.path.join(self.root, 'train-canon.jsonl')) as source:
            row = json.loads(source.read())
        self.assertEqual(row['reviews'], result['reviews'])
        self.assertEqual(row['actor'], 'bureau')
        self.assertEqual(row['lot'], 4037)
        self.assertIn('milestone:1', self.note.call_args.args[2])
        self.assertIn('milestone:2', self.note.call_args.args[2])

    def test_no_opinion_never_authorizes_rule_changes(self):
        for content in ('reviews:\n  distinct_families: false\n',
                        'prevalidation:\n  classes: [P3]\n'):
            with self.subTest(content=content):
                self.repo.write('processus/mecanique.yaml', content)
                self.gate = self.repo.commit('unreviewed rules')
                with self.assertRaisesRegex(TrainError, 'requires --review'):
                    self.execute(reviews=[])
                self.assert_unchanged()

    def test_mechanical_path_still_refuses_guard(self):
        with self.assertRaisesRegex(TrainError, 'rules change'):
            self.execute(reviewed=False, reviews=[])
        self.assert_unchanged()

    def test_missing_pilot_and_missing_diff_review_are_distinct_refusals(self):
        for refs, why in ((['milestone:2'], 'pilot opinion'),
                          (['milestone:1'], 'diff reviews refused')):
            with self.subTest(refs=refs):
                with self.assertRaisesRegex(TrainError, why):
                    self.execute(reviews=refs)
                self.assert_unchanged()

    def test_duplicate_opinion_does_not_count_twice(self):
        with self.assertRaisesRegex(TrainError, 'diff reviews refused'):
            self.execute(reviews=['milestone:1', 'milestone:1'])
        self.assert_unchanged()

    def test_proposed_policy_cannot_lower_its_own_review_requirement(self):
        lower = types.SimpleNamespace(**vars(self.rules))
        lower.reviews_for = lambda paths: types.SimpleNamespace(reviews=1)
        with mock.patch.object(review.mechanics, 'load', side_effect=[self.rules, lower]):
            with self.assertRaisesRegex(TrainError, 'diff reviews refused'):
                self.execute(reviews=['milestone:1'])
        self.assert_unchanged()

    def test_arbitrary_references_and_review_without_reviewed_are_refused(self):
        for reference in ('https://example.invalid/review', 'milestone:0', 'pilot:ok'):
            with self.subTest(reference=reference):
                with self.assertRaisesRegex(TrainError, 'unsupported review reference'):
                    self.execute(reviews=[reference])
                self.assert_unchanged()
        with self.assertRaisesRegex(TrainError, '--review requires --reviewed'):
            self.execute(reviewed=False)
        self.assert_unchanged()

    def test_exact_head_provenance_and_independence_are_required(self):
        mutations = (
            lambda: self.receipts[2].update(commit=self.pin),
            lambda: self.receipts.pop(2),
            lambda: self.receipts[2].update(harness='deepseek'),
            lambda: self.commit_proof.configure_mock(return_value=None))
        for mutate in mutations:
            saved = {key: dict(value) for key, value in self.receipts.items()}
            with self.subTest(mutate=mutate):
                mutate()
                with self.assertRaises(TrainError):
                    self.execute()
                self.assert_unchanged()
            self.receipts.clear()
            self.receipts.update(saved)
            self.commit_proof.return_value = dict(actor='author', harness='deepseek')

    def test_canon_author_without_hook_uses_existing_historical_proof(self):
        self.commit_proof.return_value = None
        self.historical_authors.return_value = (
            [dict(name='author', family='deepseek', proof=True)], [])
        self.assertEqual(self.execute()['state'], 'requalified')
        self.assertFalse(self.historical_authors.call_args.kwargs['carry_receipts'])

    def test_source_lots_also_come_from_the_gate_receipts(self):
        """#4505: one gate over several PRs reads the lots of its commits.

        Both cited opinions were recorded on lot 4011, but the commit of the
        range was frozen on lot 4453, where its author is the reference. The
        fallback must read that lot from the gate's exact receipts; reading
        only the cited lot's origins leaves the author unknown.
        """
        self.commit_proof.return_value = None
        self.origins.return_value = [4453]
        self.items[4453] = {'id': 4453, 'assignee': 'author'}

        def authors(db, item, repo, sha, **kwargs):
            if int(item['id']) == 4453:
                return [dict(name='author', family='deepseek', proof=True)], []
            return [], [sha[:12]]
        self.historical_authors.side_effect = authors
        self.assertEqual(self.execute()['state'], 'requalified')
        self.assertIn(self.gate, self.origins.call_args.args[0])
        self.assertEqual(self.historical_authors.call_args.args[1]['id'], 4453)

    def test_uncited_newer_block_is_not_hidden(self):
        for actor in ('pilot', 'human:smichea'):
            with self.subTest(actor=actor):
                block = dict(self.marks[0], id=3, actor=actor, verdict='blocked', at_ts=3)
                self.marks.append(block)
                if actor == 'pilot':
                    self.receipts[3] = dict(self.receipts[1], milestone=block)
                else:
                    self.receipts.pop(3, None)
                with self.assertRaisesRegex(TrainError, 'blocking opinion'):
                    self.execute()
                self.assert_unchanged()
                self.marks.pop()

    def test_block_on_uncited_lot_still_refuses_without_writing(self):
        block = dict(self.marks[0], id=3, work_item_id=9999,
                     actor='human:smichea', verdict='blocked', at_ts=3)
        self.marks.append(block)
        # The old per-cited-lot read cannot see the opposition on lot 9999.
        with mock.patch.object(review.work, 'milestones',
                               return_value=self.marks[:2]):
            with self.assertRaisesRegex(TrainError, 'blocking opinion'):
                self.execute()
        self.assert_unchanged()
        self.note.assert_not_called()

    def test_contradictory_receipt_is_named_and_never_writes(self):
        for number in (1, 3):
            with self.subTest(number=number):
                if number == 3:
                    self.marks.append(dict(self.marks[0], id=3, at_ts=3))
                def receipt(db, ident):
                    if ident == number:
                        raise review.review_provenance.ProvenanceError(
                            'milestone:%s: proof actor differs' % ident)
                    return self.receipts.get(ident)
                with mock.patch.object(review.review_provenance,
                                       'receipt_for_milestone', side_effect=receipt):
                    with self.assertRaisesRegex(
                            TrainError, 'provenance refused: milestone:%s' % number):
                        self.execute()
                self.assert_unchanged()
                self.note.assert_not_called()

    def test_superseded_pilot_opinion_cannot_be_cited_as_current(self):
        self.marks.append(dict(self.marks[0], id=3, at_ts=3))
        self.receipts[3] = dict(self.receipts[1], milestone=self.marks[-1])
        with self.assertRaisesRegex(TrainError, 'pilot opinion'):
            self.execute()
        self.assert_unchanged()

    def test_opinion_family_comes_from_current_receipt_not_reference_order(self):
        self.receipts[1]['harness'] = 'codex'
        self.receipts[2]['harness'] = 'claude'
        self.marks.append(dict(self.marks[0], id=3, at_ts=3))
        self.receipts[3] = dict(self.receipts[1], harness='deepseek',
                                milestone=self.marks[-1])
        # L'ancien avis codex est cité en dernier ; il ne doit pas remplacer
        # la famille deepseek du nouvel avis (celle de l'auteur du canon).
        with self.assertRaisesRegex(TrainError, 'diff reviews refused'):
            self.execute(reviews=['milestone:3', 'milestone:2', 'milestone:1'])
        self.assert_unchanged()

    def test_unproven_block_does_not_revoke_a_proven_opinion(self):
        self.marks.append(dict(self.marks[0], id=3, verdict='blocked', at_ts=3))
        self.assertEqual(self.execute()['state'], 'requalified')

    def test_dry_run_validates_without_writing(self):
        self.assertTrue(self.execute(dry_run=True)['dry_run'])
        self.assert_unchanged()
        self.note.assert_not_called()

    def test_aligned_pin_still_checks_reviewed_arguments(self):
        self.gate = self.pin
        with self.assertRaisesRegex(TrainError, 'requires --review'):
            self.execute(reviews=[])
        for mark in self.marks:
            mark['sha'] = self.pin
            self.receipts[mark['id']]['commit'] = self.pin
        self.assertEqual(self.execute()['state'], 'aligned')
        self.assert_unchanged()
        self.note.assert_not_called()

    def set_gate(self, commit):
        self.gate = commit
        for mark in self.marks:
            mark['sha'] = commit
            self.receipts[mark['id']]['commit'] = commit

    def test_hosting_merge_with_parent_tree_needs_no_fictitious_author(self):
        head = self.gate
        self.repo.git('checkout', '-qb', 'hosting-merge', self.pin)
        self.repo.git('merge', '--no-ff', '-qm', 'hosting merge', head)
        self.set_gate(self.repo.git('rev-parse', 'HEAD'))
        self.commit_proof.side_effect = lambda db, sha: (
            None if sha == self.gate else dict(actor='author', harness='deepseek'))
        self.assertEqual(self.execute()['state'], 'requalified')

    def test_clean_merge_from_an_older_base_needs_no_fictitious_author(self):
        main = self.repo.git('rev-parse', '--abbrev-ref', 'HEAD')
        self.repo.git('checkout', '-qb', 'clean-pr', self.pin)
        self.repo.write('extra.md', 'reviewed content\n')
        self.repo.commit('reviewed branch')
        self.repo.git('checkout', '-q', main)
        self.repo.git('merge', '--no-ff', '-qm', 'Merge pull request #217', 'clean-pr')
        self.set_gate(self.repo.git('rev-parse', 'HEAD'))
        # The merge tree differs from both parents (the PR left from the pin,
        # main advanced): only git merge-tree proves it added no content.
        self.commit_proof.side_effect = lambda db, sha: (
            None if sha == self.gate else dict(actor='author', harness='deepseek'))
        self.assertEqual(self.execute()['state'], 'requalified')

    def test_conflict_resolution_merge_requires_its_own_author_provenance(self):
        main = self.repo.git('rev-parse', '--abbrev-ref', 'HEAD')
        self.repo.git('checkout', '-qb', 'conflicting-pr', self.pin)
        self.repo.write('processus/mecanique.yaml', 'schema: reviewed branch\n')
        self.repo.commit('conflicting branch')
        self.repo.git('checkout', '-q', main)
        conflict = subprocess.run(
            ['git', '-C', self.repo.root, 'merge', '--no-ff', '--no-commit',
             'conflicting-pr'], capture_output=True, text=True, env=self.repo.env)
        self.assertEqual(conflict.returncode, 1, conflict.stderr)
        self.repo.write('processus/mecanique.yaml', 'schema: resolved by hand\n')
        self.set_gate(self.repo.commit('resolve conflict'))
        self.commit_proof.side_effect = lambda db, sha: (
            None if sha == self.gate else dict(actor='author', harness='deepseek'))
        with self.assertRaisesRegex(TrainError, 'author provenance missing'):
            self.execute()
        self.assert_unchanged()

    def test_edited_clean_merge_still_needs_its_own_author_provenance(self):
        main = self.repo.git('rev-parse', '--abbrev-ref', 'HEAD')
        self.repo.git('checkout', '-qb', 'edited-pr', self.pin)
        self.repo.write('extra.md', 'reviewed content\n')
        self.repo.commit('reviewed branch')
        self.repo.git('checkout', '-q', main)
        self.repo.git('merge', '--no-ff', '--no-commit', 'edited-pr')
        self.repo.write('extra.md', 'content added during the merge\n')
        self.set_gate(self.repo.commit('edited merge'))
        self.commit_proof.side_effect = lambda db, sha: (
            None if sha == self.gate else dict(actor='author', harness='deepseek'))
        with self.assertRaisesRegex(TrainError, 'author provenance missing'):
            self.execute()
        self.assert_unchanged()

    def test_failed_lot_trace_rolls_back_journal_and_all_pins(self):
        self.note.side_effect = OSError('lot trace failed')
        with self.assertRaisesRegex(OSError, 'lot trace failed'):
            self.execute()
        self.assert_unchanged()

    def test_failed_journal_rolls_back_all_pins(self):
        with mock.patch.object(rq.os, 'fsync', side_effect=OSError('disk')):
            with self.assertRaisesRegex(OSError, 'disk'):
                self.execute()
        self.assert_unchanged()
        self.note.assert_not_called()


class ReviewedActorTest(unittest.TestCase):
    def test_unreadable_role_is_a_named_refusal(self):
        with mock.patch.object(identity, 'resolve_binding',
                               return_value=identity.Binding(name='bureau', ok=True)), \
                mock.patch.object(review.assignments, 'is_orchestrator',
                                  side_effect=RuntimeError('canon unreadable')):
            with self.assertRaisesRegex(TrainError, 'authority unreadable'):
                review.actor(None, None)

    def test_only_bound_pilot_or_desk_can_execute(self):
        for name, ok, role, admitted in (
                ('bureau', True, True, True), ('pilot', True, True, True),
                ('worker', True, False, False), ('bureau', False, True, False)):
            with self.subTest(name=name, ok=ok):
                with mock.patch.object(identity, 'resolve_binding',
                                       return_value=identity.Binding(name=name, ok=ok)), \
                        mock.patch.object(review.assignments, 'is_orchestrator',
                                          return_value=role) as authority:
                    if admitted:
                        self.assertEqual(review.actor(None, None), name)
                        self.assertFalse(authority.call_args.kwargs['history'])
                        self.assertTrue(authority.call_args.kwargs['strict'])
                    else:
                        with self.assertRaises(TrainError):
                            review.actor(None, None)

    def test_cli_passes_review_references_and_bound_actor(self):
        db = mock.Mock()
        with mock.patch.object(train_cli.config, 'load', return_value=None), \
                mock.patch.object(train_cli.db_mod, 'open_db', return_value=db), \
                mock.patch.object(review, 'actor', return_value='bureau'), \
                mock.patch.object(rq, 'execute', return_value={}) as execute, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(train_cli.main([
                '--project', 'nexlink', 'requalify', '--reviewed', '--lot', '4037',
                '--review', 'milestone:1', '--review', 'milestone:2', '--dry-run']), 0)
        self.assertEqual(execute.call_args.kwargs['reviews'], ['milestone:1', 'milestone:2'])
        self.assertEqual(execute.call_args.kwargs['actor'], 'bureau')
        self.assertTrue(execute.call_args.kwargs['reviewed'])
        db.close.assert_called_once_with()
