# SPDX-License-Identifier: AGPL-3.0-only
"""#4430: packet coverage uses active reservations and added SQL files only."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from ameesh import migration_reservations, packet_candidates
from .test_l122_numeros_de_migration import _commit, _depot, _git, NEXLINK


class MigrationCoverageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = _depot(os.path.join(self.tmp.name, 'repo'), 'manaty/nexlink',
                           NEXLINK.directory, ['376_base.sql'])
        self.base = migration_reservations._git(self.repo, 'rev-parse', 'HEAD')
        _commit(self.repo, NEXLINK.directory, '377_new.sql')
        self.entry = dict(work_item=42, ready=True, causes=[],
                          source=migration_reservations._git(self.repo, 'rev-parse', 'HEAD'),
                          files=[NEXLINK.directory + '/377_new.sql'])

    def check(self):
        packet_candidates._check_migration_coverage(
            None, self.entry, repo=self.repo, origin_repo=self.repo,
            base=self.base, target='develop')

    def test_missing_reservation_blocks_with_named_reason(self):
        with mock.patch.object(migration_reservations, 'uncovered', return_value=[377]) as read:
            self.check()
        self.assertFalse(self.entry['ready'])
        cause = self.entry['causes'][0]
        self.assertEqual(cause['code'], 'migration-collision')
        self.assertIn('377', str(cause))
        self.assertIn('active reservation', str(cause))
        read.assert_called_once_with(None, 42, {377}, repository='manaty/nexlink',
                                     target_branch='develop')

    def test_active_reservation_leaves_candidate_ready(self):
        with mock.patch.object(migration_reservations, 'uncovered', return_value=[]):
            self.check()
        self.assertTrue(self.entry['ready'])
        self.assertEqual(self.entry['causes'], [])

    def test_registry_error_is_not_read_as_coverage(self):
        with mock.patch.object(migration_reservations, 'uncovered', side_effect=RuntimeError('offline')):
            with self.assertRaisesRegex(packet_candidates.CandidateError, 'active migration reservations'):
                self.check()

    def test_modified_historical_migration_needs_no_new_reservation(self):
        path = NEXLINK.directory + '/376_base.sql'
        with open(os.path.join(self.repo, path), 'a') as handle:
            handle.write('-- modified\n')
        _git(self.repo, 'add', '.')
        _git(self.repo, 'commit', '-qm', 'modify historical migration')
        self.entry.update(source=migration_reservations._git(self.repo, 'rev-parse', 'HEAD'), files=[path])
        with mock.patch.object(migration_reservations, 'uncovered') as read:
            self.check()
        read.assert_not_called()
        self.assertTrue(self.entry['ready'])

    def test_no_migration_does_not_read_registry(self):
        self.entry['files'] = ['README.md']
        with mock.patch.object(migration_reservations, 'uncovered') as read:
            self.check()
        read.assert_not_called()
        self.assertTrue(self.entry['ready'])
