# SPDX-License-Identifier: AGPL-3.0-only
"""Outgoing report contract: suppress quiet repeats without hiding action."""
import copy
import unittest
from unittest import mock

from ameesh import auditor_report as report


QUIET = {"debit_check": 0, "waits": [], "alerts": [],
         "idle_over_threshold": False}


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.db = object()
        self.journal = []
        self.store = mock.Mock()
        self.store.events.recent_type.side_effect = self.previous
        self.storage = self.enterContext(mock.patch.object(
            report.storage, "of", return_value=self.store))
        self.send = self.enterContext(mock.patch.object(
            report.mail, "send", return_value=42))
        self.record = self.enterContext(mock.patch.object(
            report.events, "record", side_effect=self.record_event))
        self.status = self.enterContext(mock.patch.object(
            report.registry, "set_status_text", return_value=True))

    def previous(self, event, project, limit, *, object):
        return [row for row in reversed(self.journal)
                if row["type"] == event and row["object"] == object][:limit]

    def record_event(self, db, event, key, **data):
        self.journal.append({"type": event, "object": key, "data": data})
        return len(self.journal)

    def publish(self, state=None, **kwargs):
        return report.publish(self.db, agent="auditor-a", recipient="design-a",
                              state=QUIET if state is None else state,
                              body="Audit: observations", status="audit terminé",
                              **kwargs)

    def test_first_report_then_quiet_passes_do_not_replace_sent_baseline(self):
        self.assertEqual(self.publish()["decision"], "sent")
        for _ in range(3):
            self.assertEqual(self.publish()["decision"], "skipped")
        self.send.assert_called_once()
        self.assertEqual([r["type"] for r in self.journal],
                         [report.EVENT] + [report.SKIPPED] * 3)
        self.assertEqual(self.status.call_count, 4)
        self.assertEqual(self.send.call_args.kwargs["payload"], {"info": True})

    def test_quiet_pass_uses_real_journal_validation_and_updates_status(self):
        # Exercise publish through the actual journal grammar, including the
        # quiet path; mocking record itself hid the invalid type in #7894.
        self.record.stop()
        self.store.events.record.side_effect = (
            lambda event, key, project, data:
            self.record_event(self.db, event, key, **data))
        self.assertEqual(self.publish()["decision"], "sent")
        self.assertEqual(self.publish()["decision"], "skipped")
        self.assertEqual(self.journal[-1]["type"], "auditor.report_skipped")
        self.send.assert_called_once()
        self.assertEqual(self.status.call_count, 2)

    def test_each_stable_change_is_reported_and_then_suppressed(self):
        self.publish()
        state = copy.deepcopy(QUIET)
        changes = [
            ("waits", [{"id": "lot:12", "pilot": "pilot-a", "gesture": "review"}]),
            ("waits", [{"id": "lot:12", "pilot": "pilot-b", "gesture": "review"}]),
            ("waits", [{"id": "lot:12", "pilot": "pilot-b", "gesture": "merge"}]),
            ("alerts", ["alert:8"]), ("alerts", []),
            ("idle_over_threshold", True), ("idle_over_threshold", False),
            ("waits", []),
        ]
        for key, value in changes:
            state[key] = value
            with self.subTest(key=key, value=value):
                self.assertEqual(self.publish(state)["reason"], "changed")
                self.assertEqual(self.publish(state)["decision"], "skipped")

    def test_action_activity_nonzero_debit_and_summaries_always_send(self):
        self.publish()
        cases = [({"activity": True}, QUIET, "activity"),
                 ({"needs_action": True}, QUIET, "action"),
                 ({"summary": "daily"}, QUIET, "summary"),
                 ({"summary": "weekly"}, QUIET, "summary"),
                 ({}, dict(QUIET, debit_check=2), "debit")]
        for kwargs, state, reason in cases:
            for _ in range(2):
                with self.subTest(reason=reason):
                    self.assertEqual(self.publish(state, **kwargs)["reason"], reason)
                    self.assertEqual(self.send.call_args.kwargs["payload"]["info"],
                                     not kwargs.get("needs_action", False))

    def test_order_and_duplicates_do_not_trigger_a_report(self):
        a = {"id": "lot:1", "pilot": "p", "gesture": "review"}
        b = dict(a, id="lot:2")
        first = dict(QUIET, waits=[a, b], alerts=["a", "b"])
        second = dict(QUIET, waits=[b, a, a], alerts=["b", "a", "a"])
        self.assertEqual(report.fingerprint(first), report.fingerprint(second))

    def test_failed_send_does_not_suppress_retry(self):
        self.send.side_effect = RuntimeError("unavailable")
        with self.assertRaises(RuntimeError):
            self.publish()
        self.record.assert_not_called()
        self.send.side_effect = None
        self.assertEqual(self.publish()["decision"], "sent")

    def test_missing_baseline_read_is_an_error_not_quiet(self):
        self.store.events.recent_type.side_effect = RuntimeError("unavailable")
        with self.assertRaises(RuntimeError):
            self.publish()
        self.send.assert_not_called()
        self.record.assert_not_called()

    def test_invalid_observations_cannot_silently_become_quiet(self):
        for state in (None, [], {}, dict(QUIET, debit_check=1),
                      dict(QUIET, debit_check=False), dict(QUIET, alerts="a"),
                      dict(QUIET, idle_over_threshold=0),
                      dict(QUIET, waits=[{"id": "a"}])):
            with self.subTest(state=state), self.assertRaises(ValueError):
                report.fingerprint(state)

    def test_recipient_has_its_own_comparison_baseline(self):
        self.publish()
        result = report.publish(self.db, agent="auditor-a", recipient="design-b",
                                state=QUIET, body="audit", status="done")
        self.assertEqual(result["decision"], "sent")

if __name__ == "__main__":
    unittest.main()
