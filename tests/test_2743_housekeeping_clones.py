# SPDX-License-Identifier: AGPL-3.0-only
"""#2743: ad-hoc clones of ended lots — report first, remove under guards."""
import os
import subprocess
import time
import unittest
from unittest import mock

from ameesh import housekeeping as hk, housekeeping_clones as clones, platform, registry, storage, work
from ameesh.platform import process
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

    def test_the_dependency_inventory_is_bounded_by_the_pass_budget(self):
        """#3802 B1/cx2 : l'inventaire profond qui tient host-pass (walk de
        /home/ameesh/work) reçoit le budget du passage, pas son seul 60 s."""
        self.clone("c1")
        with mock.patch.object(clones, "_dependency_inventory",
                               return_value=([], True, "")) as dep:
            entries, plan, skipped = clones.reap(
                self.db, "pc", self.cfg, roots=(self.root,), agents=self.agents,
                protected={self.main}, dry_run=True, now=self.now,
                artifacts=(), lock_path=self.lock, journal_path=self.journal,
                skip_roots=(self.cfg.state_dir,), budget_s=0.5)
        dep.assert_called_once()
        self.assertAlmostEqual(dep.call_args.kwargs["budget_s"], 0.5, places=2)

    def plan_for(self, path, report=None):
        entries, plan, skipped = report or self.reap()
        self.assertEqual(skipped, [])
        return next(row for row in plan if row["path"] == path)


    def test_an_unknown_named_lot_keeps_the_clone(self):
        _sans_processus(self)
        lot = self.ended_lot()
        path = self.clone("copy-%s-999999" % lot)
        row = self.plan_for(path)
        self.assertEqual(row["action"], "garde")
        self.assertIn("#999999 unknown", row["detail"])
        self.assertTrue(os.path.isdir(path))


    def test_dependency_proof_does_not_depend_on_the_candidate_bound(self):
        _sans_processus(self)
        lot = self.ended_lot()
        source = self.clone("a-%s-source" % lot)
        self.clone("z-dependent-direct", source=source, shared=True)
        nested = os.path.join(self.root, "container", "nested")
        os.makedirs(nested)
        subprocess.run(["git", "clone", "-q", "--shared", source,
                        os.path.join(nested, "z-dependent")],
                       check=True, capture_output=True, cwd=self.tmp)
        with mock.patch.object(clones, "CLONE_LIMIT", 1):
            copies, complete, why = clones._dependency_inventory(
                self.cfg, (self.root,), self.agents, ())
            self.assertTrue(complete, why)
            dependents, doubt = clones._dependents(copies, [source])
        self.assertEqual(doubt, "")
        self.assertGreaterEqual(len(dependents[source]), 2)

    def test_a_dependent_clone_nested_under_a_container_is_seen(self):
        _sans_processus(self)
        lot = self.ended_lot()
        source = self.clone("copy-%s-source" % lot)
        nested = os.path.join(self.root, "container", "nested")
        os.makedirs(nested)
        subprocess.run(["git", "clone", "-q", "--shared", source,
                        os.path.join(nested, "dependent")],
                       check=True, capture_output=True, cwd=self.tmp)
        row = self.plan_for(source)
        self.assertEqual(row["action"], "garde")
        self.assertIn("alternate dependent", row["detail"])
        self.assertTrue(os.path.isdir(source))

    def test_a_dependent_nested_in_another_checkout_is_seen(self):
        """cx2 #115192: the other checkout's tree is not abandoned.

        The candidate source is `a-<lot>`; an unrelated checkout
        `other-checkout` contains a `--shared` clone of the source.  The
        inventory must enter the other checkout's working tree, otherwise
        the source looks free and removing it would break the nested clone.
        """
        _sans_processus(self)
        lot = self.ended_lot()
        source = self.clone("a-%s-source" % lot)
        other = self.clone("other-checkout")
        nested = os.path.join(other, "nested")
        subprocess.run(["git", "clone", "-q", "--shared", source, nested],
                       check=True, capture_output=True, cwd=self.tmp)
        copies, complete, why = clones._dependency_inventory(
            self.cfg, (self.root,), self.agents, ())
        self.assertTrue(complete, why)
        dependents, doubt = clones._dependents(copies, [source])
        self.assertEqual(doubt, "")
        self.assertIn(hk._real(nested),
                      {hk._real(path) for path in dependents[source]})
        row = self.plan_for(source)
        self.assertEqual(row["action"], "garde")
        self.assertIn("alternate dependent", row["detail"])
        self.assertTrue(os.path.isdir(source))

    def test_the_removal_recheck_sees_a_dependent_in_another_checkout(self):
        _sans_processus(self)
        lot = self.ended_lot()
        source = self.clone("a-%s-source" % lot)
        other = self.clone("other-checkout")
        subprocess.run(["git", "clone", "-q", "--shared", source,
                        os.path.join(other, "nested")],
                       check=True, capture_output=True, cwd=self.tmp)
        why = clones._removal_check(source, self.cfg, (), (), (self.root,),
                                    self.agents)
        self.assertIn("alternate dependent", why)

    def test_a_bare_shared_repository_is_seen_as_a_dependent(self):
        _sans_processus(self)
        lot = self.ended_lot()
        source = self.clone("copy-%s-source" % lot)
        bare = os.path.join(self.root, "bare-%s.git" % lot)
        subprocess.run(["git", "clone", "-q", "--bare", "--shared", source, bare],
                       check=True, capture_output=True, cwd=self.tmp)
        self.assertTrue(os.path.isfile(
            os.path.join(bare, "objects", "info", "alternates")))
        row = self.plan_for(source)
        self.assertEqual(row["action"], "garde")
        self.assertIn("alternate dependent", row["detail"])
        self.assertTrue(os.path.isdir(source))


    def test_an_unreadable_candidate_root_is_a_doubt(self):
        with mock.patch.object(clones.os, "scandir", side_effect=OSError("denied")):
            found, complete, why = clones._clones_under((self.root,))
        self.assertEqual(found, [])
        self.assertFalse(complete)
        self.assertIn("unreadable root", why)

    def test_a_bounded_candidate_inventory_removes_nothing(self):
        _sans_processus(self)
        lot = self.ended_lot()
        source = self.clone("a-%s-source" % lot)
        dependent = self.clone("z-dependent", source=source, shared=True)
        with mock.patch.object(clones, "CLONE_LIMIT", 1):
            entries, plan, skipped = self.reap(dry_run=False)
        self.assertTrue(os.path.isdir(source))
        self.assertTrue(os.path.isdir(dependent))
        self.assertTrue(any("candidate inventory incomplete" in line for line in skipped))
        self.assertEqual([e for e in entries if e["action"] in ("removed", "partial")], [])

    def test_a_bounded_dependency_inventory_removes_nothing(self):
        _sans_processus(self)
        lot = self.ended_lot()
        source = self.clone("copy-%s-source" % lot)
        self.clone("z-dependent", source=source, shared=True)
        with mock.patch.object(clones, "DEPENDENCY_LIMIT", 1):
            entries, plan, skipped = self.reap(dry_run=False)
        self.assertTrue(os.path.isdir(source))
        self.assertTrue(any("dependency inventory incomplete" in line for line in skipped))
        self.assertEqual([e for e in entries if e["action"] in ("removed", "partial")], [])

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
        self.assertIn("unknown", row["detail"])

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

    def test_an_unreadable_radar_top_is_a_doubt(self):
        _sans_processus(self)
        self.ended_lot()
        radar = os.path.join(self.cfg.state_dir, "radar")
        os.makedirs(radar)
        real_stat = clones.os.stat

        def fake_stat(path, *args, **kwargs):
            if os.path.abspath(os.fspath(path)) == os.path.abspath(radar):
                raise PermissionError(13, "permission denied")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(clones.os, "stat", side_effect=fake_stat):
            copies, complete, why = clones._dependency_inventory(
                self.cfg, (self.root,), self.agents, ())
        self.assertFalse(complete)
        self.assertIn("unreadable top", why)
        self.assertIn("radar", why)

    def test_a_lot_named_only_by_a_lot_branch_is_attributed(self):
        _sans_processus(self)
        lot = self.ended_lot()
        path = self.clone("adhoc")
        _git("checkout", "-q", "-b", "lot/%s-branch" % lot, cwd=path)
        row = self.plan_for(path)
        self.assertEqual(row["action"], "retrait")
        self.assertEqual(row["lot"], str(lot))


    def test_a_branch_slug_date_is_not_a_lot_id(self):
        _sans_processus(self)
        lot = self.ended_lot()
        path = self.clone("adhoc-date")
        _git("checkout", "-q", "-b", "lot/%s-roadmap-14-10" % lot, cwd=path)
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

    def test_a_secret_on_the_holder_argv_stays_out_of_the_journal(self):
        """#3403 (témoin) : l'entrée « signaled » du journal partagé nomme le
        détenteur par pid et base d'argv[0], jamais par son argv."""
        _sans_processus(self)
        secret = "s3cr3t-3403"
        lot = self.ended_lot()
        path = self.clone("copy-%s-x" % lot)
        argv = ["/usr/bin/psql", "postgresql://u:%s@db/x" % secret,
                "--resume", "sess-" + secret]
        found = [process._verdict(42, "working directory", argv, None, ())]
        with mock.patch.object(platform, "holders", return_value=found):
            entries, plan, skipped = self.reap(dry_run=False)
        signaled = [e for e in entries if e["path"] == path]
        self.assertEqual([e["action"] for e in signaled], ["signaled"])
        self.assertIn("processes inside (42 psql)", signaled[0]["detail"])
        self.assertNotIn(secret, repr(entries))
        self.assertNotIn(secret, repr(plan))
        self.assertTrue(os.path.isdir(path))

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


