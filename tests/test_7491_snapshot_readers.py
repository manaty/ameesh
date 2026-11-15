# SPDX-License-Identifier: AGPL-3.0-only
"""Recreated carriers must select their own exact snapshot (#7491)."""
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from ameesh import packet_snapshot, storage
from tests.test_1644_paquet_inclusion import inclusion
from tests.test_4304_paquet_poussee_precoce import paquet_auto as auto


class SnapshotIdentityTest(TestCase):
    def test_recreated_carrier_selects_its_receipt_and_unknown_identity_refuses(self):
        rows = [{"id": carrier, "target": target, "package_id": carrier,
                 "head_sha": "a" * 40, "base_sha": "b" * 40,
                 "payload": {"version": 1, "units": [], "manifest": manifest}}
                for target, carrier, manifest in (("main", 1, "old"),
                                                   ("main", 2, "current"),
                                                   ("develop", 2, "other target"))]
        st = mock.Mock()
        def select(**filters):
            return [row for row in rows
                    if all(filters.get(key) is None or filters[key] == row[key]
                           for key in ("target", "package_id"))]
        st.work.packet_snapshots.side_effect = select
        with mock.patch.object(storage, 'of', return_value=st), \
             mock.patch.object(packet_snapshot, 'repository_name', return_value='acme/app'), \
             mock.patch('ameesh.config.load'), \
             mock.patch('ameesh.db.connect', return_value=mock.Mock()), \
             mock.patch.object(auto, 'git', side_effect=AssertionError('no Git fallback')):
            self.assertEqual('current', auto.composition_text(
                Path('/unused'), 'a' * 40, 'x', target='main', carrier=2))
            with self.assertRaisesRegex(packet_snapshot.SnapshotError, 'ambiguous'):
                auto.composition_text(Path('/unused'), 'a' * 40, 'x', target='main')

    def test_inclusion_uses_explicit_identity_even_without_a_manifest_file(self):
        args = SimpleNamespace(repo='/unused', manifest='/unused/integration-paquet-x.md',
                               head='a' * 40, target='develop', carrier=2)
        # Stop immediately after reading the receipt, before unrelated inclusion checks.
        stop = RuntimeError('receipt parsed')
        with mock.patch.object(inclusion, 'rev_parse', return_value='a' * 40), \
             mock.patch.object(packet_snapshot, 'lookup', return_value={
                 'payload': {'manifest': 'current'}}) as lookup, \
             mock.patch.object(inclusion, 'parse_manifest', side_effect=stop) as parse:
            with self.assertRaisesRegex(RuntimeError, 'receipt parsed'):
                inclusion.run(args)
        self.assertEqual('current', parse.call_args.kwargs['text'])
        self.assertEqual('develop', lookup.call_args.kwargs['target'])
        self.assertEqual(2, lookup.call_args.kwargs['package_id'])
