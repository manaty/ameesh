# SPDX-License-Identifier: AGPL-3.0-only
"""Read historical notify keys using the existing acknowledgement identity."""
import hashlib
import unittest

from ameesh import alert_ack, notify, repair_signals as rs


class AlertKeyTest(unittest.TestCase):
    def adapt(self, key):
        return rs.from_alert({'key': key}, project='acme',
                             environment='staging', received_at=1794144000)

    def test_existing_hex_identity_is_unchanged(self):
        self.assertEqual(self.adapt('a' * 64).native_id, 'a' * 64)

    def test_notify_keys_equal_acknowledgement_keys(self):
        for alert in (
            {'type': 'ci_main_red', 'reason': 'acme/widget main'},
            {'type': 'ci_main_red', 'reason': 'échec de vérification'},
            {'type': 'decision_pending', 'agent': 'worker-1', 'lot': 42,
             'since': 1794144000, 'decision': 123},
        ):
            with self.subTest(alert=alert):
                key = notify.key_of(alert)
                self.assertEqual(self.adapt(key).native_id, alert_ack.key_of(alert))

    def test_other_text_keys_have_stable_private_identity(self):
        signal = self.adapt('source-key')
        self.assertEqual(signal.native_id,
                         hashlib.sha256(b'source-key').hexdigest())
        self.assertNotIn('source-key', str(signal.record()))

    def test_empty_or_missing_key_is_refused(self):
        for key in ('', ' ', None):
            with self.subTest(key=key), self.assertRaises(rs.SignalError):
                self.adapt(key)
