# SPDX-License-Identifier: AGPL-3.0-only
"""An incomplete shared sync explains its refusal without opening the gate."""
import types
import unittest
from unittest import mock

from ameesh import canon_sync


class IncompleteDiagnosticTest(unittest.TestCase):
    def test_pending_and_different_snapshots_report_each_pass_then_recover(self):
        canon = types.SimpleNamespace(
            id="team", root="/agent/canon", is_default=True,
            untrusted=False, readable=True, load_findings=[],
            sources=[types.SimpleNamespace(member="root", repo="/agent/canon",
                                           commit="abc", mode="git")])
        store = mock.Mock()
        store.claim_sync.return_value = False
        store.shared_sync.side_effect = [None, {"sync_signature": "other"}]
        messages = []
        with mock.patch.object(canon_sync, "state_key", return_value=""), \
                mock.patch.object(canon_sync, "configured_ref", return_value="main"), \
                mock.patch.object(canon_sync.storage, "of",
                                  return_value=types.SimpleNamespace(canon=store)), \
                mock.patch.object(canon_sync, "sync") as sync:
            for _ in range(2):
                self.assertIsNone(canon_sync.periodic_sync(
                    None, canon, "host", 300, on_incomplete=messages.append))
            sync.assert_not_called()
            self.assertEqual(len(messages), 2)
            self.assertIn("pending or failed", messages[0])
            self.assertIn("snapshot mismatch", messages[1])
            for message in messages:
                self.assertIn("team at /agent/canon", message)
                self.assertIn("root=/agent/canon@abc", message)
                self.assertIn("interval 300.0s", message)
            store.claim_sync.return_value = True
            report = canon_sync.periodic_sync(
                None, canon, "host", 300, on_incomplete=messages.append)
            self.assertIs(report, sync.return_value)
            self.assertEqual(len(messages), 2)
            store.complete_sync.assert_called_once()
