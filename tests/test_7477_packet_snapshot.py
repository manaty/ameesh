# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from ameesh import packet_snapshot, storage, work
from ameesh.db import DbError
from .support import PgTestCase


class ExactSnapshotTest(unittest.TestCase):
    def receipt(self):
        return {"id": 1, "base_sha": "b" * 40,
                "payload": {"version": 1, "units": [], "manifest": "composition"}}

    def test_exact_filters_and_absent_head(self):
        st = Mock()
        st.work.packet_snapshots.return_value = []
        with patch.object(storage, 'of', return_value=st):
            self.assertIsNone(packet_snapshot.exact(object(), repository='acme/app',
                head='a'*40, branch='integration/paquet-x', target='origin/develop'))
            st.work.packet_snapshots.assert_called_once_with(repository='acme/app',
                target='develop', package_id=None, branch='integration/paquet-x', head_sha='a'*40)

    def test_invalid_or_ambiguous_never_falls_back(self):
        for rows in ([dict(self.receipt(), payload={})], [self.receipt(), self.receipt()]):
            st = Mock()
            st.work.packet_snapshots.return_value = rows
            fallback = Mock()
            with patch.object(storage, 'of', return_value=st):
                with self.assertRaises(packet_snapshot.SnapshotError):
                    packet_snapshot.text(db=object(), repository='acme/app', head='a'*40,
                        branch='integration/paquet-x', fallback=fallback)
            fallback.assert_not_called()

    def test_exact_receipt_and_legacy_fallback(self):
        st = Mock()
        for rows, expected in (([self.receipt()], 'composition'), ([], 'legacy')):
            st.work.packet_snapshots.return_value = rows
            with patch.object(storage, 'of', return_value=st):
                self.assertEqual(expected, packet_snapshot.text(db=object(),
                    repository='acme/app', head='a'*40, branch='integration/paquet-x',
                    fallback=lambda: 'legacy'))

    def test_short_head_is_not_a_receipt_key(self):
        with self.assertRaises(packet_snapshot.SnapshotError):
            packet_snapshot.exact(object(), repository='acme/app', head='a'*12, branch='x')

    def test_database_unavailable_is_explicit_and_does_not_read_git(self):
        from ameesh import db as database
        fallback = Mock()
        with patch.object(database, 'connect', side_effect=database.Unavailable('offline')):
            with self.assertRaisesRegex(packet_snapshot.SnapshotError, 'unavailable'):
                packet_snapshot.text(repository='acme/app', head='a'*40,
                    branch='integration/paquet-x', fallback=fallback)
        fallback.assert_not_called()


class SnapshotStorageTest(PgTestCase):
    def test_history_idempotence_target_isolation_and_lease_fence(self):
        st = storage.of(self.db).work
        carrier = work.add(self.db, title='packet receipt')
        epoch = st.land_lock('test-snapshot', 'composer', 300)
        args = dict(repository='acme/app', target='main', package_id=carrier['id'],
            branch='integration/paquet-x', head_sha='a'*40, base_sha='b'*40,
            payload={"version": 1, "units": [], "manifest": "first"}, actor='composer',
            lease_key='test-snapshot', lease_owner='composer', lease_epoch=epoch)
        first = st.record_packet_snapshot(**args)
        self.assertEqual(first['id'], st.record_packet_snapshot(**args)['id'])
        second = st.record_packet_snapshot(**dict(args, head_sha='c'*40))
        self.assertNotEqual(first['id'], second['id'])
        third = st.record_packet_snapshot(**dict(args, target='develop'))
        self.assertNotEqual(first['id'], third['id'])
        self.assertEqual(first['id'], packet_snapshot.exact(self.db, repository='acme/app',
            target='main', package_id=carrier['id'], branch=args['branch'], head=args['head_sha'])['id'])
        with self.assertRaises(DbError):
            st.record_packet_snapshot(**dict(args, payload={"changed": True}))
        with self.assertRaises(DbError):
            st.record_packet_snapshot(**dict(args, lease_epoch=epoch+1, head_sha='d'*40))
        self.assertEqual(3, len(st.packet_snapshots(repository='acme/app')))
        self.assertEqual([], st.packet_snapshots(repository='other/app'))
        st.land_unlock('test-snapshot', 'composer', epoch)
        with self.assertRaises(DbError):
            st.record_packet_snapshot(**dict(args, head_sha='e'*40))


