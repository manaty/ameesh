# SPDX-License-Identifier: AGPL-3.0-only
"""#5506: an input delivered to the watcher is followed until a proof of
processing; recalled at 60 min, escalated to the pilot at 2 h."""
from __future__ import annotations

import dataclasses
from unittest import mock

from ameesh import github_watch as watch, mail, registry, storage, work
from .support import PgTestCase
from .test_2845_github_watch import SUB, NOW, Api, comment, issue

H = 3600


class RepliesApi(Api):
    """Issues/comments feed, plus the per-issue comment read of the follow-up."""
    def __init__(self, issues=(), comments=(), replies=()):
        super().__init__(issues, comments)
        self.replies = list(replies)

    def get(self, path, **kw):
        if "/issues/1/comments" in path:
            self.calls.append(path)
            return list(self.replies)
        return super().get(path, **kw)


class FollowUpTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE github_watch_cursors, github_watch_events,"
                        " github_watch_scans, github_watch_pending")
        registry.upsert(self.db, "watcher", harness="codex", chantier="app")
        registry.upsert(self.db, "pilote", harness="claude", chantier="app")
        self.cfg = dataclasses.replace(self.cfg, host="atelier", github={"host": "atelier"})
        pilots = mock.patch("ameesh.alert_briefs.project_pilots", return_value=["pilote"])
        pilots.start()
        self.addCleanup(pilots.stop)

    def tick(self, api, now):
        return watch.Watcher(self.cfg, api=api, declarations=[SUB]).tick(self.db, now)

    def inbox(self, name):
        return mail.unread(self.db, name)

    def follow_ups(self, name, stage):
        return [m for m in self.inbox(name)
                if (m.get("payload") or {}).get("github_watch_follow_up", {}).get("stage")
                == stage]

    def pending(self):
        return {(r["kind"], r["object_id"]): r for r in self.db.query(
            "SELECT * FROM github_watch_pending")}

    def test_unprocessed_input_is_recalled_at_60_min_then_escalated_at_2_h(self):
        api = RepliesApi([issue()])
        self.tick(api, NOW)
        self.assertEqual(set(self.pending()), {("issue", 1)})
        self.tick(api, NOW + H - 60)
        self.assertEqual(self.follow_ups("watcher", "reminder"), [])
        self.tick(api, NOW + H + 1)  # checked 61 s ago: next check within 5 min
        self.assertEqual(self.follow_ups("watcher", "reminder"), [])
        self.tick(api, NOW + H - 60 + watch.FOLLOW_UP_EVERY)
        recalls = self.follow_ups("watcher", "reminder")
        self.assertEqual(len(recalls), 1)
        self.assertIn("https://github.com/acme/app/issues/1", recalls[0]["body"])
        self.assertEqual(self.follow_ups("pilote", "escalation"), [])
        self.tick(api, NOW + H - 60 + 2 * watch.FOLLOW_UP_EVERY)  # no second recall
        self.assertEqual(len(self.follow_ups("watcher", "reminder")), 1)
        self.tick(api, NOW + 2 * H + 1)
        escalations = self.follow_ups("pilote", "escalation")
        self.assertEqual(len(escalations), 1)
        self.assertIn("acme/app#1", escalations[0]["body"])
        self.tick(api, NOW + 2 * H + 400)
        self.assertEqual(len(self.follow_ups("pilote", "escalation")), 1)
        self.assertIsNone(self.pending()[("issue", 1)]["resolved_at"])

    def test_lot_with_issue_ref_closes_the_input(self):
        api = RepliesApi([issue()])
        self.tick(api, NOW)
        lot = work.add(self.db, title="export", issue_ref="acme/app#1", app="app",
                       actor="watcher")
        self.tick(api, NOW + H + 1)
        self.assertEqual(self.follow_ups("watcher", "reminder"), [])
        row = self.pending()[("issue", 1)]
        self.assertEqual(row["proof"], "lot #%s" % lot["id"])
        self.assertIsNotNone(row["resolved_at"])

    def test_comment_needs_a_later_lot_or_a_note_citing_its_url(self):
        lot = work.add(self.db, title="export", issue_ref="acme/app#1", app="app",
                       actor="watcher")
        api = RepliesApi(comments=[comment()])
        self.tick(api, NOW)
        # The lot predates the comment: it does not prove the comment handled.
        self.tick(api, NOW + H + 1)
        self.assertEqual(len(self.follow_ups("watcher", "reminder")), 1)
        work.note(self.db, lot["id"], "read https://github.com/acme/app/issues/1"
                  "#issuecomment-10: scope widened", actor="watcher")
        self.tick(api, NOW + 2 * H + 1)
        self.assertEqual(self.follow_ups("pilote", "escalation"), [])
        self.assertEqual(self.pending()[("comment", 10)]["proof"],
                         "note on lot #%s" % lot["id"])

    def test_note_written_by_ameesh_is_not_a_proof(self):
        lot = work.add(self.db, title="export", issue_ref="acme/app#9", app="app",
                       actor="watcher")
        api = RepliesApi([issue()])
        self.tick(api, NOW)
        work.note(self.db, lot["id"], "mirror https://github.com/acme/app/issues/1",
                  actor="ameesh")
        self.tick(api, NOW + H + 1)
        self.assertEqual(len(self.follow_ups("watcher", "reminder")), 1)

    def test_marked_mesh_reply_after_delivery_closes_the_input(self):
        api = RepliesApi([issue()], replies=[
            dict(body="earlier " + watch.REPLY_MARKER, created_at=watch.iso(NOW - 10)),
            dict(body="Answered " + watch.REPLY_MARKER, created_at=watch.iso(NOW + 600),
                 html_url="https://github.com/acme/app/issues/1#issuecomment-99")])
        self.tick(api, NOW)
        self.tick(api, NOW + H + 1)
        self.assertEqual(self.follow_ups("watcher", "reminder"), [])
        self.assertEqual(self.pending()[("issue", 1)]["proof"],
                         "mesh reply https://github.com/acme/app/issues/1#issuecomment-99")

    def test_gh_comment_from_a_turn_is_a_reply_but_a_stage_comment_is_not(self):
        stage = "<!-- ameesh:work-stage:v1 lot=12 event=merged -->"
        api = RepliesApi([issue()], replies=[
            dict(body="Merged " + stage, created_at=watch.iso(NOW + 300))])
        self.tick(api, NOW)
        self.tick(api, NOW + H + 1)
        self.assertEqual(len(self.follow_ups("watcher", "reminder")), 1)
        api.replies.append(dict(
            body="Fixed in 1.34 <!-- ameesh:work-stage:v1 lot=0 event=manual -->",
            created_at=watch.iso(NOW + H + 100)))
        self.tick(api, NOW + 2 * H + 1)
        self.assertEqual(self.follow_ups("pilote", "escalation"), [])
        self.assertIsNotNone(self.pending()[("issue", 1)]["resolved_at"])

    def test_unmarked_or_older_reply_is_not_a_proof(self):
        api = RepliesApi([issue()], replies=[
            dict(body=watch.REPLY_MARKER, created_at=watch.iso(NOW - 10)),
            dict(body="thanks", created_at=watch.iso(NOW + 600))])
        self.tick(api, NOW)
        self.tick(api, NOW + H + 1)
        self.assertEqual(len(self.follow_ups("watcher", "reminder")), 1)

    def test_edited_comment_reopens_its_follow_up(self):
        api = RepliesApi(comments=[comment()])
        self.tick(api, NOW)
        lot = work.add(self.db, title="export", issue_ref="acme/app#1", app="app",
                       actor="watcher")
        self.db.execute("UPDATE work_items SET created_at = to_timestamp(%s) WHERE id = %s",
                        (NOW + 60, lot["id"]))
        self.tick(api, NOW + 901)
        self.assertIsNotNone(self.pending()[("comment", 10)]["resolved_at"])
        api.comments[0]["body"] = "Changed my mind"
        self.tick(api, NOW + 1802)
        row = self.pending()[("comment", 10)]
        self.assertIsNone(row["resolved_at"])
        self.assertEqual(row["delivered_at"], NOW + 1802)

    def test_new_digest_is_headed_by_the_open_count(self):
        api = RepliesApi([issue()], [comment()])
        self.tick(api, NOW)
        api.issues.append(issue(2))
        self.tick(api, NOW + 1802)
        bodies = [m["body"] for m in self.inbox("watcher")
                  if (m.get("payload") or {}).get("github_watch")]
        self.assertFalse(bodies[0].startswith("Still open"))
        self.assertTrue(bodies[-1].startswith(
            "Still open from earlier GitHub watch passes: 2 input(s)"))
        self.assertEqual(bodies[-1].count("https://github.com/acme/app/issues/1#"), 0)

    def reply_reads(self, api):
        return [c for c in api.calls if "/issues/1/comments" in c]

    def test_check_cadence_is_persisted_across_fresh_processes(self):
        # The role timer runs `notify --once` every 90 s: a fresh Watcher each time.
        api = RepliesApi([issue()])
        self.tick(api, NOW)
        self.tick(api, NOW + H + 1)
        self.assertEqual(len(self.reply_reads(api)), 1)
        self.tick(api, NOW + H + 91)
        self.tick(api, NOW + H + 181)
        self.assertEqual(len(self.reply_reads(api)), 1)
        self.tick(api, NOW + H + 1 + watch.FOLLOW_UP_EVERY)
        self.assertEqual(len(self.reply_reads(api)), 2)

    def test_escalated_input_is_still_closed_by_a_later_reply(self):
        api = RepliesApi([issue()])
        self.tick(api, NOW)
        self.tick(api, NOW + H + 1)
        self.tick(api, NOW + 2 * H + 1)
        self.assertEqual(len(self.follow_ups("pilote", "escalation")), 1)
        api.replies.append(dict(body="Answered " + watch.REPLY_MARKER,
                                created_at=watch.iso(NOW + 2 * H + 100)))
        self.tick(api, NOW + 2 * H + 1 + watch.FOLLOW_UP_EVERY)
        row = self.pending()[("issue", 1)]
        self.assertIsNotNone(row["resolved_at"])
        api.issues.append(issue(2))
        self.tick(api, NOW + 3 * H)
        bodies = [m["body"] for m in self.inbox("watcher")
                  if (m.get("payload") or {}).get("github_watch")]
        self.assertFalse(bodies[-1].startswith("Still open"))

    def test_dry_run_follows_nothing(self):
        api = RepliesApi([issue()])
        watch.Watcher(self.cfg, api=api, declarations=[SUB], dry_run=True).tick(
            self.db, NOW)
        self.assertEqual(self.pending(), {})
