# SPDX-License-Identifier: AGPL-3.0-only
"""#4995: the signed replacement log resists a SQL writer, and the authority
read proves the replacement from the signed answer instead of the marker."""
from ameesh import decisions

from . import test_l124_decisions as fixtures

ALICE = fixtures.ALICE


class JournalTamperingTest(fixtures._Base):
    owner_key = fixtures.SignatureTest.owner_key

    def setUp(self):
        super().setUp()
        self.key = self.owner_key()

    def old_answer(self):
        old = self.ask(needs_signature=True)
        return self.answer(old["id"], "a", key_path=self.key)["decision"]

    def replacement(self, old):
        return self.ask(supersedes=old["id"], supersedes_option="a",
                        question="Replace the exact former authority?")

    def superseded(self):
        old = self.old_answer()
        new = self.replacement(old)
        self.answer(new["id"], "a", key_path=self.key)
        return old, new

    def current(self, decision_id):
        return decisions.require_current(self.db, decisions.get(self.db, decision_id))

    def test_clearing_the_marker_does_not_restore_authority(self):
        old, new = self.superseded()
        self.db.execute("UPDATE agent_mailbox SET payload = payload #- "
                        "'{decision,superseded_by}' WHERE id = %s", (old["id"],))
        self.assertIsNone(decisions.get(self.db, old["id"])["superseded_by"])
        with self.assertRaisesRegex(decisions.DecisionError,
                                    "superseded by decision #%d" % new["id"]):
            self.current(old["id"])
        # A new replacement is still refused: the log is the evidence.
        with self.assertRaisesRegex(decisions.DecisionError,
                                    "superseded by decision #%d" % new["id"]):
            self.replacement(old)

    def test_removing_the_log_row_fails_closed_on_the_marker(self):
        old, _new = self.superseded()
        self.db.execute("DELETE FROM decision_supersessions")
        with self.assertRaisesRegex(decisions.DecisionError,
                                    "marker without a logged replacement"):
            self.current(old["id"])

    def test_logged_event_without_a_signed_replacement_fails_closed(self):
        """A forged INSERT can only deny the old authority (fail closed): it
        never makes a replacement effective, and the owner repairs the log."""
        old = self.old_answer()
        pending = self.replacement(old)
        self.db.execute(
            "INSERT INTO decision_supersessions"
            " (supersedes, superseded_by, person, occurred_at, proof)"
            " VALUES (%s, %s, %s, now(), '{}'::jsonb)",
            (old["id"], pending["id"], ALICE))
        with self.assertRaisesRegex(decisions.DecisionError, "not a signed replacement"):
            self.current(old["id"])
        self.assertEqual(decisions.get(self.db, pending["id"])["state"], "pending")

    def test_a_rewritten_event_does_not_match_its_signed_answer(self):
        old, _new = self.superseded()
        self.db.execute("UPDATE decision_supersessions SET proof = '{}'::jsonb")
        with self.assertRaisesRegex(decisions.DecisionError,
                                    "does not match its signed answer"):
            self.current(old["id"])

    def test_revoked_owner_key_leaves_the_old_authority_refused(self):
        from ameesh import authority
        old, _new = self.superseded()
        authority.revoke_key(self.db, "alice")
        with self.assertRaisesRegex(decisions.DecisionError, "not proven"):
            self.current(old["id"])

    def test_the_signed_event_keeps_its_exact_old_authority_refused(self):
        old, new = self.superseded()
        with self.assertRaisesRegex(decisions.DecisionError,
                                    "decision #%d superseded by decision #%d"
                                    % (old["id"], new["id"])):
            self.current(old["id"])
        events = self.db.query("SELECT * FROM decision_supersessions")
        self.assertEqual([(event["supersedes"], event["superseded_by"]) for event in events],
                         [(old["id"], new["id"])])
        # An unrelated decision is untouched by the journal.
        other = self.ask(needs_signature=True)
        other = self.answer(other["id"], "a", key_path=self.key)["decision"]
        decisions.require_current(self.db, other)
