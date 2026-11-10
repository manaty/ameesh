# SPDX-License-Identifier: AGPL-3.0-only
"""#6605: lot target, packet evidence and delta archives (isolated Git)."""
import json
import os
from types import SimpleNamespace
from unittest import mock

from ameesh import housekeeping as hk, housekeeping_archive as ha
from .test_l230_build_artifacts import _Repo, _Store, _git, _write, DAY
from .test_3728_menage_archive import _ArchiveRepo, _touch, RETENTION


class TargetTest(_Repo):
    def test_develop_instead_of_default_main(self):
        wt = self.add_worktree('lot-7')
        _git('push', '-q', 'origin', 'HEAD:develop', cwd=wt)
        self.assertFalse(hk.worktree_state(wt)['integrated'])
        self.assertTrue(hk.worktree_state(wt, target='develop')['integrated'])
        # A missing explicit target cannot fall back to a default containing HEAD.
        _git('push', '-q', 'origin', 'HEAD:main', cwd=wt)
        self.assertFalse(hk.worktree_state(wt, target='missing')['integrated'])

    def test_packet_evidence_only_covers_frozen_head(self):
        wt = self.add_worktree('lot-7')
        head = _git('rev-parse', 'HEAD', cwd=wt).strip()
        self.assertTrue(hk.worktree_state(wt, integrated_head=head)['integrated'])
        _write(os.path.join(wt, 'later.txt'), text='later work\n')
        _git('add', '.', cwd=wt)
        _git('commit', '-qm', 'later', cwd=wt)
        self.assertFalse(hk.worktree_state(wt, integrated_head=head)['integrated'])

    def test_packet_removed_after_grace_releases_cache_link(self):
        wt = self.add_worktree('lot-7')
        head = _git('rev-parse', 'HEAD', cwd=wt).strip()
        cache = os.path.join(self.base, 'cache', 'nm')
        _write(os.path.join(cache, 'keep.js'), text='shared\n')
        os.symlink(cache, os.path.join(wt, 'node_modules'))
        # A rewritten packet lands different content on develop (e.g. renumbered migration).
        _git('checkout', '-qb', 'develop', cwd=self.main)
        _write(os.path.join(self.main, 'packet.txt'), text='rewritten packet\n')
        _git('add', '.', cwd=self.main)
        _git('commit', '-qm', 'packet', cwd=self.main)
        _git('push', '-q', 'origin', 'develop', cwd=self.main)
        self.assertFalse(hk.worktree_state(wt, target='develop')['integrated'])
        now = 2_000_000_000.0
        _touch(wt, now - 2 * DAY)
        row = dict(id=1, path=wt, repo=hk.common_dir(self.main), agent='dev2', lot='7',
                   status='kept', created_ts=now - 3 * DAY, checked_ts=now - 2 * DAY,
                   detail='previously kept')
        store = _Store([row], {7: 'merged'})
        store.work.get = lambda ident: dict(state='merged', branch='lot-7',
                                            branch_target='develop', frozen_sha=head, closed_ts=now - 2 * DAY)
        store.work.milestones = lambda ident, limit: [dict(kind='merged', sha='rewritten')]
        with mock.patch.object(hk.storage, 'of', return_value=store), \
                mock.patch.object(hk, 'worktree_holders', return_value=(False, '')):
            entries, plan = hk._worktree_pass(None, 'pc', protected=set(), now=now, dry_run=False,
                archive=dict(after=DAY, retention=RETENTION, root=os.path.join(self.base, 'archives')),
                lock_path=os.path.join(self.base, 'state', 'lock'),
                journal_path=self.journal_path)
        self.assertEqual(plan[0]['action'], 'retrait', plan)
        self.assertFalse(os.path.lexists(wt))
        self.assertTrue(os.path.isfile(os.path.join(cache, 'keep.js')))
        self.assertEqual(store.statuses[0][1], 'removed')

    def test_unknown_target_grants_no_git_proof(self):
        wt = self.add_worktree('lot-7')
        _git('push', '-q', 'origin', 'HEAD:main', cwd=wt)
        self.assertFalse(hk.worktree_state(wt, target=None)['integrated'])

    def test_unproved_or_annulled_delivery_does_not_grant_integration(self):
        wt = self.add_worktree('lot-7')
        item = dict(state='merged', branch='lot-7', branch_target='develop', frozen_sha='abc')
        store = SimpleNamespace(work=SimpleNamespace(get=lambda ident: item))
        with mock.patch.object(hk.storage, 'of', return_value=store):
            self.assertEqual(hk._worktree_lot_evidence(None, ['7'], 'lot-7', wt),
                             ('develop', ''))
            store.work.milestones = lambda ident, limit: [dict(kind='merged', annulled=True)]
            self.assertEqual(hk._worktree_lot_evidence(None, ['7'], 'lot-7', wt),
                             ('develop', ''))
            item['frozen_at'] = 200
            store.work.milestones = lambda ident, limit: [dict(kind='merged', at_ts=100)]
            self.assertEqual(hk._worktree_lot_evidence(None, ['7'], 'lot-7', wt),
                             ('develop', ''))
            store.work.milestones = lambda ident, limit: [dict(kind='merged', at_ts=300)]
            item['build_at'] = 400
            self.assertEqual(hk._worktree_lot_evidence(None, ['7'], 'lot-7', wt),
                             ('develop', ''))
            self.assertEqual(hk._worktree_lot_evidence(None, ['7'], 'other', wt),
                             ('develop', ''))


class LargeArchiveTest(_ArchiveRepo):
    def test_default_accepts_large_delta_footprint(self):
        wt = self.add_worktree('lot-7')
        # Exercise a >512 MiB delta footprint without filling the host disk.
        _write(os.path.join(wt, 'note.txt'), text='retain me\n')
        head = _git('rev-parse', 'HEAD', cwd=wt).strip()
        with mock.patch.object(ha, '_size', return_value=513 * 1024 ** 2):
            result = ha.archive(wt, self.archives, head=head, lot=7, now=2_000_000_000,
                                retention_s=RETENTION)
        with open(os.path.join(result['archive'], 'manifest.json')) as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest['untracked'], ['note.txt'])
        self.assertEqual(manifest['untracked_bytes'], 513 * 1024 ** 2)
        self.assertLess(result['bytes'], 1024 ** 2)
