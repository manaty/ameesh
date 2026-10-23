# SPDX-License-Identifier: AGPL-3.0-only
"""#2854: versioned receipt cache, tolerant replay and documented repair."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from ameesh import github_stage_comments as comments, plan_github as pg
from .fake_github import FakeGh

REPO = "acme/app"
TICKET = REPO + "#7/lot-12"


class StageReceiptsRecoveryTest(unittest.TestCase):
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

    @property
    def checkpoint_path(self):
        return Path(self.folder.name) / "github-stage-comments.json"

    def checkpoint(self):
        return json.loads(self.checkpoint_path.read_text(encoding="utf-8"))

    def project(self, dry_run=False):
        report = {"issues": [{"work_item": 12, "number": 7, "action": "unchanged"}],
                  "comments": [], "withheld": [], "errors": []}
        comments.project(self.db, self.st, pg._Repo(self.gh, REPO, dry_run), report,
                         [(self.item, self.gh.repo(REPO)["issues"][0])], guard=self.guard,
                         refused=set(), project="app", closings={})
        return report

    def bodies(self):
        return self.gh.repo(REPO)["comments"].get("7", [])

    def test_legacy_cache_is_read_then_versioned_on_next_write(self):
        self.project()
        legacy = self.checkpoint()
        self.assertEqual(legacy["schema"], comments.CHECKPOINT_SCHEMA)
        legacy.pop("schema")
        self.checkpoint_path.write_text(json.dumps(legacy))
        self.marks = [dict(id=3, kind="frozen", at_ts=3)]
        report = self.project()
        self.assertEqual([r["event"] for r in report["comments"]], ["frozen"])
        data = self.checkpoint()
        self.assertEqual(data["schema"], comments.CHECKPOINT_SCHEMA)
        self.assertEqual(data[TICKET], {"enrolled": True, "seen": ["accepted", "frozen"],
                                        "baseline": False})

    def test_unknown_schema_is_reported_and_never_rewritten(self):
        raw = json.dumps({"schema": "ameesh/github-stage-comments/9",
                          TICKET: {"enrolled": True, "seen": ["accepted"]}}) + "\n"
        self.checkpoint_path.write_text(raw)
        report = self.project()
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("unsupported stage-comment checkpoint schema",
                      report["errors"][0]["detail"])
        self.assertEqual(self.bodies(), [])
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual(self.checkpoint_path.read_text(encoding="utf-8"), raw)

    def test_malformed_entry_replays_without_duplicating_markers(self):
        self.project()
        self.events = [dict(id=7, state="blocked", created_ts=2,
                            note="Public status: Please confirm the contract."), *self.events]
        for bad in (None, [], {"enrolled": "yes", "seen": []},
                    {"enrolled": True}, {"enrolled": True, "seen": ["accepted", 3]}):
            with self.subTest(entry=bad):
                data = self.checkpoint()
                data[TICKET] = bad
                self.checkpoint_path.write_text(json.dumps(data))
                report = self.project()
                journal = [e for e in report["errors"] if e.get("action") == "stage-checkpoint"]
                self.assertEqual(len(journal), 1)
                self.assertEqual(journal[0]["work_item"], 12)
        text = "\n".join(self.bodies())
        self.assertEqual(text.count(comments.MARKER % (12, "accepted")), 1)
        self.assertEqual(text.count(comments.MARKER % (12, "event-7")), 1)

    def test_deleted_remote_comment_is_repaired_after_clearing_seen(self):
        # #2861: a mirror enrolled while already frozen starts from its current
        # stage, so "accepted" is posted before the freeze.
        self.project()
        self.marks = [dict(id=3, kind="frozen", at_ts=3)]
        self.project()
        self.assertEqual(len(self.bodies()), 2)
        # Gesture documented for an operator: drop the deleted stage key from
        # `seen`, keep the rest of the cache, then rerun the projection.
        data = self.checkpoint()
        data[TICKET]["seen"].remove("frozen")
        self.checkpoint_path.write_text(json.dumps(data))
        self.gh.repo(REPO)["comments"]["7"] = [
            body for body in self.bodies()
            if (comments.MARKER % (12, "frozen")) not in body]
        report = self.project()
        self.assertEqual([r["event"] for r in report["comments"]], ["frozen"])
        text = "\n".join(self.bodies())
        self.assertEqual(text.count(comments.MARKER % (12, "accepted")), 1)
        self.assertEqual(text.count(comments.MARKER % (12, "frozen")), 1)
        self.gh.reset_calls()
        self.assertEqual(self.project()["comments"], [])
        self.assertEqual(self.gh.writes(), [])

    def test_corrupt_root_is_reported_and_left_untouched(self):
        self.checkpoint_path.write_text("[]")
        report = self.project()
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["errors"][0]["action"], "stage-checkpoint")
        self.assertEqual(self.bodies(), [])
        self.assertEqual(self.checkpoint_path.read_text(encoding="utf-8"), "[]")

    def test_malformed_closed_entry_is_reset_once_without_backfill(self):
        self.gh.repo(REPO)["issues"][0]["state"] = "closed"
        self.checkpoint_path.write_text(json.dumps(
            {"schema": comments.CHECKPOINT_SCHEMA,
             TICKET: {"enrolled": True, "seen": "accepted"}}))
        report = self.project()
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(self.checkpoint()[TICKET], {"enrolled": False, "seen": []})
        self.assertEqual(self.bodies(), [])
        self.gh.reset_calls()
        self.assertEqual(self.project()["errors"], [])
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual(self.bodies(), [])
