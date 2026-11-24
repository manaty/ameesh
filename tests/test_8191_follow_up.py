# SPDX-License-Identifier: AGPL-3.0-only
"""#8191: personal follow-ups survive without entering development queues."""
import json
from unittest import mock

from ameesh import registry, storage, work, work_footprints
from ameesh.db import DbError

from .support import PgTestCase


class FollowUpTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("avatar", "other"):
            registry.upsert(self.db, name, harness="codex", host=self.cfg.host,
                            cwd=self.tmp, mode="execute")

    def follow_up(self, **kwargs):
        fields = dict(title="Check the requested answer", type="follow-up",
                      assignee="avatar", app="ameesh", actor="avatar", cfg=self.cfg)
        fields.update(kwargs)
        return work.add(self.db, **fields)

    def test_five_personal_follow_ups_and_a_development_lot(self):
        with mock.patch("ameesh.work_footprints.analyze_for_creation") as analyze:
            lots = [self.follow_up() for _ in range(5)]
        analyze.assert_not_called()
        self.assertEqual(len({lot["id"] for lot in lots}), 5)
        for lot in lots:
            self.assertEqual(lot["assignee"], "avatar")
            self.assertEqual(lot["state"], "intake")
            self.assertIsNone(lot.get("estimate_minutes"))
            self.assertNotIn("queued_behind", lot)
            self.assertEqual(self.db.query(
                "SELECT * FROM work_item_footprints WHERE work_item_id = %s",
                (lot["id"],)), [])
        # Personal reminders do not take the one pending development slot.
        ordinary = work.add(self.db, title="Implement the change", app="ameesh",
                            assignee="avatar", actor="avatar", footprint=[])
        self.assertEqual(ordinary["assignee"], "avatar")
        with self.assertRaises(work.WorkError):
            work.assign(self.db, lots[0]["id"], "other", claim=True, actor="other")
        self.assertEqual(work.get(self.db, lots[0]["id"])["assignee"], "avatar")

    def test_follow_ups_do_not_fill_repository_capacity(self):
        storage.of(self.db).work_footprints.set_policy(
            "ameesh", actor="human:owner", max_in_flight=1)
        for _ in range(5):
            self.follow_up()
        free = work.add(self.db, title="Free development", app="ameesh", footprint=[])
        self.assertIn(free["id"], {row["id"] for row in
                                  storage.of(self.db).work.unassigned_lots(50)})
        ordinary = work.add(self.db, title="Development", app="ameesh",
                            assignee="avatar", actor="avatar", footprint=[])
        self.assertEqual(ordinary["assignee"], "avatar")
        self.assertTrue(work_footprints.is_exempt({"type": "follow-up"}))

    def test_stopped_agent_returns_code_and_keeps_personal_follow_ups(self):
        follow_up = self.follow_up()
        code = work.add(self.db, title="Development", app="ameesh",
                        assignee="avatar", actor="avatar", footprint=[])
        registry.set_status(self.db, "avatar", "stopped", stop_reason="manuel")
        for dry_run in (True, False):
            released = storage.of(self.db).work.release_agent(
                "avatar", reason="manual stop", actor="human:owner",
                stopped_with=work.RELEASABLE_STOP_REASONS, dry_run=dry_run)
            self.assertEqual([row["id"] for row in released], [code["id"]])
        self.assertIsNone(work.get(self.db, code["id"])["assignee"])
        self.assertEqual(work.get(self.db, follow_up["id"])["assignee"], "avatar")

    def test_stopped_agent_holding_only_a_follow_up_is_not_held(self):
        self.follow_up()
        registry.set_status(self.db, "avatar", "stopped", stop_reason="manuel")
        self.assertEqual(work.held_lots(self.db, ["avatar"]), {})
        code = work.add(self.db, title="Development", app="ameesh",
                        assignee="avatar", actor="avatar", footprint=[])
        self.assertEqual(work.held_lots(self.db, ["avatar"]),
                         {"avatar": {"open": True, "promoted": False}})
        storage.of(self.db).work.release_agent(
            "avatar", reason="manual stop", actor="human:owner",
            stopped_with=work.RELEASABLE_STOP_REASONS)
        self.assertIsNone(work.get(self.db, code["id"])["assignee"])
        self.assertEqual(work.held_lots(self.db, ["avatar"]), {})

    def test_assignment_is_required(self):
        with self.assertRaisesRegex(work.WorkError, "requires an assignee"):
            self.follow_up(assignee=None)

    def test_no_development_metadata(self):
        for fields in ({"branch": "lot/123"}, {"estimate": "1h"}, {"size": "s"}):
            with self.subTest(fields=fields), self.assertRaises(work.WorkError):
                self.follow_up(**fields)

    def test_close_done_and_abandoned(self):
        for done in (True, False):
            lot = self.follow_up()
            result = work.close(self.db, lot["id"], done=done, abandoned=not done,
                                actor="avatar")
            self.assertEqual(result["state"], "closed")
            self.assertEqual(result["close_reason"], "done" if done else "abandoned")

    def test_sql_cannot_offer_a_follow_up_to_the_team(self):
        lot = self.follow_up()
        with self.assertRaises(DbError):
            self.db.execute("UPDATE work_items SET assignee = NULL WHERE id = %s",
                            (lot["id"],))
        with self.assertRaises(DbError):
            self.db.execute("INSERT INTO work_items (type, title) VALUES ('follow-up', 'missing owner')")

    def test_sql_cannot_enter_development_cycle(self):
        lot = self.follow_up()
        with self.assertRaises(DbError):
            self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (lot["id"],))

    def test_cli_add_without_estimate_and_show(self):
        proc = self.mesh("work", "add", "--type", "follow-up", "--title", "Check answer",
                         "--assignee", "avatar", "--app", "ameesh", "--new-project",
                         "--actor", "avatar", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        item = json.loads(proc.stdout)["lot"]
        shown = self.mesh("work", "show", str(item["id"]))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        for label in ("estimate :", "reference:", "milestones:", "delays   :",
                      "reviewed :", "reviewer :", "floor    :"):
            self.assertNotIn(label, shown.stdout)
        self.assertEqual(item["assignee"], "avatar")
