# SPDX-License-Identifier: AGPL-3.0-only
"""L230 (#247): the housekeeping pass deletes the regenerable build dirs
(node_modules, Rust target) of idle worktrees, never with a process inside,
and removes after 24 h a worktree kept although its lot is merged. Offline:
temporary git trees, a fake /proc, a fake store."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import build_artifacts as ba
from ameesh import menage

DAY = 24 * 3600.0
#: a ctime cannot be set back: the tests move "now" forward instead
LATER = 2 * DAY
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")


def _git(*args: str, cwd: str) -> str:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                          check=True, env=GIT_ENV).stdout


def _write(path: str, size: int = 4096) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"z" * size)


def _age(top: str, seconds: float) -> None:
    """Every mtime under `top` (links included, not followed) pushed back."""
    moment = time.time() - seconds
    for current, dirs, files in os.walk(top):
        for name in dirs + files:
            os.utime(os.path.join(current, name), (moment, moment), follow_symlinks=False)
    os.utime(top, (moment, moment))


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ameesh-l230-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.proc = os.path.join(self.base, "proc")
        os.makedirs(self.proc)

    def fake_process(self, pid: int, *, cwd: str = "/", fds=(), maps=(), comm: str = "bash",
                     unreadable: bool = False) -> None:
        d = os.path.join(self.proc, str(pid))
        os.makedirs(os.path.join(d, "fd"))
        with open(os.path.join(d, "comm"), "w") as fh:
            fh.write(comm + "\n")
        os.symlink(cwd, os.path.join(d, "cwd"))
        os.symlink("/usr/bin/" + comm, os.path.join(d, "exe"))
        os.symlink("/", os.path.join(d, "root"))
        for i, target in enumerate(fds):
            os.symlink(target, os.path.join(d, "fd", str(i)))
        with open(os.path.join(d, "maps"), "w") as fh:
            for path in maps:
                fh.write("7f00-7f01 r-xp 00000000 00:00 1   %s\n" % path)
        if unreadable:
            os.chmod(os.path.join(d, "fd"), 0)
            self.addCleanup(os.chmod, os.path.join(d, "fd"), 0o700)

    def worktree(self, name: str = "wt") -> str:
        """An idle worktree: sources, node_modules (two levels), a Rust
        target beside a Cargo.toml, a `target` that is not Rust."""
        wt = os.path.join(self.base, name)
        _write(os.path.join(wt, ".git"), 0)
        _write(os.path.join(wt, "src", "app.ts"))
        _write(os.path.join(wt, "node_modules", "left-pad", "index.js"), 8192)
        _write(os.path.join(wt, "web", "node_modules", "react", "index.js"), 8192)
        _write(os.path.join(wt, "desktop", "host", "Cargo.toml"))
        _write(os.path.join(wt, "desktop", "host", "target", "debug", "app"), 16384)
        _write(os.path.join(wt, "docs", "target", "keep.md"))
        return wt

    def go(self, wt: str, **kw) -> dict:
        args = dict(rules=ba.rules_of(None), idle_s=DAY, protected=(), dry_run=False,
                    now=time.time() + LATER, proc=self.proc)
        args.update(kw)
        return ba.run([{"path": wt}], **args)


class RulesTest(unittest.TestCase):
    def test_rules_and_policy(self):
        self.assertEqual([str(r) for r in ba.rules_of(None)], ["node_modules", "target:Cargo.toml"])
        self.assertEqual(ba.rules_of([]), ())
        for bad in ("node_modules", [".."], ["a/b"], ["target:../x"], [3]):
            self.assertIsNone(ba.parse_rules(bad)[0], bad)
        parsed, problems, unknown = menage.parse_policy(
            {"artifacts": ["node_modules", ".venv"], "artifact_idle": "36h"})
        self.assertEqual((parsed, problems, unknown), (
            {"artifacts": ("node_modules", ".venv"), "artifact_idle": 36 * 3600.0}, [], []))
        self.assertEqual(len(menage.parse_policy({"artifacts": "x", "artifact_idle": "?"})[1]), 2)

    def test_effective_keeps_the_longest_idle(self):
        def canon(ident, declared):
            fiche = SimpleNamespace(policy=SimpleNamespace(housekeeping=declared))
            return SimpleNamespace(id=ident, host=lambda _h: fiche)
        policy = menage.effective([canon("a", {"artifact_idle": DAY}),
                                   canon("b", {"artifact_idle": 2 * DAY, "artifacts": ()})], "pc")
        self.assertEqual((policy.artifact_idle, policy.artifacts), (2 * DAY, ()))
        self.assertEqual(menage.Policy().artifact_idle, DAY)


class ProcScanTest(_Base):
    def test_cwd_fd_maps_and_doubt(self):
        wt = self.worktree()
        self.fake_process(10, cwd=os.path.join(wt, "src"))
        self.fake_process(11, fds=[os.path.join(wt, "node_modules", "x") + " (deleted)",
                                   "socket:[1]", "pipe:[2]"])
        self.fake_process(12, maps=[os.path.join(wt, "desktop/host/target/debug/libx.so")])
        self.fake_process(13, cwd=self.base)
        scan = ba.scan_processes(self.proc)
        self.assertEqual(scan.holders(wt), ["10 bash", "11 bash", "12 bash"])
        self.assertEqual(scan.holders(os.path.join(wt, "web")), [])
        self.assertEqual(scan.doubt(), "")

    @unittest.skipIf(os.getuid() == 0, "root reads everything")
    def test_unreadable_process_is_a_doubt_except_the_service_manager(self):
        self.fake_process(20, comm="systemd", unreadable=True)
        self.assertEqual(ba.scan_processes(self.proc).doubt(), "")
        self.fake_process(21, comm="node", unreadable=True)
        self.assertIn("21 node", ba.scan_processes(self.proc).doubt())
        self.assertIn("/proc unreadable", ba.scan_processes(os.path.join(self.base, "no")).doubt())


class SurveyTest(_Base):
    def test_artifacts_found_and_not_counted_as_activity(self):
        wt = self.worktree()
        os.makedirs(os.path.join(self.base, "cache", "nm", "k", "node_modules"))
        os.symlink(os.path.join(self.base, "cache", "nm", "k", "node_modules"),
                   os.path.join(wt, "src", "node_modules"))
        _write(os.path.join(wt, "nested", ".git"), 0)
        _write(os.path.join(wt, "nested", "node_modules", "y.js"))
        # an install inside an artifact is not activity of the worktree
        future = time.time() + 10 * DAY
        _write(os.path.join(wt, "node_modules", "fresh.js"))
        for rel in ("node_modules/fresh.js", "node_modules"):
            os.utime(os.path.join(wt, rel), (future, future))
        found = ba.survey(wt, ba.rules_of(None))
        self.assertTrue(found["complete"])
        self.assertEqual(sorted(os.path.relpath(p, wt) for p in found["artifacts"]),
                         ["desktop/host/target", "node_modules", "web/node_modules"])
        self.assertLess(found["latest"], time.time() + 60)
        os.utime(os.path.join(wt, "src", "app.ts"), (future, future))
        self.assertEqual(ba.survey(wt, ba.rules_of(None))["latest"], future)

    def test_git_index_and_too_many_entries(self):
        wt = self.worktree()
        gitdir = os.path.join(self.base, "repo.git", "worktrees", "wt")
        _write(os.path.join(gitdir, "index"))
        with open(os.path.join(wt, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % gitdir)
        future = time.time() + DAY
        os.utime(os.path.join(gitdir, "index"), (future, future))
        self.assertEqual(ba.survey(wt, ba.rules_of(None))["latest"], future)
        self.assertFalse(ba.survey(wt, ba.rules_of(None), limit=3)["complete"])


class RunTest(_Base):
    def test_idle_worktree_artifacts_deleted_and_logged(self):
        wt = self.worktree()
        out = self.go(wt, owner_of=lambda p: "dev2")
        gone = sorted(os.path.relpath(e["path"], wt) for e in out["entries"])
        self.assertEqual(gone, ["desktop/host/target", "node_modules", "web/node_modules"])
        for rel in gone:
            self.assertFalse(os.path.lexists(os.path.join(wt, rel)))
        self.assertTrue(os.path.exists(os.path.join(wt, "docs", "target", "keep.md")))
        self.assertTrue(os.path.exists(os.path.join(wt, "src", "app.ts")))
        self.assertTrue(all(e["kind"] == "artifact" and e["action"] == "deleted"
                            and e["bytes"] > 0 and e["agent"] == "dev2" for e in out["entries"]))

    def test_dry_run_lists_with_size_and_deletes_nothing(self):
        wt = self.worktree()
        out = self.go(wt, dry_run=True)
        self.assertEqual(out["entries"], [])
        self.assertEqual(len(out["plan"]), 3)
        self.assertTrue(all(p["bytes"] > 0 and p["action"] == "suppression" for p in out["plan"]))
        self.assertTrue(os.path.isdir(os.path.join(wt, "node_modules")))

    def test_shared_link_and_read_only_dirs(self):
        wt = self.worktree()
        shared = os.path.join(self.base, "cache", "nm", "k", "node_modules")
        _write(os.path.join(shared, "dep.js"))
        os.symlink(shared, os.path.join(wt, "src", "node_modules"))
        ro = os.path.join(wt, "node_modules", "left-pad")
        os.chmod(ro, 0o500)
        self.addCleanup(lambda: os.path.isdir(ro) and os.chmod(ro, 0o700))
        out = self.go(wt)
        self.assertFalse(os.path.lexists(os.path.join(wt, "node_modules")))
        self.assertTrue(os.path.islink(os.path.join(wt, "src", "node_modules")))
        self.assertTrue(os.path.exists(os.path.join(shared, "dep.js")))
        self.assertEqual(len(out["entries"]), 3)

    def test_kept_when_active_held_protected_locked_or_in_doubt(self):
        wt = self.worktree()
        out = self.go(wt, now=time.time() + 3600)   # last activity an hour ago
        self.assertEqual(out["entries"], [])
        self.assertIn("1 worktree(s) active", " ".join(out["skipped"]))
        self.fake_process(30, cwd=os.path.join(wt, "web"), comm="node")
        out = self.go(wt)
        self.assertEqual(out["entries"], [])
        self.assertIn("process inside (30 node)", " ".join(out["skipped"]))
        shutil.rmtree(os.path.join(self.proc, "30"))
        self.assertEqual(self.go(wt, protected=[os.path.join(wt, "src")])["entries"], [])
        self.assertEqual(ba.run([{"path": wt, "locked": True}], ba.rules_of(None), idle_s=DAY,
                                protected=(), dry_run=False, now=time.time() + LATER,
                                proc=self.proc)["entries"], [])
        self.assertEqual(self.go(wt, rules=())["entries"], [])
        self.assertTrue(os.path.isdir(os.path.join(wt, "node_modules")))

    def test_process_arriving_before_deletion_stops_it(self):
        wt = self.worktree()
        real = ba.scan_processes
        calls = []

        def scan(proc="/proc"):
            calls.append(proc)
            if len(calls) == 2:
                self.fake_process(40, cwd=wt, comm="npm")
            return real(proc)

        with mock.patch.object(ba, "scan_processes", side_effect=scan):
            out = self.go(wt)
        self.assertEqual(out["entries"], [])
        self.assertIn("40 npm", " ".join(out["skipped"]))

    def test_time_budget_leaves_the_rest(self):
        wt = self.worktree()
        ticks = iter([0.0] + [10 ** 6] * 10)
        out = self.go(wt, budget_s=1.0, clock=lambda: next(ticks))
        self.assertEqual(out["entries"], [])
        self.assertEqual({p["action"] for p in out["plan"]}, {"remis"})
        self.assertIn("3 left for the next pass", " ".join(out["skipped"]))

    def test_remove_artifact_refuses_links_and_outside(self):
        wt = self.worktree()
        os.symlink(os.path.join(wt, "node_modules"), os.path.join(wt, "link"))
        with self.assertRaises(ValueError):
            ba.remove_artifact(os.path.join(wt, "link"), wt)
        with self.assertRaises(ValueError):
            ba.remove_artifact(os.path.join(wt, "node_modules"), os.path.join(wt, "src"))
        self.assertTrue(os.path.isdir(os.path.join(wt, "node_modules")))

    def test_gently_runs_and_reraises(self):
        self.assertEqual(ba.gently(lambda x: x + 1, 1), 2)
        with self.assertRaises(KeyError):
            ba.gently(lambda: {}["x"])


class _Repo(_Base):
    def setUp(self) -> None:
        super().setUp()
        origin = os.path.join(self.base, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True,
                       env=GIT_ENV)
        self.main = os.path.join(self.base, "main")
        subprocess.run(["git", "clone", "-q", origin, self.main], check=True,
                       capture_output=True, env=GIT_ENV)
        _git("commit", "-q", "--allow-empty", "-m", "root", cwd=self.main)
        _git("push", "-q", "origin", "main", cwd=self.main)

    def add_worktree(self, branch: str) -> str:
        path = os.path.join(self.base, branch)
        _git("worktree", "add", "-q", "-b", branch, path, cwd=self.main)
        _write(os.path.join(path, "f.txt"))
        _git("add", "f.txt", cwd=path)
        _git("commit", "-q", "-m", branch, cwd=path)
        _git("push", "-q", "origin", branch, cwd=path)
        return path


class WorktreesTest(_Repo):
    def test_main_checkout_never_listed(self):
        wt = self.add_worktree("lot-1")
        found = ba.worktrees([self.main, wt, os.path.join(self.base, "nope")],
                             menage.list_worktrees)
        self.assertEqual([os.path.realpath(w["path"]) for w in found], [os.path.realpath(wt)])


class _Store:
    def __init__(self, rows, lots):
        self.rows, self.lots, self.statuses = rows, lots, []
        self.housekeeping = self
        self.work = SimpleNamespace(get=lambda ident: {"state": self.lots.get(ident)})

    def worktrees(self, host, statuses=None, limit=500):
        return [r for r in self.rows if r["status"] in (statuses or (r["status"],))]

    def set_worktree_status(self, ident, status, detail):
        self.statuses.append((ident, status, detail))


class MergedKeptTest(_Repo):
    def row(self, path, *, checked_ago, detail=None, lot="7"):
        st = menage.worktree_state(path)
        return {"id": 1, "path": path, "agent": "dev2", "lot": lot, "repo": self.main,
                "status": "kept", "created_ts": time.time() - 3 * DAY,
                "checked_ts": time.time() - checked_ago,
                "detail": detail if detail is not None else
                "lot #%s merged : gardé — %s" % (lot, st["reason"])}

    def run_pass(self, rows, lots, *, dry_run=False):
        store = _Store(rows, lots)
        _age(rows[0]["path"], 2 * 3600)
        with mock.patch.object(menage.storage, "of", return_value=store), \
                mock.patch.object(menage, "worktree_in_use", return_value=False):
            entries, plan = menage._worktree_pass(None, "pc", dry_run=dry_run,
                                                  include_lotless=False, protected=set(),
                                                  now=time.time())
        return store, entries, plan

    def test_pushed_not_merged_removed_after_a_day_branch_kept(self):
        wt = self.add_worktree("lot-7")
        _store, _e, plan = self.run_pass([self.row(wt, checked_ago=DAY - 60)], {7: "merged"})
        self.assertEqual(plan[0]["action"], "garde")
        _store, _e, plan = self.run_pass([self.row(wt, checked_ago=DAY + 60)], {7: "merged"},
                                         dry_run=True)
        self.assertEqual(plan[0]["action"], "retrait")
        self.assertTrue(os.path.isdir(wt))
        store, entries, _plan = self.run_pass([self.row(wt, checked_ago=DAY + 60)], {7: "merged"})
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(store.statuses[0][1], "removed")
        self.assertIn("propre et poussé", entries[0]["detail"])
        self.assertIn("lot-7", _git("branch", "--list", "lot-7", cwd=self.main))

    def test_closed_dirty_unpushed_or_changed_reason_stay_kept(self):
        wt = self.add_worktree("lot-8")
        self.assertEqual(self.run_pass([self.row(wt, checked_ago=2 * DAY, lot="8")],
                                       {8: "closed"})[2][0]["action"], "garde")
        self.assertEqual(self.run_pass([self.row(wt, checked_ago=2 * DAY, lot="8",
                                                 detail="lot #8 merged : gardé — autre")],
                                       {8: "merged"})[2][0]["action"], "garde")
        _write(os.path.join(wt, "g.txt"))
        _git("add", "g.txt", cwd=wt)
        _git("commit", "-q", "-m", "local only", cwd=wt)
        self.assertEqual(self.run_pass([self.row(wt, checked_ago=2 * DAY, lot="8")],
                                       {8: "merged"})[2][0]["action"], "garde")
        _git("push", "-q", "origin", "lot-8", cwd=wt)
        _write(os.path.join(wt, "dirty.txt"))
        self.assertEqual(self.run_pass([self.row(wt, checked_ago=2 * DAY, lot="8")],
                                       {8: "merged"})[2][0]["action"], "garde")
        self.assertTrue(os.path.isdir(wt))

    def test_integrated_worktree_removed_with_its_merged_branch(self):
        wt = self.add_worktree("lot-9")
        _git("merge", "-q", "--ff-only", "origin/lot-9", cwd=self.main)
        _git("push", "-q", "origin", "main", cwd=self.main)
        _git("fetch", "-q", "origin", cwd=wt)
        row = self.row(wt, checked_ago=0, lot="9")
        row["status"] = "active"
        _store, entries, _plan = self.run_pass([row], {9: "merged"})
        self.assertFalse(os.path.exists(wt))
        self.assertIn("branch lot-9 deleted (merged)", entries[0]["detail"])
        self.assertEqual(_git("branch", "--list", "lot-9", cwd=self.main), "")


class ArtifactPassTest(_Repo):
    def test_pass_covers_agents_repositories_and_owner(self):
        wt = self.add_worktree("lot-5")
        _write(os.path.join(wt, "node_modules", "a.js"))
        _write(os.path.join(self.main, "node_modules", "main.js"))
        store = _Store([], {})
        with mock.patch.object(menage.storage, "of", return_value=store), \
                mock.patch.object(ba, "scan_processes", return_value=ba.ProcScan()):
            out = menage._artifact_pass(None, "pc", menage.Policy(),
                                        cwd_of={"dev2": self.main}, dry_run=False,
                                        now=time.time() + LATER)
        self.assertEqual([(os.path.realpath(e["path"]), e["agent"]) for e in out["entries"]],
                         [(os.path.realpath(os.path.join(wt, "node_modules")), None)])
        self.assertTrue(os.path.exists(os.path.join(self.main, "node_modules", "main.js")))


if __name__ == "__main__":
    unittest.main()
