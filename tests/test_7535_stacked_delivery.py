# SPDX-License-Identifier: AGPL-3.0-only
"""A merged base wakes its project pilot once, including migration follow-up."""
from unittest import mock

from ameesh import registry, stacked_delivery, storage, work

from .support import PgTestCase


class StackedDeliveryTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "pilot", harness="claude", host=self.cfg.host)
        self.base = self.lot("base", "demo", "lot/base", "main")
        self.child = self.lot("child", "demo", "lot/child", "lot/base")
        self.target = mock.patch.object(work, "_project_target", return_value="main")
        self.target.start()
        self.addCleanup(self.target.stop)
        self.pilots = mock.patch.object(stacked_delivery.alert_briefs, "project_pilots",
                                       return_value=["pilot"])
        self.pilots.start()
        self.addCleanup(self.pilots.stop)

    def lot(self, title, app, branch, target):
        return work.add(self.db, title=title, app=app, branch=branch,
                        branch_target=target, actor="author")

    def delivered(self):
        with mock.patch.object(work, "_review_after_merge", return_value={}), \
                mock.patch.object(work, "_resume_review_after_merge", return_value={}):
            return work.close_merged(self.db, self.base["id"], actor="author")

    def test_merge_alert_names_three_followups_once(self):
        self.db.execute("UPDATE work_items SET migration = 582, migration_repo = 'acme/demo',"
                        " pr_ref = 'acme/demo#12' WHERE id = %s", (self.child["id"],))
        self.db.execute(
            "INSERT INTO work_migration_reservations"
            " (work_item_id, slot, number, project, target_repo, target_branch, status)"
            " VALUES (%s, 'extra', 583, 'demo', 'github.com/acme/demo', 'lot/base', 'reserved')",
            (self.child["id"],))
        self.assertEqual(self.delivered()["result"], "merged")
        notes = work.events(self.db, self.child["id"])
        text = next(e["note"] for e in notes if "Base lot #" in e["note"])
        for expected in ("main", "PR base (acme/demo#12)", "migration reservations",
                         "582", "583", "extra"):
            self.assertIn(expected, text)
        self.assertEqual(work.get(self.db, self.child["id"])["branch_target"], "lot/base")
        count = self.db.query("SELECT count(*) AS n FROM agent_mailbox WHERE recipient = 'pilot'")[0]["n"]
        self.assertEqual(count, 1)
        self.delivered()
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM agent_mailbox WHERE recipient = 'pilot'")[0]["n"], 1)

    def test_no_pilot_keeps_explicit_journal_warning(self):
        with mock.patch.object(stacked_delivery.alert_briefs, "project_pilots", return_value=[]):
            self.delivered()
        self.assertTrue(any("no project pilot found" in e["note"]
                            for e in work.events(self.db, self.child["id"])))

    def test_selection_excludes_closed_other_project_and_other_target(self):
        self.lot("other project", "other", "lot/other", "lot/base")
        self.lot("other target", "demo", "lot/other", "lot/different")
        closed = self.lot("closed", "demo", "lot/closed", "lot/base")
        self.db.execute("UPDATE work_items SET state = 'closed', close_reason = 'abandoned' WHERE id = %s", (closed["id"],))
        rows = storage.of(self.db).work.stacked_on("demo", ["lot/base"])
        self.assertEqual([r["id"] for r in rows], [self.child["id"]])

    def test_delivery_to_intermediate_target_is_silent(self):
        self.db.execute("UPDATE work_items SET branch_target = 'lot/earlier' WHERE id = %s",
                        (self.base["id"],))
        self.delivered()
        self.assertFalse(any("Base lot #" in e["note"] for e in work.events(self.db, self.child["id"])))

    def test_mail_failure_rolls_back_merge_and_journal(self):
        with mock.patch.object(stacked_delivery.mail, "send", side_effect=RuntimeError("mail unavailable")):
            with self.assertRaisesRegex(RuntimeError, "mail unavailable"):
                self.delivered()
        self.assertEqual(work.get(self.db, self.base["id"])["state"], self.base["state"])
        self.assertFalse(any("Base lot #" in e["note"] for e in work.events(self.db, self.child["id"])))
