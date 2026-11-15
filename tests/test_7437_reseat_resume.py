# SPDX-License-Identifier: AGPL-3.0-only
"""#7437: a kept manual seat reports the portable pause deadline."""
import unittest
from unittest import mock

from ameesh import mail, registry
from ameesh import review_designation_activation as act


class ResumeMailTest(unittest.TestCase):
    def body(self, status, *, replacement=""):
        db = mock.Mock()
        with mock.patch.object(registry, "get", return_value={"status_text": status}), \
                mock.patch.object(mail, "send") as send:
            result = act._manual_reseat_alert(
                db, {"id": 7437, "title": "review"}, {"id": 123}, "pilot",
                [("reviewer", "status blocked", "claude", replacement,
                  "no claude reviewer available")], [])
        self.assertEqual(result, "")
        return send.call_args.args[3]

    def test_pace_pause_has_utc_deadline_and_estimate_label(self):
        body = self.body("pause (account claude2, seven_day 80 %) until 16:48@1794761280; pace cap 79%")
        self.assertIn("2026-11-15T16:48:00Z", body)
        self.assertIn("estimation (plafond de rythme)", body)
        self.assertIn("ameesh work reviewer 7437 <agent>", body)

    def test_fixed_pause_has_deadline_without_pace_label(self):
        body = self.body("pause (account claude2, five_hour 96 %) until 16:48@1794761280; hard cap")
        self.assertIn("2026-11-15T16:48:00Z", body)
        self.assertNotIn("estimation", body)

    def test_missing_deadline_does_not_invent_a_resume_time(self):
        self.assertNotIn("Reprise annoncée", self.body("blocked"))

    def test_replaced_seat_does_not_report_old_pause(self):
        self.assertNotIn("Reprise annoncée", self.body(
            "pause (account claude2) until 16:48@1794761280;", replacement="other"))
