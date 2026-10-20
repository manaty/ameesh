# SPDX-License-Identifier: AGPL-3.0-only
"""Lifecycle replay, remote receipts, publication boundaries, release evidence."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from ameesh import github_stage_comments as comments, plan_github as pg
from .fake_github import FakeGh

REPO = "acme/app"


class StageCommentsTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = SimpleNamespace(cfg=SimpleNamespace(state_dir=self.folder.name))
        self.item = {"id": 12, "title": "Export", "type": "bug", "app": "app",
                     "state": "build", "issue_ref": REPO + "#7", "priority": 1,
                     "planned_end": "2026-10-22"}
        self.events = [{"id": 1, "state": "build", "created_ts": 1, "note": "private"}]
        self.marks = []
        self.st = SimpleNamespace(work=SimpleNamespace(
            milestones=lambda *_: self.marks, events=lambda *_: self.events))
        self.gh = FakeGh()
        self.gh.repo(REPO)["visibility"] = "private"
        self.gh.repo(REPO)["issues"].append({"id": 9007, "number": 7, "title": "Export",
            "body": "human text", "state": "open", "labels": [], "state_reason": None})
        self.guard = pg.Guard(host_names=("worker-7",))

    def project(self, dry_run=False):
        report = {"issues": [{"work_item": 12, "number": 7, "action": "unchanged"}],
                  "comments": [], "withheld": [], "errors": []}
        comments.project(self.db, self.st, pg._Repo(self.gh, REPO, dry_run), report,
                         [(self.item, self.gh.repo(REPO)["issues"][0])], guard=self.guard,
                         refused=set(), project="app", closings={})
        return report

    def bodies(self):
        return self.gh.repo(REPO)["comments"].get("7", [])

    def test_replay_between_passes_order_and_idempotence(self):
        self.marks = [dict(id=4, kind="merged", at_ts=5), dict(id=3, kind="frozen", at_ts=3)]
        self.events = [dict(id=8, state="build", created_ts=4),
                       dict(id=7, state="blocked", created_ts=2,
                            note="internal\nPublic status: Please confirm the CSV contract on this ticket."),
                       *self.events]
        self.item["state"] = "merged"
        report = self.project()
        self.assertEqual([r["event"] for r in report["comments"]],
                         ["accepted", "event-7", "milestone-3", "milestone-4"])
        text = "\n".join(self.bodies())
        for phrase in ("Priority: P1", "2026-10-22", "Please confirm the CSV contract", "Merged;"):
            self.assertIn(phrase, text)
        self.assertNotIn("internal", text)
        self.gh.reset_calls()
        self.assertEqual(self.project()["comments"], [])
        self.assertEqual(self.gh.writes(), [])

    def test_post_failure_and_lost_ack_reconciled_without_issue_update(self):
        self.project()
        self.marks = [dict(id=3, kind="frozen", at_ts=3)]
        self.gh.fail = lambda argv: "-X" in argv and "POST" in argv
        self.assertEqual(len(self.project()["errors"]), 1)
        self.gh.fail = None
        original = self.gh.run

        def lost_ack(argv, stdin=None):
            result = original(argv, stdin)
            if "-X" in argv and "POST" in argv:
                return 1, "", "response lost"
            return result

        with mock.patch.object(self.gh, "run", side_effect=lost_ack):
            self.assertEqual(len(self.project()["errors"]), 1)
        self.assertEqual(len(self.bodies()), 2)
        self.gh.reset_calls()
        self.assertEqual(self.project()["errors"], [])
        self.assertEqual(self.gh.writes(), [])

    def test_paginated_read_failure_never_posts_and_dry_run_is_read_only(self):
        self.assertEqual(len(self.project(dry_run=True)["comments"]), 1)
        self.assertEqual(self.gh.writes(), [])
        reads = [a for a in self.gh.calls() if "--paginate" in a]
        self.assertTrue(any("comments?per_page=100" in " ".join(a) for a in reads))
        self.gh.fail = lambda argv: "--paginate" in argv
        self.assertEqual(len(self.project()["errors"]), 1)
        self.assertEqual(self.bodies(), [])

    def test_private_notes_and_unsafe_opt_in_are_withheld_even_in_private_repo(self):
        for event_id, secret in enumerate(("/home/bob/private", "worker-7", "ghp_" + "a1" * 15), 7):
            with self.subTest(secret=secret[:10]):
                state = "blocked" if event_id == 8 else "waiting_human"
                self.events = [dict(id=event_id, state=state, created_ts=2,
                                    note="private detail\nPublic status: Please use " + secret)]
                report = self.project()
                self.assertTrue(report["withheld"])
                self.assertNotIn(secret, json.dumps(report))
                self.assertNotIn(secret, "\n".join(self.bodies()))
                prefix = "Blocked. " if state == "blocked" else "Awaiting a decision. "
                self.assertIn(prefix + comments.WAIT_DETAIL, self.bodies()[-1])
        self.events = [dict(id=20, state="blocked", created_ts=3, note="private only")]
        self.project()
        self.assertNotIn("private only", "\n".join(self.bodies()))
        self.assertIn("required decision or information", self.bodies()[-1])

    def test_delivery_needs_release_and_matching_activation(self):
        self.item["state"] = "promoted"
        self.marks = [dict(id=2, kind="delivered", at_ts=3)]
        self.project()
        self.assertNotIn("Delivered in release", "\n".join(self.bodies()))
        folder = Path(self.folder.name) / "release" / "reports"
        folder.mkdir(parents=True)
        report = {"schema": "ameesh-release-cutoff/1", "project": "app", "result": "released",
                  "release_sha": "a" * 40, "tag": "v1.2.3", "version": "v1.2.3", "completed_at": 1792454400,
                  "included": [12], "staging": {"exit": 0, "attestation": {
                      "schema": "ameesh-staging-actif/1", "sha": "a" * 40,
                      "hotes": {str(n): {} for n in range(4)}, "lots": ["12"]}}}
        path = folder / "release.json"
        for bad in (dict(report, staging=None), dict(report, project="other"),
                    dict(report, staging={"exit": 0}),
                    dict(report, result="refused-red")):
            path.write_text(json.dumps(bad))
            self.project()
            self.assertNotIn("Delivered in release", "\n".join(self.bodies()))
        bad = copy.deepcopy(report)
        bad["staging"]["attestation"]["sha"] = "b" * 40
        path.write_text(json.dumps(bad))
        self.project()
        self.assertNotIn("Delivered in release", "\n".join(self.bodies()))
        # A reconciled report may get included lots from the activation proof.
        report.pop("included")
        report["result"] = "reconciled"
        path.write_text(json.dumps(report))
        self.project()
        self.assertIn("Delivered in release v1.2.3 on 2026-10-20", self.bodies()[-1])
        self.gh.reset_calls()
        self.project()
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual(self.gh.calls(), [])

    def test_preexisting_closed_ticket_does_not_backfill_or_read_thread(self):
        self.item["state"] = "closed"
        self.gh.repo(REPO)["issues"][0]["state"] = "closed"
        self.marks = [dict(id=4, kind="merged", at_ts=5), dict(id=3, kind="frozen", at_ts=3)]
        self.st.hosts = SimpleNamespace(current=lambda *_: [])
        self.st.work.issue_feed = lambda *_: [self.item]
        before = copy.deepcopy(self.bodies())
        with mock.patch.object(pg.storage, "of", return_value=self.st):
            for _ in range(2):
                self.gh.reset_calls()
                result = pg.project_lots(self.db, self.gh, "app", REPO)
                self.assertEqual(result["comments"], [])
                self.assertFalse(any("/comments" in " ".join(a) for a in self.gh.calls()))
        self.assertEqual(self.bodies(), before)

    def test_closure_retries_after_post_failure_on_now_closed_ticket(self):
        self.st.hosts = SimpleNamespace(current=lambda *_: [])
        self.st.work.issue_feed = lambda *_: [self.item]
        with mock.patch.object(pg.storage, "of", return_value=self.st):
            # Existing mirror from before enabling stage comments.
            with mock.patch.object(comments, "project"):
                pg.project_lots(self.db, self.gh, "app", REPO)
            self.item.update(state="merged", pr_ref=REPO + "#42")
            self.gh.fail = lambda argv: "POST" in argv and "/comments" in " ".join(argv)
            result = pg.project_lots(self.db, self.gh, "app", REPO)
            self.assertTrue(result["errors"])
            self.assertEqual(self.gh.repo(REPO)["issues"][0]["state"], "closed")
            self.gh.fail = None
            result = pg.project_lots(self.db, self.gh, "app", REPO)
            self.assertEqual([r["event"] for r in result["comments"]], ["accepted", "merged"])
            self.assertIn("Merged by PR #42;", self.bodies()[-1])
            self.gh.reset_calls()
            self.assertEqual(pg.project_lots(self.db, self.gh, "app", REPO)["comments"], [])
            self.assertFalse(any("/comments" in " ".join(a) for a in self.gh.calls()))

    def test_closed_unenrolled_ticket_only_receives_proven_delivery(self):
        self.gh.repo(REPO)["issues"][0]["state"] = "closed"
        self.item["state"] = "promoted"
        with mock.patch.object(comments, "release_receipts", return_value={12: [("v1.2.3", "2026-10-20")]}):
            result = self.project()
            self.assertEqual([r["event"] for r in result["comments"]], ["delivered-v1.2.3"])
            self.gh.reset_calls()
            self.assertEqual(self.project()["comments"], [])
            self.assertEqual(self.gh.calls(), [])

    def test_dry_run_never_enrolls_or_consumes_receipts(self):
        self.project(dry_run=True)
        self.assertFalse((Path(self.folder.name) / "github-stage-comments.json").exists())
        self.assertEqual(len(self.project()["comments"]), 1)
        self.gh.reset_calls()
        self.assertEqual(self.project()["comments"], [])
        self.assertEqual(self.gh.calls(), [])

    def test_real_projection_calls_comments_when_issue_is_unchanged(self):
        self.st.hosts = SimpleNamespace(current=lambda *_: [])
        self.st.work.issue_feed = lambda *_: [self.item]
        with mock.patch.object(pg.storage, "of", return_value=self.st):
            pg.project_lots(self.db, self.gh, "app", REPO)
            self.gh.reset_calls()
            self.events = [dict(id=7, state="blocked", created_ts=2,
                                note="Public status: Please confirm the contract."), *self.events]
            result = pg.project_lots(self.db, self.gh, "app", REPO)
        self.assertEqual(result["counts"]["unchanged"], 1)
        self.assertEqual(result["comments"][0]["event"], "event-7")
        self.assertTrue(all("PATCH" not in a for a in self.gh.writes()))

    def test_external_issue_ref_comments_created_and_rediscovered_mirror(self):
        self.item["issue_ref"] = "external/requests#42"
        self.gh.repo(REPO)["issues"] = []
        self.st.hosts = SimpleNamespace(current=lambda *_: [])
        self.st.work.issue_feed = lambda *_: [self.item]
        self.marks = [dict(id=3, kind="frozen", at_ts=3)]
        with mock.patch.object(pg.storage, "of", return_value=self.st):
            result = pg.project_lots(self.db, self.gh, "app", REPO)
            self.assertEqual(result["errors"], [])
            self.assertEqual([r["event"] for r in result["comments"]],
                             ["accepted", "milestone-3"])
            mirror = result["issues"][0]["number"]
            self.assertTrue(all(r["number"] == mirror for r in result["comments"]))
            self.assertEqual(self.item["issue_ref"], "external/requests#42")
            self.events = [dict(id=7, state="blocked", created_ts=4,
                                note="Public status: Please confirm the contract."), *self.events]
            result = pg.project_lots(self.db, self.gh, "app", REPO)
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["counts"]["unchanged"], 1)
            self.assertEqual([(r["number"], r["event"]) for r in result["comments"]],
                             [(mirror, "event-7")])
            self.assertEqual(self.item["issue_ref"], "external/requests#42")
            self.gh.reset_calls()
            self.assertEqual(pg.project_lots(self.db, self.gh, "app", REPO)["comments"], [])
            self.assertEqual(self.gh.writes(), [])
        self.assertEqual(set(self.gh.state["repos"]), {REPO})
