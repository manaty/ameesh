# SPDX-License-Identifier: AGPL-3.0-only
"""#7309: comma waits must be anchored to the hold header."""
import unittest

from ameesh import planned_waits, work
from .support import PgTestCase
from .test_3437_debit_dependance import H, T
from .test_3498_hold_externe import event, item, run


class ExternalCommaTest(unittest.TestCase):
    def test_comma_declares_executor_gesture_and_due(self):
        for who in ("owner", "bureau"):
            with self.subTest(who=who):
                text = (f"hold: attente {who}, signer le go "
                        "(due 2026-10-25 12:00 UTC)")
                self.assertEqual(planned_waits.external_hold(text), {
                    "executor": who, "gesture": "signer le go",
                    "due": "2026-10-25 12:00 UTC"})
                self.assertEqual(planned_waits.external_hold_warning(text), "")

    def test_comma_in_prose_does_not_declare_external_wait(self):
        for text in ("hold: rebase, en attente du bureau",
                     "hold: rebase, en attente du bureau, signer",
                     "hold: rebase; attente owner, signer",
                     "attente owner, signer", "hold: attente owner, "):
            with self.subTest(text=text):
                self.assertIsNone(planned_waits.external_hold(text))

    def test_malformed_leading_wait_warns(self):
        for text in ("hold: attente owner signer le go",
                     "hold: attente bureau", "hold: attente owner,",
                     "hold: attente owner :"):
            with self.subTest(text=text):
                self.assertIn("external wait not recognized",
                              planned_waits.external_hold_warning(text))

    def test_valid_separators_and_ordinary_prose_do_not_warn(self):
        for text in ("hold: attente bureau : signer", "hold: attente owner — signer",
                     "hold: rebase, en attente du bureau", "unhold: attente owner",
                     "note: attente owner", "hold: attente ownership", None):
            with self.subTest(text=text):
                self.assertEqual(planned_waits.external_hold_warning(text), "")

    def test_old_comma_hold_is_external_in_debit(self):
        report = run(items=[item(7003)], events=[event(
            7003, 1, T - 11 * H, "hold: attente owner, signer le go")])
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual(report["totals"]["external_waits"], 1)
        self.assertEqual(report["external_waits"][0]["who"], "owner")


class ExternalWaitWriteTest(PgTestCase):
    def test_note_is_saved_and_warns_without_rejection(self):
        self.register("dev-a", "claude")
        lot = work.add(self.db, title="external wait syntax", assignee="dev-a")
        text = "hold: attente owner signer le go"
        proc = self.mesh("work", "note", str(lot["id"]), text,
                         env=self.env(AGENT_MAIL_NAME="dev-a"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("external wait not recognized", proc.stderr)
        self.assertTrue(any(e.get("note") == text
                            for e in work.events(self.db, lot["id"])))
