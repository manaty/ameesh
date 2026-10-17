# SPDX-License-Identifier: AGPL-3.0-only
"""Discovery uses real Git and disposable SQL, never a host cleanup."""
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import housekeeping as hk, housekeeping_discovery as discovery
from ameesh import registry, storage, work
from tests.support import PgTestCase
from tests.test_l73_housekeeping import _Cfg, _git, _repo, _sans_processus


class RootsTest(unittest.TestCase):
    def test_host_templates_give_shallow_roots_and_no_home_scan(self):
        policy = SimpleNamespace(work_root="/srv/work", housekeeping={}, work_roots={
            "ameesh": "/srv/work/{agent}", "nexlink": "/srv/work/nexlink-{agent}",
            "nested": "/srv/nested/{agent}/repo", "broad": "~/"})
        canon = SimpleNamespace(id="test", host=lambda name: SimpleNamespace(policy=policy))
        self.assertEqual(hk.effective([canon], "pc").worktree_roots,
                         ("/srv/nested", "/srv/work"))
        self.assertEqual(hk.effective([], "pc").worktree_roots, ())


class DiscoveryTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM managed_worktrees")
        self.db.execute("DELETE FROM housekeeping_log")
        self.root = os.path.join(self.tmp, "work")
        os.mkdir(self.root)
        self.main = _repo(self.tmp)
        registry.upsert(self.db, "a1", harness="claude", host="pc", cwd=self.main)
        self.agents = {"a1": self.main}
        self.store = storage.of(self.db).housekeeping

    def linked(self, name, branch):
        path = os.path.join(self.root, name)
        _git("worktree", "add", "-q", path, "-b", branch, cwd=self.main)
        return path

    def discover(self, dry_run=False):
        return discovery.discover(self.db, "pc", self.agents, (self.root,), dry_run=dry_run)

    def test_dry_run_lists_lost_worktrees_clones_and_plain_dirs_without_writing(self):
        linked = self.linked("a1-review", "review/topic")
        clone = os.path.join(self.root, "clone")
        _git("clone", "-q", self.main, clone, cwd=self.tmp)
        plain = os.path.join(self.root, "plain")
        os.mkdir(plain)
        outside = os.path.join(self.tmp, "outside")
        os.mkdir(outside)
        os.symlink(outside, os.path.join(self.root, "symlink"))
        marker_link = os.path.join(self.root, "marker-link")
        os.mkdir(marker_link)
        os.symlink(os.path.join(self.main, ".git"), os.path.join(marker_link, ".git"))

        report = self.discover(dry_run=True)
        plan = {row["path"]: row for row in report["plan"]}
        self.assertEqual(plan[linked]["action"], "register")
        for path in (clone, plain, marker_link):
            self.assertEqual(plan[path]["action"], "garde")
        self.assertNotIn(os.path.join(self.root, "symlink"), plan)
        self.assertEqual(report["entries"], [])
        self.assertEqual(self.store.worktrees("pc"), [])
        self.assertEqual(self.store.recent("pc", 3600), [])

        applied = self.discover()
        self.assertEqual([row["path"] for row in self.store.worktrees("pc")], [linked])
        self.assertEqual(sum(row["action"] == "signaled" for row in applied["entries"]), 3)
        self.assertTrue(all(os.path.isdir(path) for path in (linked, clone, plain, outside)))
        self.assertFalse(any(row["action"] == "registered" for row in self.discover()["entries"]))

    def test_gone_or_removed_path_gets_a_new_lot_branch_and_age(self):
        for terminal in ("gone", "removed"):
            with self.subTest(terminal=terminal):
                old_lot = work.add(self.db, title="old", assignee="a1")["id"]
                path = self.linked(terminal, "old-" + terminal)
                old = self.store.register_worktree(host="pc", path=path,
                    repo=hk.common_dir(self.main), agent="a1", lot=str(old_lot),
                    turn_id="old", branch="old-" + terminal, head="old")
                self.store.set_worktree_status(old["id"], terminal, "old path")
                self.db.execute("UPDATE managed_worktrees SET created_at=now()-interval '10 days'"
                                " WHERE id=%s", (old["id"],))
                _git("worktree", "remove", path, cwd=self.main)
                work.close(self.db, old_lot, abandoned=True)
                new_lot = work.add(self.db, title="new", assignee="a1")["id"]
                branch = "lot/%s-new" % new_lot
                _git("worktree", "add", "-q", path, "-b", branch, cwd=self.main)
                work.close(self.db, new_lot, abandoned=True)

                self.discover(dry_run=True)
                self.assertEqual(len([r for r in self.store.worktrees("pc") if r["path"] == path]), 1)
                self.discover()
                rows = [r for r in self.store.worktrees("pc") if r["path"] == path]
                self.assertEqual(len(rows), 2)
                fresh = next(row for row in rows if row["status"] == "active")
                self.assertEqual((fresh["lot"], fresh["branch"], fresh["turn_id"], fresh["ended_ts"]),
                                 (str(new_lot), branch, None, None))
                self.assertNotEqual(fresh["id"], old["id"])
                _entries, plan = hk._worktree_pass(self.db, "pc", dry_run=True,
                    protected={self.main}, now=fresh["created_ts"] + 1)
                self.assertEqual(next(row["action"] for row in plan if row["path"] == path), "attend")

    def test_discovered_copies_use_normal_removal_and_unpushed_guards(self):
        _sans_processus(self)
        clean = self.linked("clean", "topic-clean")
        unpushed = self.linked("unpushed", "topic-unpushed")
        with open(os.path.join(unpushed, "work.txt"), "w") as output:
            output.write("unpublished work\n")
        _git("add", "work.txt", cwd=unpushed)
        _git("commit", "-q", "-m", "local work", cwd=unpushed)
        self.discover()
        created = max(row["created_ts"] for row in self.store.worktrees("pc"))
        _entries, plan = hk._worktree_pass(self.db, "pc", dry_run=False,
            protected={self.main}, now=created + hk.WORKTREE_MIN_AGE_S + 1)
        self.assertTrue(os.path.isdir(clean))  # orphan grace is 24 h
        _entries, plan = hk._worktree_pass(self.db, "pc", dry_run=False,
            protected={self.main}, now=created + hk.ORPHAN_WORKTREE_IDLE_S + 60,
            artifacts=(), lock_path=os.path.join(self.tmp, "cleanup.lock"),
            journal_path=os.path.join(self.tmp, "cleanup.journal"))
        self.assertFalse(os.path.exists(clean))
        self.assertTrue(os.path.isdir(unpushed))
        self.assertIn("topic-clean", _git("branch", "--list", "topic-clean", cwd=self.main))
        self.assertEqual({row["path"]: row["status"] for row in self.store.worktrees("pc")},
                         {clean: "removed", unpushed: "kept"})

    def test_only_local_agent_repositories_are_discovered_and_scan_is_gated(self):
        local = self.linked("local", "local")
        other_base = os.path.join(self.tmp, "remote")
        os.mkdir(other_base)
        remote_main = _repo(other_base)
        remote = os.path.join(self.root, "remote-copy")
        _git("worktree", "add", "-q", remote, "-b", "remote-copy", cwd=remote_main)
        registry.upsert(self.db, "remote", harness="claude", host="other-host", cwd=remote_main)
        cfg = _Cfg(os.path.join(self.tmp, "cleanup-state"))
        policy = hk.Policy(worktree_roots=(self.root,), artifacts=(), deps=False)
        hk.run_pass(cfg, self.db, policy, host="pc", actor="test", artifact_scan=False)
        self.assertEqual(self.store.worktrees("pc"), [])
        report = hk.run_pass(cfg, self.db, policy, host="pc", actor="test", artifact_scan=True)
        self.assertEqual([row["path"] for row in self.store.worktrees("pc")], [local])
        self.assertTrue(any(row["path"] == remote and row["action"] == "garde"
                            for row in report["plan"]))
        self.assertTrue(os.path.isdir(remote))

    def test_a_tracked_path_replaced_by_a_clone_is_never_removed(self):
        _sans_processus(self)
        path = self.linked("reused", "old-linked")
        self.discover()
        _git("worktree", "remove", path, cwd=self.main)
        _git("clone", "-q", self.main, path, cwd=self.tmp)
        _entries, plan = hk._worktree_pass(self.db, "pc", dry_run=False,
            protected={self.main}, now=time.time() + 3 * 24 * 3600,
            artifacts=(), lock_path=os.path.join(self.tmp, "cleanup.lock"),
            journal_path=os.path.join(self.tmp, "cleanup.journal"))
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(plan[0]["action"], "garde")
        self.assertIn("no longer matches", plan[0]["detail"])

    def test_branch_changed_after_listing_is_attributed_from_current_references(self):
        path = self.linked("changed", "topic-before")
        lot = work.add(self.db, title="current work", assignee="a1")["id"]
        branch = "lot/%s-current" % lot
        original = hk.list_worktrees

        def listing(cwd):
            rows = original(cwd)
            _git("checkout", "-q", "-b", branch, cwd=path)
            _git("commit", "-q", "--allow-empty", "-m", "new HEAD", cwd=path)
            return rows

        with mock.patch.object(hk, "list_worktrees", side_effect=listing):
            self.discover()
        row = self.store.worktrees("pc")[0]
        self.assertEqual((row["lot"], row["branch"], row["head"]),
                         (str(lot), branch, _git("rev-parse", "HEAD", cwd=path).strip()))
        work.close(self.db, lot, abandoned=True)

    def test_repository_listing_is_shared_and_directory_inventory_is_bounded(self):
        linked = self.linked("copy", "copy")
        self.agents["a2"] = self.main
        with mock.patch.object(hk, "list_worktrees", wraps=hk.list_worktrees) as listing:
            self.discover(dry_run=True)
        self.assertEqual(listing.call_count, 1)
        with mock.patch.object(discovery, "DISCOVERY_LIMIT", 1):
            report = self.discover(dry_run=True)
        self.assertTrue(report["skipped"])
        self.assertTrue(os.path.isdir(linked))
