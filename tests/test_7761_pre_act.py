# SPDX-License-Identifier: AGPL-3.0-only
import unittest

from ameesh import pre_act


class Transaction:
    def __init__(self, events=(), messages=(), state="build"):
        self.events = events
        self.messages = messages
        self.state = state
        self.writes = []

    def query(self, sql, params):
        if "FROM work_items" in sql:
            return [{"state": self.state, "now_ts": 200}]
        if "FROM work_item_events" in sql:
            return self.events
        if "FROM agent_mailbox" in sql:
            return self.messages
        raise AssertionError(sql)

    def execute(self, sql, params):
        self.writes.append((sql, params))


class PreActTest(unittest.TestCase):
    def check(self, tx):
        return pre_act.inspect(tx, 5772, "bureau", 100, "kms")

    def test_stop_delivered_during_wait_refuses_without_receipt(self):
        tx = Transaction(messages=[{"id": 42, "recipient": "bureau", "unread": False,
                                    "body": "STOP séance #5772", "ts": 150}])
        with self.assertRaisesRegex(pre_act.Refused, "STOP"):
            self.check(tx)
        self.assertEqual(tx.writes, [])

    def test_clear_check_records_receipt(self):
        tx = Transaction(events=[{"id": 1, "note": "préparation", "ts": 90}])
        self.check(tx)
        self.assertIn("INSERT INTO work_item_events", tx.writes[0][0])
        self.assertIn("kind=kms actor=bureau", tx.writes[0][1][2])

    def test_unread_lot_mail_blocks_even_before_window(self):
        tx = Transaction(messages=[{"id": 4, "recipient": "bureau", "unread": True,
                                    "body": "nouvelle information", "ts": 10}])
        with self.assertRaisesRegex(pre_act.Refused, "unread"):
            self.check(tx)
        self.assertFalse(tx.writes)

    def test_old_active_hold_and_new_hold_both_block(self):
        for timestamp in (10, 150):
            with self.subTest(timestamp=timestamp):
                tx = Transaction(events=[{"id": 2, "note": "hold: owner", "ts": timestamp}])
                with self.assertRaises(pre_act.Refused):
                    self.check(tx)
                self.assertFalse(tx.writes)

    def test_withdrawn_decision_uses_withdrawal_time(self):
        message = {"id": 5, "recipient": "human:owner", "unread": False,
                   "body": "decision", "ts": 10, "decision_state": "withdrawn",
                   "withdrawn_ts": "150"}
        tx = Transaction(messages=[message])
        with self.assertRaisesRegex(pre_act.Refused, "withdrawn"):
            self.check(tx)
        self.assertFalse(tx.writes)
        message["withdrawn_ts"] = "NaN"
        with self.assertRaisesRegex(pre_act.Refused, "no readable timestamp"):
            self.check(Transaction(messages=[message]))

    def test_stop_in_lot_journal_during_wait_refuses(self):
        tx = Transaction(events=[{"id": 7, "note": "STOP séance", "ts": 150}])
        with self.assertRaisesRegex(pre_act.Refused, "STOP"):
            self.check(tx)
        self.assertFalse(tx.writes)

    def test_bad_window_and_blocked_lot_refused(self):
        with self.assertRaises(pre_act.Refused):
            pre_act.parse_since("2026-11-18T02:00:00")
        with self.assertRaises(pre_act.Refused):
            self.check(Transaction(state="blocked"))
        with self.assertRaises(pre_act.Refused):
            pre_act.inspect(Transaction(), 5772, "bureau", 201, "kms")
