# SPDX-License-Identifier: AGPL-3.0-only
"""#7480: non-blocking defects found while reviewing #7477."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ameesh import (packet_red, packet_snapshot, release_cutoff, storage,
                    train_shadow)
from ameesh.db import DbError

MANIFEST = "## Composition\n" + (
    "| unité | lot(s) | source | commit | note |\n"
    "|---|---|---|---|---|\n"
    "| 1 | #12 | `ccccccc` | `eeeeeee` | unit |\n")
PARALLEL = ("- Tests locaux : joués par le composeur après la poussée, en "
            "parallèle de la CI complète (#4304) ; résultat à la PR, au porteur "
            "et au courrier du pilote.")


class ParallelTests(unittest.TestCase):
    def test_a_parallel_manifest_is_not_pending(self):
        payload = packet_snapshot.payload(None, [], "## Composition\n\n" + PARALLEL + "\n")
        self.assertEqual({'status': 'parallel'}, payload['tests'])

    def test_a_parallel_receipt_keeps_the_composer_exemption(self):
        from ameesh import prevalidation_calls as calls
        receipt = {"id": 7, "head_sha": "a"*40, "payload": {
            "version": 1, "units": [], "manifest": "", "tests": {"status": "parallel"}}}
        item = {"id": 3, "branch": "integration/paquet-x", "branch_target": "develop"}
        with patch.object(packet_snapshot, 'lookup', return_value=receipt), \
             patch.object(calls, 'repository', return_value='/unused'):
            self.assertEqual(
                {"manifest": "packet-snapshot:7", "sha": "a"*40,
                 "tests": 0, "parallel": True},
                calls.composer_tested(None, object(), item, "a"*40))


class DegradationTests(unittest.TestCase):
    def test_an_unreadable_receipt_leaves_prevalidation_due(self):
        from ameesh import prevalidation_calls as calls
        item = {"id": 3, "branch": "integration/paquet-x", "branch_target": "develop"}
        for error in (packet_snapshot.SnapshotError("offline"), DbError("offline")):
            with self.subTest(error=type(error).__name__), \
                 patch.object(packet_snapshot, 'lookup', side_effect=error), \
                 patch.object(calls, 'repository', return_value='/unused'):
                self.assertIsNone(calls.composer_tested(None, object(), item, "a"*40))

    def test_attribution_never_raises_on_a_snapshot_failure(self):
        item = {"id": 3, "branch": "integration/paquet-x"}
        run = {"head_sha": "a"*40}
        with patch.object(packet_red, 'composition',
                          side_effect=packet_snapshot.SnapshotError("offline")), \
             patch.object(packet_red, 'checkout_for', return_value='/unused'), \
             patch.object(packet_red.work, 'lot_repository', side_effect=packet_red.work.WorkError("x")):
            out = packet_red._attribute_run(object(), item, run, [{"test": "t"}])
        self.assertEqual('error', out['status'])
        self.assertIn('SnapshotError', out['why'])


class RepositoryKeyTests(unittest.TestCase):
    def test_reads_use_the_same_lowercase_key_as_writes(self):
        from ameesh import plan_git
        with patch.object(plan_git, 'github_slug', return_value='Acme/App'):
            self.assertEqual('acme/app', packet_snapshot.repository_name('/checkout'))
        st = Mock()
        st.work.packet_snapshots.return_value = []
        with patch.object(storage, 'of', return_value=st):
            self.assertIsNone(packet_snapshot.exact(
                object(), repository='Acme/App', head='a'*40, branch='x'))
        st.work.packet_snapshots.assert_called_once_with(
            repository='acme/app', target=None, package_id=None, branch='x',
            head_sha='a'*40)


class ReaderTargetAndCostTests(unittest.TestCase):
    def receipt(self):
        return {"id": 7, "head_sha": "a"*40, "base_sha": "b"*40, "payload": {
            "version": 1, "units": [{"work_item_id": 12, "unit_sha": "c"*40}],
            "manifest": MANIFEST, "tests": {"status": "pending"}}}

    def test_release_reuses_the_caller_connection_and_passes_target(self):
        log = ('d'*40 + '\x1fMerge pull request #21 from acme/integration/paquet-x'
               + '\x1fpacket\x1e')
        db = object()
        with patch.object(packet_snapshot, 'lookup', return_value=self.receipt()) as lookup, \
             patch.object(release_cutoff, '_out', return_value=log), \
             patch.object(release_cutoff, '_git', return_value=SimpleNamespace(
                 returncode=0, stdout='a'*40)):
            packets, direct = release_cutoff.packets_between(
                '/unused', 'before', 'after', db=db, target_branch='develop')
        lookup.assert_called_once_with(db=db, checkout='/unused', head='a'*40,
                                       branch='integration/paquet-x', target='develop')
        self.assertEqual([12], packets[0].units)
        self.assertEqual([], direct)

    def test_shadow_passes_the_target_to_the_snapshot_reader(self):
        with patch.object(packet_snapshot, 'lookup',
                          return_value=self.receipt()) as lookup, \
             patch.object(train_shadow, 'parse_manifest', return_value=[12]):
            self.assertEqual([12], train_shadow._manifest_lots(
                '/unused', 'a'*40, 'paquet-x', target='main'))
        lookup.assert_called_once_with(checkout='/unused', head='a'*40,
                                       branch='integration/paquet-x', target='main')


if __name__ == "__main__":
    unittest.main()
