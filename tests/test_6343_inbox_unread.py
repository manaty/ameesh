# SPDX-License-Identifier: AGPL-3.0-only
"""#6343 : --unread lit l'identité, y compris ses courriers réservés."""
from ameesh import mail, registry

from .support import PgTestCase


class InboxUnreadTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "beta", harness="codex", host=self.cfg.host)

    def inbox(self, *args):
        result = self.cli("inbox", *args, env=self.env(AGENT_MAIL_NAME="beta"))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_reserved_mail_visible_then_delivered_mail_absent(self):
        mid = mail.send(self.db, "alpha", "beta", "correction-6343", thread=False)
        token = mail.new_token()
        reserved = mail.reserve(self.db, "beta", None, None, token,
                                ids=[mid], porteur="test-6343")
        self.assertEqual([row["id"] for row in reserved], [mid])
        before = mail.get(self.db, mid)
        for args in (("--unread",), (), ("beta", "--unread")):
            output = self.inbox(*args)
            self.assertIn("correction-6343", output)
            self.assertIn("réservé pour le tour suivant", output)
        self.assertEqual(mail.get(self.db, mid), before,
                         "inbox doit laisser la réservation intacte")
        self.assertEqual(mail.deliver(self.db, "beta", None, None, token, [mid]), [mid])
        self.assertNotIn("correction-6343", self.inbox("--unread"))

    def test_unreserved_mail_has_no_reservation_label(self):
        mail.send(self.db, "alpha", "beta", "pending-6343", thread=False)
        output = self.inbox("--unread")
        self.assertIn("pending-6343", output)
        self.assertNotIn("réservé pour le tour suivant", output)

    def test_unknown_option_is_rejected(self):
        result = self.cli("inbox", "--unknown", env=self.env(AGENT_MAIL_NAME="beta"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