class SeparateGitDirTest(unittest.TestCase):
    def test_shared_copy_with_a_separate_gitdir_is_a_dependent(self):
        import tempfile
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "work")
            os.mkdir(root)
            source = _repo(tmp)
            other = os.path.join(root, "other")
            metadata = os.path.join(tmp, "metadata")
            subprocess.run(["git", "clone", "-q", "--shared",
                            "--separate-git-dir=" + metadata, source, other],
                           check=True, capture_output=True)
            cfg = SimpleNamespace(state_dir=os.path.join(tmp, "state"))
            copies, complete, why = clones._dependency_inventory(
                cfg, (root,), {}, ())
            self.assertTrue(complete, why)
            self.assertIn((other, os.path.join(metadata, "objects")), copies)
            dependents, doubt = clones._dependents(copies, [source])
            self.assertEqual(doubt, "")
            self.assertIn(other, dependents[source])
            with mock.patch.object(hk, "worktree_holders", return_value=(False, "")), \
                    mock.patch.object(hk, "_git_copy_work", return_value=""):
                self.assertIn("alternate dependent", clones._removal_check(
                    source, cfg, (), (), (root,), {}))


class NamedLotsTest(unittest.TestCase):
    def test_the_agent_number_is_not_a_lot_when_a_longer_number_is_named(self):
        agents = {"deepseek4", "mty-amh-impl-04", "a1"}
        self.assertEqual(clones.named_lots("ameesh-deepseek4-1666", agents), [1666])
        self.assertEqual(clones.named_lots("mty-amh-impl-04-2162", agents), [2162])
        self.assertEqual(clones.named_lots("ds4-749-review", agents), [749])
        self.assertEqual(clones.named_lots("ds6-2181-2176", agents), [2181, 2176])
        self.assertEqual(clones.named_lots("nexlink-665-reseed", agents), [665])
        self.assertEqual(clones.named_lots("a1", agents), [])
        self.assertEqual(clones.named_lots("ameesh-deepseek4", agents), [])
        # a version is not a lot
        self.assertEqual(clones.named_lots("nexlink-0.2.10", agents), [])
        self.assertEqual(clones.named_lots("foo-2026.10.19-665", agents), [665])

    def test_a_sha_fragment_is_not_a_lot(self):
        """#3266 (relecture) : un nombre collé à des lettres est un fragment
        de SHA, pas un lot ; un nombre nommé par un marqueur (`wt3220`,
        `.work-1392`) reste un lot."""
        agents = {"deepseek4", "mty-amh-impl-04", "a1"}
        self.assertEqual(clones.named_lots("review-3220-ab1234cd", agents), [3220])
        self.assertEqual(clones.named_lots("wt3220-ca47", agents), [3220])
        self.assertEqual(clones.named_lots("copy-1234abcd", agents), [])
        self.assertEqual(clones.named_lots("wt3220", agents), [3220])
        self.assertEqual(clones.named_lots(".work-1392", agents), [1392])
        self.assertEqual(clones.named_lots("wt-432-433", agents), [432, 433])

    def test_a_diag_directory_names_its_lot_and_a_date_names_none(self):
        """#3277 : `diag462` (tmp d'agent) nomme #462 ; une date n'est jamais
        un lot, collée à `diag` ou non."""
        agents = {"codex2", "a1"}
        self.assertEqual(clones.named_lots("diag462", agents), [462])
        self.assertEqual(clones.named_lots("diag462-fix", agents), [462])
        self.assertEqual(clones.named_lots("pre-diag462", agents), [462])
        self.assertEqual(clones.named_lots("diagnostic462", agents), [])
        self.assertEqual(clones.named_lots("diag2024-10-24", agents), [])
        self.assertEqual(clones.named_lots("diag462-20261024", agents), [462])
        self.assertEqual(clones.named_lots("diag462-2026-10-24", agents), [462])
        self.assertEqual(clones.named_lots("wt3220-20261024", agents), [3220])
        self.assertEqual(clones.named_lots("diag462-463", agents), [462, 463])


if __name__ == "__main__":
    unittest.main()
