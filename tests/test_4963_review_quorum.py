# SPDX-License-Identifier: AGPL-3.0-only
"""#4963: unavailable designation does not erase seats or own the quorum."""
from unittest import mock

from ameesh import review_designation_activation as act, review_floor, storage, work
from ameesh.work_reviewers import designated
from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture


class UnavailableDesignationTest(_ActivationFixture, PgTestCase):
    def test_unavailable_designation_with_two_proven_families_validates(self):
        # Fail the selection only: the exact-head gate still proves authors
        # and verdict receipts independently of any designation mark.
        with mock.patch.object(act, '_selection', side_effect=RuntimeError('selection failed')):
            freeze, _ = self.freeze()
        review_floor.set_floor(self.cfg, self.db, self.lot, 2,
                               reason='two families', actor='human:smichea', complete=False)
        for actor, harness in (('r1', 'claude'), ('r2', 'codex')):
            # Persist receipt with the verdict before the validation hook.
            proof = {'sha': self.frozen, 'actor': actor, 'session': 's-' + harness, 'turn': 't-' + actor,
                     'harness': harness, 'work_item_id': self.lot}
            storage.of(self.db).work.add_milestone(
                self.lot, 'verdict', sha=self.frozen, actor=actor,
                verdict='ok', note='', proof=proof)
            if actor == 'r1':
                self.assertEqual(work.get(self.db, self.lot)['state'], 'qa')
        self.assertEqual(work.get(self.db, self.lot)['state'], 'validated')
        self.assertFalse(act._marks(self.db, self.lot, freeze_id=freeze['id']))

    def test_current_seats_survive_an_unavailable_effective_mark(self):
        freeze, _ = self.freeze()
        before = designated(work.get(self.db, self.lot))
        with mock.patch.object(act, '_effective_mark', return_value=None):
            act._sync_reviewers(self.db, work.get(self.db, self.lot), freeze, [])
        self.assertEqual(designated(work.get(self.db, self.lot)), before)
        self.assertFalse([n for n in self.notes() if 'cleared (previous head)' in n])

    def test_old_seat_cleanup_names_the_old_freeze(self):
        previous, _ = self.freeze()
        self.repo.write('src/ameesh/mail.py', 'new content\n')
        self.frozen = self.repo.commit('new head')
        self.receipts()
        # No new selection: only the previous freeze's seats are retired.
        current, _ = self.freeze(hook=lambda *a, **kw: None)
        act._sync_reviewers(self.db, work.get(self.db, self.lot), current, [])
        notes = [n for n in self.notes() if 'cleared' in n]
        self.assertTrue(notes)
        self.assertIn('freeze #%d cleared' % previous['id'], notes[-1])
        self.assertNotIn('freeze #%d cleared' % current['id'], notes[-1])

    def test_partial_clone_probe_has_no_remote_configuration(self):
        import subprocess
        from pathlib import Path
        from ameesh import review_authors
        self.repo.git('config', 'uploadpack.allowFilter', 'true')
        partial = Path(self.tmp) / 'partial'
        subprocess.run(['git', 'clone', '--quiet', '--filter=blob:none',
                        '--no-checkout', Path(self.repo.root).as_uri(), str(partial)],
                       check=True)
        git = review_authors.plan_git._git
        remotes_seen = []

        def observe(repo, *args, **kwargs):
            if args[0] == 'cat-file':
                remotes_seen.append(subprocess.check_output(
                    ['git', '-C', repo, 'remote']).decode().split())
            return git(repo, *args, **kwargs)

        with mock.patch.object(review_authors.plan_git, '_git', side_effect=observe):
            found = review_authors._present_commits(
                str(partial), [self.frozen, '1' * 40] * 500, timeout=5)
        self.assertEqual(found, {self.frozen: self.frozen})
        self.assertEqual(remotes_seen, [[]], 'presence probe inherited a remote')
