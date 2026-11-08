# SPDX-License-Identifier: AGPL-3.0-only
"""Read historical notify keys without changing the writer's identity."""
import hashlib
import unittest

from ameesh import notify, repair_signals as rs


class AlertKeyTest(unittest.TestCase):
    def adapt(self, key):
        return rs.from_alert({'key': key}, project='acme',
                             environment='staging', received_at=1794144000)

    def test_existing_hex_identity_is_unchanged(self):
        self.assertEqual(self.adapt('a' * 64).native_id, 'a' * 64)

    def test_real_notify_array_key(self):
        key = notify.key_of({'type': 'ci_main_red', 'reason': 'acme/widget main'})
        expected = hashlib.sha256(b'["ci_main_red","acme/widget main"]').hexdigest()
        self.assertEqual(self.adapt(key).native_id, expected)

    def test_whitespace_and_nested_object_order_do_not_change_identity(self):
        first = '["probe", {"z": {"b": 2, "a": "é"}, "a": 1}]'
        second = '["probe",{"a":1,"z":{"a":"\\u00e9","b":2}}]'
        self.assertEqual(self.adapt(first), self.adapt(second))

    def test_other_text_keys_have_stable_private_identity(self):
        signal = self.adapt('source-key')
        self.assertEqual(signal.native_id,
                         hashlib.sha256(b'"source-key"').hexdigest())
        self.assertNotIn('source-key', str(signal.record()))

    def test_non_finite_json_key_is_refused(self):
        with self.assertRaises(rs.SignalError):
            self.adapt('[NaN]')
