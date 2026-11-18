# SPDX-License-Identifier: AGPL-3.0-only
import unittest
import json
from contextlib import nullcontext
from unittest import mock

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
            return [dict({"work_item_id": 5772, "state": "build", "created_ts": e.get("ts", 0)}, **e)
                    for e in self.events]
        if "FROM agent_mailbox" in sql:
            return self.messages
        raise AssertionError(sql)

    def execute(self, sql, params):
        self.writes.append((sql, params))


class PreActTest(unittest.TestCase):
    def check(self, tx):
        with mock.patch.object(pre_act.planned_waits, "lift_authority", return_value=lambda who: False):
            return pre_act.inspect(tx, 5772, "bureau", 100, "kms")

    def test_stop_delivered_during_wait_refuses_without_receipt(self):
        tx = Transaction(messages=[{"id": 42, "recipient": "bureau", "unread": False,
                                    "body": "STOP séance #5772", "ts": 150}])
        with self.assertRaisesRegex(pre_act.Refused, "STOP"):
            self.check(tx)
        self.assertEqual(tx.writes, [])

    def test_ordinary_hold_and_actor_notes_do_not_block(self):
        tx = Transaction(events=[
            {"id": 1, "actor": "orchestrateur", "note": "hold: on attend ses reçus", "ts": 150},
            {"id": 2, "actor": "bureau", "note": "Arrêt au premier échec", "ts": 151},
        ], messages=[{"id": 3, "sender": "bureau", "recipient": "bureau", "unread": True,
                      "body": "STOP #5772", "ts": 152}])
        self.check(tx)
        self.assertTrue(tx.writes)

    def test_wait_until_detects_stop_before_executing(self):
        tx = Transaction(events=[{"id": 2, "actor": "owner", "note": "STOP", "ts": 150}])
        db = mock.Mock()
        db.transaction.return_value = nullcontext(tx)
        binding = mock.Mock(ok=True, name="bureau")
        binding.name = "bureau"
        with mock.patch.object(pre_act.time, "time", side_effect=[100, 100, 160]), \
             mock.patch.object(pre_act.time, "sleep") as sleep, \
             mock.patch.object(pre_act.config, "load", return_value=object()), \
             mock.patch.object(pre_act.db_mod, "open_db", return_value=db), \
             mock.patch.object(pre_act.storage, "is_remote", return_value=False), \
             mock.patch.object(pre_act.identity, "resolve_binding", return_value=binding), \
             mock.patch.object(pre_act.os, "execvp") as execute:
            result = pre_act.main(["--lot", "5772", "--kind", "kms", "--wait-until",
                                   "1970-01-01T00:02:40Z", "--", "true"])
        self.assertEqual(result, 1)
        sleep.assert_called_once_with(60)
        execute.assert_not_called()
        self.assertFalse(tx.writes)

    def test_wait_until_clear_executes_after_wait(self):
        tx = Transaction()
        db = mock.Mock()
        db.transaction.return_value = nullcontext(tx)
        binding = mock.Mock(ok=True)
        binding.name = "bureau"
        with mock.patch.object(pre_act.time, "time", side_effect=[100, 100, 160]), \
             mock.patch.object(pre_act.time, "sleep") as sleep, \
             mock.patch.object(pre_act.config, "load", return_value=object()), \
             mock.patch.object(pre_act.db_mod, "open_db", return_value=db), \
             mock.patch.object(pre_act.storage, "is_remote", return_value=False), \
             mock.patch.object(pre_act.identity, "resolve_binding", return_value=binding), \
             mock.patch.object(pre_act.os, "execvp") as execute:
            result = pre_act.main(["--lot", "5772", "--kind", "kms", "--wait-until",
                                   "1970-01-01T00:02:40Z", "--", "true"])
        self.assertEqual(result, 0)
        sleep.assert_called_once_with(60)
        execute.assert_called_once_with("true", ["true"])
        self.assertIn("since=100", tx.writes[0][1][2])

    def test_unhold_only_clears_its_author(self):
        tx = Transaction(events=[
            {"id": 1, "actor": "other", "note": "hold: STOP A", "ts": 10},
            {"id": 2, "actor": "orchestrateur", "note": "hold: STOP B", "ts": 20},
            {"id": 3, "actor": "other", "note": "unhold: A levé", "ts": 30},
        ])
        with self.assertRaisesRegex(pre_act.Refused, "orchestrateur"):
            self.check(tx)
        self.assertFalse(tx.writes)

    def test_state_change_clears_ordinary_stop_hold(self):
        tx = Transaction(events=[
            {"id": 1, "actor": "other", "state": "intake", "note": "hold: STOP A", "ts": 10},
            {"id": 2, "actor": "other", "state": "build", "note": "reprise", "ts": 20},
        ])
        self.check(tx)
        self.assertTrue(tx.writes)

    def test_actor_transition_does_not_clear_another_authors_stop(self):
        for author in ("orchestrateur", "other"):
            with self.subTest(author=author):
                tx = Transaction(events=[
                    {"id": 1, "actor": author, "state": "intake", "note": "hold: STOP", "ts": 10},
                    {"id": 2, "actor": "bureau", "state": "build", "note": "claim", "ts": 20},
                ])
                with self.assertRaisesRegex(pre_act.Refused, "active STOP"):
                    self.check(tx)
                self.assertFalse(tx.writes)

    def test_clear_check_records_receipt(self):
        tx = Transaction(events=[{"id": 1, "note": "préparation", "ts": 90}])
        receipt_id = self.check(tx)
        self.assertIn("INSERT INTO work_item_events", tx.writes[0][0])
        self.assertIn("kind=kms actor=bureau", tx.writes[0][1][2])
        evidence = json.loads(tx.writes[0][1][4])
        self.assertEqual(evidence, {"type": "pre-act/1", "kind": "kms",
                                    "lot": 5772, "actor": "bureau", "target": "",
                                    "pregeste_id": receipt_id})

    def test_prod_dispatch_carries_the_recorded_id(self):
        tx = Transaction()
        db = mock.Mock()
        db.transaction.return_value = nullcontext(tx)
        binding = mock.Mock(ok=True)
        binding.name = "bureau"
        with mock.patch.object(pre_act.config, "load", return_value=object()), \
             mock.patch.object(pre_act.db_mod, "open_db", return_value=db), \
             mock.patch.object(pre_act.storage, "is_remote", return_value=False), \
             mock.patch.object(pre_act.identity, "resolve_binding", return_value=binding), \
             mock.patch.object(pre_act.os, "execvp") as execute:
            result = pre_act.main(["--lot", "5772", "--kind", "prod", "--target", "nexlink",
                                   "--since", "1970-01-01T00:01:40Z", "--",
                                   "gh", "workflow", "run", "deploy.yml"])
        self.assertEqual(result, 0)
        receipt_id = json.loads(tx.writes[0][1][4])["pregeste_id"]
        execute.assert_called_once_with("gh", ["gh", "workflow", "run", "deploy.yml",
                                               "-f", "pregeste=" + receipt_id])

    def test_prod_requires_target_before_database_access(self):
        with mock.patch.object(pre_act.db_mod, "open_db") as open_db:
            result = pre_act.main(["--lot", "5772", "--kind", "prod", "--since",
                                   "2026-11-18T01:00:00Z", "--", "true"])
        self.assertEqual(result, 1)
        open_db.assert_not_called()

    def test_unread_lot_mail_blocks_even_before_window(self):
        tx = Transaction(messages=[{"id": 4, "recipient": "bureau", "unread": True,
                                    "body": "nouvelle information", "ts": 10}])
        with self.assertRaisesRegex(pre_act.Refused, "unread"):
            self.check(tx)
        self.assertFalse(tx.writes)

    def test_old_active_hold_and_new_hold_both_block(self):
        for timestamp in (10, 150):
            with self.subTest(timestamp=timestamp):
                tx = Transaction(events=[{"id": 2, "note": "hold: STOP owner", "ts": timestamp}])
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

    def test_unavailable_database_refuses(self):
        with mock.patch.object(pre_act.config, "load", return_value=object()), \
             mock.patch.object(pre_act.db_mod, "open_db", side_effect=OSError("offline")):
            result = pre_act.main(["--lot", "5772", "--kind", "kms", "--since",
                                   "2026-11-18T01:00:00Z"])
        self.assertEqual(result, 1)

    def test_hold_on_act_lot_5772_refuses(self):
        tx = Transaction(events=[{"id": 8, "note": "hold: séance suspendue", "ts": 150}])
        with self.assertRaises(pre_act.Refused):
            pre_act.inspect(tx, 5772, "bureau", 100, "kms")
        self.assertFalse(tx.writes)

    def test_guard_executes_only_after_receipt_and_closes_connection(self):
        tx = Transaction()
        db = mock.Mock()
        db.transaction.return_value = nullcontext(tx)
        binding = mock.Mock(ok=True, name="bureau")
        binding.name = "bureau"
        with mock.patch.object(pre_act.config, "load", return_value=object()), \
             mock.patch.object(pre_act.db_mod, "open_db", return_value=db), \
             mock.patch.object(pre_act.storage, "is_remote", return_value=False), \
             mock.patch.object(pre_act.identity, "resolve_binding", return_value=binding), \
             mock.patch.object(pre_act.os, "execvp") as execute:
            self.assertEqual(pre_act.main(["--lot", "5772", "--kind", "kms", "--since",
                                           "1970-01-01T00:01:40Z", "--", "true"]), 0)
        self.assertTrue(tx.writes)
        db.close.assert_called_once()
        execute.assert_called_once_with("true", ["true"])

    def test_refusal_never_executes_command(self):
        with mock.patch.object(pre_act.config, "load", return_value=object()), \
             mock.patch.object(pre_act.db_mod, "open_db", side_effect=OSError("offline")), \
             mock.patch.object(pre_act.os, "execvp") as execute:
            result = pre_act.main(["--lot", "5772", "--kind", "kms", "--since",
                                   "2026-11-18T01:00:00Z", "--", "true"])
        self.assertEqual(result, 1)
        execute.assert_not_called()

    def test_stale_receipt_never_executes_command(self):
        db = mock.Mock()
        db.transaction.return_value = nullcontext(Transaction())
        binding = mock.Mock(ok=True)
        binding.name = "bureau"
        with mock.patch.object(pre_act.config, "load", return_value=object()), \
             mock.patch.object(pre_act.db_mod, "open_db", return_value=db), \
             mock.patch.object(pre_act.storage, "is_remote", return_value=False), \
             mock.patch.object(pre_act.identity, "resolve_binding", return_value=binding), \
             mock.patch.object(pre_act.time, "monotonic", side_effect=[100, 131]), \
             mock.patch.object(pre_act.os, "execvp") as execute:
            result = pre_act.main(["--lot", "5772", "--kind", "kms", "--since",
                                   "1970-01-01T00:01:40Z", "--", "true"])
        self.assertEqual(result, 1)
        execute.assert_not_called()