class SnapshotReadersTest(unittest.TestCase):
    def setUp(self):
        self.receipt = {"id": 7, "head_sha": "a"*40, "base_sha": "b"*40,
            "payload": {"version": 1, "manifest": "receipt rendering",
                "units": [{"work_item_id": 12, "unit_sha": "c"*40}],
                "tests": {"status": "pending"}}}
        self.item = {"id": 3, "branch": "integration/paquet-x", "branch_target": "develop"}

    def test_red_uses_exact_historical_units_before_current_membership(self):
        from ameesh import packet_red
        with patch.object(packet_snapshot, 'lookup', return_value=self.receipt) as lookup:
            result = packet_red.composition(object(), self.item, 'a'*40, repo='acme/app')
        self.assertEqual('snapshot', result['source'])
        self.assertEqual([12], [unit['lot'] for unit in result['members']])
        self.assertEqual('develop', lookup.call_args.kwargs['target'])
        self.assertEqual(3, lookup.call_args.kwargs['package_id'])

    def test_ci_and_automerge_read_receipt_without_git_manifest(self):
        from ameesh import ci, packet_automerge
        with patch.object(packet_snapshot, 'lookup', return_value=self.receipt):
            self.assertEqual('packet-snapshot:7', ci._carrier_manifest(
                Mock(side_effect=AssertionError('GitHub fallback')), 'acme/app',
                self.item['branch'], 'a'*40, 'b'*40, db=object(), target='develop', carrier=3))
            self.assertEqual('receipt rendering', packet_automerge._carrier_manifest(
                '/unused', 'a'*40, self.item['branch'], db=object(), lot=3))

    def test_pending_receipt_never_proves_successful_local_tests(self):
        from ameesh import prevalidation_calls as calls
        with patch.object(packet_snapshot, 'lookup', return_value=self.receipt), \
             patch.object(calls, 'repository', return_value='/unused'):
            self.assertIsNone(calls.composer_tested(None, object(), self.item, 'a'*40))
            self.receipt['payload']['tests'] = {"status": "success", "count": 42}
            self.assertEqual(42, calls.composer_tested(None, object(), self.item, 'a'*40)['tests'])

    def test_shadow_reads_receipt_at_requested_head(self):
        from ameesh import train_shadow
        with patch.object(packet_snapshot, 'lookup', return_value=self.receipt) as lookup, \
             patch.object(train_shadow, 'parse_manifest', return_value=[12]):
            self.assertEqual([12], train_shadow._manifest_lots('/unused', 'a'*40, 'paquet-x'))
            self.assertEqual('a'*40, lookup.call_args.kwargs['head'])

    def test_release_uses_the_merge_carrier_parent(self):
        from ameesh import release_cutoff
        from types import SimpleNamespace
        log = ('d'*40 + '\x1fMerge pull request #21 from acme/integration/paquet-x'
               + '\x1fpacket\x1e')
        self.receipt['payload']['manifest'] = ('## Composition\n'
            '| unité | lot(s) | source | commit | note |\n|---|---|---|---|---|\n'
            '| 1 | #12 | `ccccccc` | `eeeeeee` | unit |\n')
        with patch.object(packet_snapshot, 'lookup', return_value=self.receipt) as lookup, \
             patch.object(release_cutoff, '_out', return_value=log), \
             patch.object(release_cutoff, '_git', return_value=SimpleNamespace(
                 returncode=0, stdout='a'*40)):
            packets, direct = release_cutoff.packets_between('/unused', 'before', 'after')
        self.assertEqual('a'*40, lookup.call_args.kwargs['head'])
        self.assertEqual([12], packets[0].units)
        self.assertEqual([], direct)

    def test_closure_only_nominates_receipts_landed_at_the_cutoff(self):
        from ameesh import packet_close
        from types import SimpleNamespace
        receipt = dict(self.receipt, repository='acme/app', target='develop',
                       package_id=3, branch=self.item['branch'])
        st = Mock()
        st.work.packet_snapshots.return_value = [receipt]
        with patch.object(packet_snapshot, 'repository_name', return_value='acme/app'), \
             patch.object(packet_snapshot, 'exact', return_value=receipt), \
             patch.object(storage, 'of', return_value=st), \
             patch.object(packet_close, 'manifest_paths', return_value=[]), \
             patch.object(packet_close.plan_git, '_git') as git:
            git.return_value = SimpleNamespace(returncode=1)
            self.assertEqual({}, packet_close.units('/unused', 'cutoff', db=object())[0])
            git.return_value = SimpleNamespace(returncode=0)
            self.assertEqual([12], list(packet_close.units('/unused', 'cutoff', db=object())[0]))
