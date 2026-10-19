# SPDX-License-Identifier: AGPL-3.0-only
"""#2743: ad-hoc clones of ended lots — report first, remove under guards."""
import os
import subprocess
import time
import unittest

from ameesh import housekeeping as hk, housekeeping_clones as clones, registry, storage, work
from tests.support import PgTestCase
from tests.test_l73_housekeeping import _Cfg, _git, _repo, _sans_processus


class CloneReapTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "work")
        os.mkdir(self.root)
        self.main = _repo(self.tmp)
        registry.upsert(self.db, "a1", harness="claude", host="pc", cwd=self.main)
        self.agents = {"a1": self.main}
        self.cfg = _Cfg(os.path.join(self.tmp, "state"))
        os.makedirs(os.path.join(self.cfg.state_dir, ".menage"))
        self.lock = os.path.join(self.cfg.state_dir, ".menage", "artifacts.lock")
        self.journal = os.path.join(self.cfg.state_dir, ".menage", "artifacts.journal")
        self.now = time.time() + 3 * 24 * 3600

    def ended_lot(self, age_s=2 * 24 * 3600, state="closed"):
        lot = work.add(self.db, title="ended lot", assignee="a1")["id"]
        if state == "closed":
            work.close(self.db, lot, abandoned=True)
        else:  # merged: closed_at stays NULL, updated_at is the merge time
            self.db.execute("UPDATE work_items SET state = %s, closed_at = NULL,"
                            " updated_at = now() WHERE id = %s", (state, lot))
        self.db.execute("UPDATE work_items SET updated_at = to_timestamp(%s),"
                        " closed_at = CASE WHEN %s = 'closed' THEN to_timestamp(%s)"
                        " ELSE NULL END WHERE id = %s",
                        (time.time() - age_s, state, time.time() - age_s, lot))
        return lot

    def clone(self, name, source=None, shared=False):
        path = os.path.join(self.root, name)
        args = ["git", "clone", "-q"]
        if shared:
            args.append("--shared")
        args += [source or self.main, path]
        subprocess.run(args, check=True, capture_output=True, cwd=self.tmp)
        return path

    def reap(self, dry_run=True, now=None):
        return clones.reap(
            self.db, "pc", self.cfg, roots=(self.root,), agents=self.agents,
            protected={self.main}, dry_run=dry_run, now=self.now if now is None else now,
            artifacts=(), lock_path=self.lock, journal_path=self.journal,
            skip_roots=(self.cfg.state_dir,))

    def plan_for(self, path, report=None):
        entries, plan, skipped = report or self.reap()
        self.assertEqual(skipped, [])
        return next(row for row in plan if row["path"] == path)

    def test_dry_run_lists_an_ended_lot_clone_and_writes_nothing(self):
        _sans_processus(self)
        lot = self.ended_lot()
        path = self.clone("copy-%s-x" % lot)
        entries, plan, skipped = self.reap(dry_run=True)
        row = self.plan_for(path, (entries, plan, skipped))
        self.assertEqual(row["action"], "retrait")
        self.assertEqual(row["lot"], str(lot))
        self.assertGreater(row["bytes"], 0)
        self.assertIn("no alternate dependent", row["detail"])
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(entries, [])
        self.assertEqual(storage.of(self.db).housekeeping.recent("pc", 3600), [])

    def test_apply_removes_a_clean_pushed_idle_clone_logged_in_the_journal(self):
        _sans_processus(self)
        lot = self.ended_lot()
        path = self.clone("copy-%s-x" % lot)
        entries, plan, skipped = self.reap(dry_run=False)
        self.assertEqual(skipped, [])
        self.assertEqual(next(row for row in plan if row["path"] == path)["action"],
                         "retrait")
        self.assertFalse(os.path.exists(path))
        removed = [e for e in entries if e["path"] == path]
        self.assertEqual([(e["kind"], e["action"], e["lot"]) for e in removed],
                         [("clone", "removed", str(lot))])
        self.assertGreater(removed[0]["bytes"], 0)
        self.assertFalse(os.path.exists(path))
        # the pass that calls `reap` logs the entries (tested by run_pass)
        self.assertEqual(storage.of(self.db).housekeeping.recent("pc", 3600), [])

    def test_open_or_unknown_lot_clone_is_kept(self):
        _sans_processus(self)
        lot = work.add(self.db, title="open", assignee="a1")["id"]
        path = self.clone("copy-%s-x" % lot)
        row = self.plan_for(path)
        self.assertEqual(row["action"], "garde")
        self.assertIn("not proved ended", row["detail"])
        self.assertTrue(os.path.isdir(path))

        unknown = self.clone("copy-999999-x")
        row = self.plan_for(unknown)
        self.assertEqual(row["action"], "garde")
        self.assertIn("no lot named", row["detail"])

    def test_lot_ended_less_than_24_hours_ago_waits(self):
        _sans_processus(self)
        lot = self.ended_lot(age_s=3600)
        path = self.clone("copy-%s-x" % lot)
        row = self.plan_for(path, self.reap(now=time.time() + 3600))
        self.assertEqual(row["action"], "garde")
        self.assertIn("waits 24 h", row["detail"])

    def test_uncommitted_unpushed_or_ignored_work_keeps_the_clone(self):
        _sans_processus(self)
        lot = self.ended_lot()
        dirty = self.clone("copy-%s-dirty" % lot)
        with open(os.path.join(dirty, "work.txt"), "w", encoding="utf-8") as output:
            output.write("not committed\n")
        unpushed = self.clone("copy-%s-unpushed" % lot)
        _git("commit", "-q", "--allow-empty", "-m", "local work", cwd=unpushed)
        ignored = self.clone("copy-%s-ignored" % lot)
        with open(os.path.join(ignored, ".env"), "w", encoding="utf-8") as output:
            output.write("SECRET=1\n")
        with open(os.path.join(ignored, ".git", "info", "exclude"), "a",
                  encoding="utf-8") as output:
            output.write(".env\n")
        for path in (dirty, unpushed, ignored):
            with self.subTest(path=path):
                row = self.plan_for(path)
                self.assertEqual(row["action"], "garde")
                self.assertTrue(os.path.isdir(path))

    def test_nested_git_copy_or_linked_worktree_keeps_the_clone(self):
        _sans_processus(self)
        lot = self.ended_lot()
        nested = self.clone("copy-%s-nested" % lot)
        subprocess.run(["git", "init", "-q", os.path.join(nested, "inner")],
                       check=True, capture_output=True)
        linked = self.clone("copy-%s-linked" % lot)
        _git("worktree", "add", "-q", os.path.join(self.root, "wt-%s" % lot),
             "-b", "lot/%s-extra" % lot, cwd=linked)
        for path in (nested, linked):
            with self.subTest(path=path):
                row = self.plan_for(path)
                self.assertEqual(row["action"], "garde")
                self.assertTrue(os.path.isdir(path))

    def test_a_dependent_clone_by_alternates_blocks_the_removal(self):
        _sans_processus(self)
        lot = self.ended_lot()
        source = self.clone("copy-%s-source" % lot)
        dependent = self.clone("copy-%s-dependent" % lot, source=source, shared=True)
        self.assertTrue(os.path.isfile(
            os.path.join(dependent, ".git", "objects", "info", "alternates")))
        row = self.plan_for(source)
        self.assertEqual(row["action"], "garde")
        self.assertIn("alternate dependent", row["detail"])
        self.assertTrue(os.path.isdir(source))

    def test_a_radar_shared_clone_under_the_state_dir_blocks_the_removal(self):
        _sans_processus(self)
        lot = self.ended_lot()
        source = self.clone("copy-%s-source" % lot)
        radar = os.path.join(self.cfg.state_dir, "radar")
        os.makedirs(radar)
        subprocess.run(["git", "clone", "-q", "--shared", source,
                        os.path.join(radar, "project.clone")],
                       check=True, capture_output=True, cwd=self.tmp)
        row = self.plan_for(source)
        self.assertEqual(row["action"], "garde")
        self.assertIn("project.clone", row["detail"])
        self.assertTrue(os.path.isdir(source))

    def test_a_lot_named_only_by_a_lot_branch_is_attributed(self):
        _sans_processus(self)
        lot = self.ended_lot()
        path = self.clone("adhoc")
        _git("checkout", "-q", "-b", "lot/%s-branch" % lot, cwd=path)
        row = self.plan_for(path)
        self.assertEqual(row["action"], "retrait")
        self.assertEqual(row["lot"], str(lot))

    def test_an_agent_number_and_a_registered_checkout_are_not_lots(self):
        _sans_processus(self)
        lot = self.ended_lot()
        _git("clone", "-q", self.main, os.path.join(self.root, "a1"),
             cwd=self.tmp)
        row = self.plan_for(os.path.join(self.root, "a1"))
        self.assertEqual(row["action"], "garde")
        self.assertIn("agent checkout", row["detail"])
        # `a1` is stripped from the name; the remaining lot alone decides.
        mixed = self.clone("a1-%s" % lot)
        self.assertEqual(self.plan_for(mixed)["action"], "retrait")

    def test_recent_activity_keeps_the_clone(self):
        _sans_processus(self)
        lot = self.ended_lot()
        path = self.clone("copy-%s-x" % lot)
        row = self.plan_for(path, self.reap(now=time.time() + 3600))
        self.assertEqual(row["action"], "garde")
        self.assertIn("active", row["detail"])
        self.assertTrue(os.path.isdir(path))

    def test_run_pass_runs_the_clone_pass_when_the_artifact_scan_is_due(self):
        _sans_processus(self)
        lot = self.ended_lot()
        path = self.clone("copy-%s-x" % lot)
        policy = hk.Policy(worktree_roots=(self.root,), artifacts=(), deps=False)
        report = hk.run_pass(self.cfg, self.db, policy, host="pc", actor="test",
                             dry_run=True, now=self.now, artifact_scan=True)
        row = next(row for row in report["plan"]
                   if row.get("kind") == "clone" and row["path"] == path)
        self.assertEqual(row["action"], "retrait")
        self.assertTrue(os.path.isdir(path))

        report = hk.run_pass(self.cfg, self.db, policy, host="pc", actor="test",
                             dry_run=False, now=self.now, artifact_scan=True)
        self.assertFalse(os.path.exists(path))
        recent = storage.of(self.db).housekeeping.recent("pc", 3600)
        self.assertTrue(any(r["kind"] == "clone" and r["action"] == "removed"
                            and r["path"] == path for r in recent))


class NamedLotsTest(unittest.TestCase):
    def test_the_agent_number_is_not_a_lot_when_a_longer_number_is_named(self):
        agents = {"deepseek4", "mty-amh-impl-04", "a1"}
        self.assertEqual(clones.named_lots("ameesh-deepseek4-1666", agents), [1666])
        self.assertEqual(clones.named_lots("mty-amh-impl-04-2162", agents), [2162])
        self.assertEqual(clones.named_lots("ds4-749-review", agents), [749])
        self.assertEqual(clones.named_lots("ds6-2181-2176", agents), [2181, 2176])
        self.assertEqual(clones.named_lots("nexlink-665-reseed", agents), [665])
        self.assertEqual(clones.named_lots("a1", agents), [])
        # a version is not a lot
        self.assertEqual(clones.named_lots("nexlink-0.2.10", agents), [])
        self.assertEqual(clones.named_lots("foo-2026.10.19-665", agents), [665])


if __name__ == "__main__":
    unittest.main()
