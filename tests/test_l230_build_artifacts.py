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
from ameesh import housekeeping_discovery as hd
from ameesh import menage, platform
from ameesh.platform import process

DAY = 24 * 3600.0
#: a ctime cannot be set back: the tests move "now" forward instead
LATER = 2 * DAY
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")


def _git(*args: str, cwd: str) -> str:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                          check=True, env=GIT_ENV).stdout


def _write(path: str, size: int = 4096, text: str | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(text.encode() if text is not None else b"z" * size)


def _age(top: str, seconds: float) -> None:
    """Every mtime under `top` (links included, not followed) pushed back."""
    moment = time.time() - seconds
    for current, dirs, files in os.walk(top):
        for name in dirs + files:
            os.utime(os.path.join(current, name), (moment, moment), follow_symlinks=False)
    os.utime(top, (moment, moment))


@unittest.skipUnless(platform.is_linux(), "the process table is read from /proc (Linux)")
class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ameesh-l230-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.proc = os.path.join(self.base, "proc")
        os.makedirs(os.path.join(self.proc, "self"))
        patcher = mock.patch.object(process, "PROC", self.proc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_process(self, pid: int, *, cwd: str = "/", fds=(), maps=(), comm: str = "bash",
                     argv0: str | None = None, state: str = "S",
                     threads=(), unreadable: bool = False) -> None:
        d = os.path.join(self.proc, str(pid))
        os.makedirs(os.path.join(d, "fd"))
        _write(os.path.join(d, "comm"), text=comm + "\n")
        _write(os.path.join(d, "cmdline"), text="%s\0--flag\0" % (argv0 or "/usr/bin/" + comm))
        # état du processus (`Z` = zombie, #1678) : /proc/<pid>/stat, champ 3
        _write(os.path.join(d, "stat"),
               text="%d (%s) %s 1 %d 1 0 -1 0 0 0 0 0 0 0 0 0 0 0 0 100\n"
                    % (pid, comm, state, pid))
        # ses fils : le meneur, puis `threads` (tid, état)
        for tid, tstate in ((pid, state),) + tuple(threads):
            _write(os.path.join(d, "task", str(tid), "stat"),
                   text="%d (%s) %s 1 %d\n" % (tid, comm, tstate, pid))
        os.symlink(cwd, os.path.join(d, "cwd"))
        os.symlink("/usr/bin/" + comm, os.path.join(d, "exe"))
        os.symlink("/", os.path.join(d, "root"))
        for i, target in enumerate(fds):
            os.symlink(target, os.path.join(d, "fd", str(i)))
        _write(os.path.join(d, "maps"),
               text="".join("7f00-7f01 r-xp 00000000 00:00 1   %s\n" % p for p in maps))
        if unreadable:
            os.chmod(os.path.join(d, "fd"), 0)
            self.addCleanup(os.chmod, os.path.join(d, "fd"), 0o700)

    def worktree(self, name: str = "wt") -> str:
        """A worktree: sources, node_modules (two levels), a Rust target
        beside a Cargo.toml, a `target` that is not Rust."""
        wt = os.path.join(self.base, name)
        _write(os.path.join(wt, ".git"), 0)
        _write(os.path.join(wt, "src", "app.ts"))
        _write(os.path.join(wt, "node_modules", "left-pad", "index.js"), 8192)
        _write(os.path.join(wt, "web", "node_modules", "react", "index.js"), 8192)
        _write(os.path.join(wt, "desktop", "host", "Cargo.toml"))
        _write(os.path.join(wt, "desktop", "host", "target", "debug", "app"), 16384)
        _write(os.path.join(wt, "docs", "target", "keep.md"))
        return wt

    @property
    def journal_path(self) -> str:
        return os.path.join(self.base, "state", "artifacts.journal")

    def journal(self) -> list[dict]:
        return ba.Journal(self.journal_path).records()

    def trash(self, wt: str) -> list[str]:
        return [n for n in os.listdir(wt) if n.startswith(ba.TRASH_PREFIX)]

    def root(self, wt: str) -> tuple:
        return os.path.realpath(wt), ba.identity(os.path.realpath(wt))

    def remove(self, artifact: str, wt: str, **kw) -> dict:
        kw.setdefault("rules", ba.rules_of(None))
        return ba.remove_artifact(artifact, wt, root=self.root(wt),
                                  journal=ba.Journal(self.journal_path), **kw)

    def go(self, wt: str, **kw) -> dict:
        tracked = kw.pop("tracked", "")
        args = dict(rules=ba.rules_of(None), idle_s=DAY, protected=(), dry_run=False,
                    now=time.time() + LATER, journal_path=self.journal_path)
        args.update(kw)
        with mock.patch.object(ba, "tracked", return_value=tracked):
            return ba.run([{"path": wt}], **args)


class RulesTest(unittest.TestCase):
    def test_rules_and_policy(self):
        self.assertEqual([str(r) for r in ba.rules_of(None)], ["node_modules", "target:Cargo.toml"])
        self.assertEqual(ba.rules_of([]), ())
        for bad in ("node_modules", [".."], ["a/b"], ["target:../x"], [3], [".ameesh-trash-x"]):
            self.assertIsNone(ba.parse_rules(bad)[0], bad)
        parsed, problems, unknown = menage.parse_policy(
            {"artifacts": ["node_modules", ".venv"], "artifact_idle": "36h",
             "artifact_ended_idle": "2h", "unreadable_ok": ["btop"]})
        self.assertEqual((parsed, problems, unknown), (
            {"artifacts": ("node_modules", ".venv"), "artifact_idle": 36 * 3600.0,
             "artifact_ended_idle": 2 * 3600.0, "unreadable_ok": ("btop",)}, [], []))
        self.assertEqual(len(menage.parse_policy({"artifacts": "x", "artifact_idle": "?",
                                                  "unreadable_ok": "btop"})[1]), 3)
        self.assertEqual(menage.Policy().unreadable_ok, ())

    def test_target_always_needs_a_cargo_toml(self):
        # (g) not bypassable by the policy
        rules, problem = ba.parse_rules(["target"])
        self.assertIsNone(rules)
        self.assertIn("Cargo.toml", problem)
        self.assertIsNone(ba.parse_rules(["target:Makefile"])[0])

    def test_effective_keeps_the_longest_idle(self):
        def canon(ident, declared):
            fiche = SimpleNamespace(policy=SimpleNamespace(housekeeping=declared))
            return SimpleNamespace(id=ident, host=lambda _h: fiche)
        policy = menage.effective([canon("a", {"artifact_idle": DAY,
                                               "artifact_ended_idle": 3600.0}),
                                   canon("b", {"artifact_idle": 2 * DAY,
                                               "artifact_ended_idle": 2 * 3600.0,
                                               "artifacts": ()})], "pc")
        self.assertEqual((policy.artifact_idle, policy.artifact_ended_idle,
                          policy.artifacts), (2 * DAY, 2 * 3600.0, ()))
        self.assertEqual(menage.Policy().artifact_idle, DAY)
        self.assertEqual(menage.Policy().artifact_ended_idle, ba.DEFAULT_ENDED_IDLE_S)


class UserPathsTest(_Base):
    def test_cwd_fd_maps_and_doubt(self):
        wt = self.worktree()
        self.fake_process(10, cwd=os.path.join(wt, "src"))
        self.fake_process(11, fds=[os.path.join(wt, "node_modules", "x") + " (deleted)",
                                   "socket:[1]", "pipe:[2]"])
        self.fake_process(12, maps=[os.path.join(wt, "desktop/host/target/debug/libx.so")])
        self.fake_process(13, cwd=self.base)
        scan = platform.user_paths()
        self.assertEqual(scan.holders(wt), ["10 bash", "11 bash", "12 bash"])
        self.assertEqual(scan.holders(os.path.join(wt, "web")), [])
        self.assertEqual(scan.doubt(), "")

    @unittest.skipIf(os.getuid() == 0, "root reads everything")
    def test_unreadable_process_is_a_doubt_unless_allowed(self):
        self.fake_process(20, comm="systemd", argv0="/usr/lib/systemd/systemd", unreadable=True)
        self.assertEqual(platform.user_paths().doubt(), "")
        self.fake_process(21, comm="btop", unreadable=True)
        self.assertIn("21 btop", platform.user_paths().doubt())
        allowed = platform.user_paths(allow=["btop"])
        self.assertEqual((allowed.doubt(), allowed.allowed), ("", ["21 btop"]))
        # the name AND the command line must both be allowed
        self.fake_process(22, comm="btop", argv0="/tmp/evil", unreadable=True)
        self.assertIn("22 btop", platform.user_paths(allow=["btop"]).doubt())
        with mock.patch.object(process, "PROC", os.path.join(self.base, "none")):
            self.assertIn("unreadable", platform.user_paths().doubt())

    @unittest.skipIf(os.getuid() == 0, "root reads everything")
    def test_3195_r2_what_was_read_before_a_refusal_is_kept(self):
        """#3195 R2 (cx1, cx2 witness): a process let through by its name,
        its cwd readable in a worktree but its descriptors refused, still
        holds that worktree: an observation is never dropped for what could
        not be read after it."""
        wt = self.worktree()
        self.fake_process(23, comm="btop", cwd=os.path.join(wt, "node_modules"),
                          unreadable=True)
        scan = platform.user_paths(allow=["btop"])
        self.assertEqual((scan.doubt(), scan.allowed), ("", ["23 btop"]))
        self.assertEqual(scan.holders(wt), ["23 btop"])
        out = self.go(wt, allow=["btop"])
        self.assertEqual(out["entries"], [])
        self.assertIn("process inside (23 btop)", " ".join(out["skipped"]))

    @unittest.skipIf(os.getuid() == 0, "root reads everything")
    def test_a_zombie_is_not_a_doubt(self):
        """#1678 : un zombie (état Z) n'est plus qu'une entrée de table — ses
        liens /proc illisibles ne sont pas un doute (constat #1665 :
        « timeout <defunct> » retenait tout le ménage)."""
        self.fake_process(30, comm="timeout", state="Z", unreadable=True)
        scan = platform.user_paths()
        self.assertEqual((scan.doubt(), scan.unreadable, scan.paths), ("", [], {}))
        # l'état seul décide : le même processus vivant reste un doute
        self.fake_process(31, comm="timeout", unreadable=True)
        self.assertEqual(platform.user_paths().doubt(),
                         "process(es) of the user unreadable: 31 timeout")

    @unittest.skipIf(os.getuid() == 0, "root reads everything")
    def test_a_zombie_leader_with_a_live_thread_is_still_a_doubt(self):
        """Un meneur sorti (`Z`) dont un fil vit encore détient toujours ses
        fichiers : il reste un doute, il ne libère pas le ménage."""
        self.fake_process(33, comm="worker", state="Z", threads=((34, "S"),),
                          unreadable=True)
        self.assertEqual(platform.user_paths().doubt(),
                         "process(es) of the user unreadable: 33 worker")

    @unittest.skipIf(os.getuid() == 0, "root reads everything")
    def test_a_zombie_does_not_hold_the_housekeeping(self):
        """#1665 : l'artefact inactif est retiré malgré le zombie présent."""
        wt = self.worktree()
        self.fake_process(32, comm="timeout", state="Z", unreadable=True)
        out = self.go(wt)
        self.assertEqual(len(out["entries"]), 3)

    @unittest.skipIf(os.getuid() == 0, "root reads everything")
    def test_allowed_process_is_logged_and_does_not_block(self):
        wt = self.worktree()
        self.fake_process(23, comm="btop", unreadable=True)
        self.assertEqual(self.go(wt)["entries"], [])
        out = self.go(wt, allow=("btop",))
        self.assertEqual(len(out["entries"]), 3)
        self.assertIn("23 btop", " ".join(out["skipped"]))
        self.assertEqual(out["entries"][0]["data"]["unreadable_allowed"], ["23 btop"])


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

    def test_git_index_too_many_entries_and_budget(self):
        wt = self.worktree()
        gitdir = os.path.join(self.base, "repo.git", "worktrees", "wt")
        _write(os.path.join(gitdir, "index"))
        with open(os.path.join(wt, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % gitdir)
        future = time.time() + DAY
        os.utime(os.path.join(gitdir, "index"), (future, future))
        self.assertEqual(ba.survey(wt, ba.rules_of(None))["latest"], future)
        self.assertFalse(ba.survey(wt, ba.rules_of(None), limit=3)["complete"])
        # (h) the deadline is checked inside the walk
        spent = ba.Budget(0.0, clock=iter([0.0, 1.0, 1.0, 1.0]).__next__)
        self.assertIn("time budget", ba.survey(wt, ba.rules_of(None), budget=spent)["why"])

    def test_reflog_last_entry_dates_activity_not_the_file_mtime(self):
        """#4685 : `git gc` réécrit logs/HEAD sans changer les entrées ; seule
        la dernière entrée date une opération, pas la mtime du fichier."""
        wt = self.worktree()
        gitdir = os.path.join(self.base, "repo.git", "worktrees", "wt")
        _write(os.path.join(gitdir, "index"))
        with open(os.path.join(wt, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % gitdir)
        reflog = os.path.join(gitdir, "logs", "HEAD")
        old = time.time() - 10 * DAY
        entry = "%s %s t <t@t> %d +0000\tcommit: old\n" % (
            "0" * 40, "1" * 40, int(old))
        _write(reflog, text=entry)
        # the maintenance rewrites the reflog: fresh mtime, unchanged entry
        now = time.time()
        os.utime(reflog, (now, now))
        self.assertEqual(ba._reflog_latest(reflog), float(int(old)))
        # a real operation is an appended entry, dated now: the survey sees it
        future = time.time() + DAY
        _write(reflog, text=entry + "%s %s t <t@t> %d +0000\treset: moving to HEAD\n" % (
            "1" * 40, "2" * 40, int(future)))
        os.utime(reflog, (old, old))
        self.assertEqual(ba.survey(wt, ba.rules_of(None))["latest"], float(int(future)))
        self.assertIsNone(ba._reflog_latest(os.path.join(gitdir, "logs", "absent")))


class RunTest(_Base):
    def test_idle_worktree_artifacts_deleted_and_logged(self):
        wt = self.worktree()
        out = self.go(wt, owner_of=lambda p: "dev2")
        gone = sorted(os.path.relpath(e["path"], wt) for e in out["entries"])
        self.assertEqual(gone, ["desktop/host/target", "node_modules", "web/node_modules"])
        for rel in gone:
            self.assertFalse(os.path.lexists(os.path.join(wt, rel)))
        self.assertEqual([n for n in os.listdir(wt) if n.startswith(ba.TRASH_PREFIX)], [])
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

    @unittest.skipIf(os.getuid() == 0, "root reads everything")
    def test_3195_dry_run_under_a_doubt_still_lists_the_plan(self):
        wt = self.worktree()
        self.fake_process(23, comm="btop", unreadable=True)
        out = self.go(wt, dry_run=True)
        self.assertEqual(len(out["plan"]), 3)
        self.assertIn("artifacts: --apply would delete nothing, process(es) of the user "
                      "unreadable: 23 btop",
                      out["skipped"])
        out = self.go(wt)
        self.assertEqual((out["entries"], out["plan"]), ([], []))
        self.assertIn("artifacts: nothing deleted, process(es) of the user unreadable: 23 btop",
                      out["skipped"])
        self.assertTrue(os.path.isdir(os.path.join(wt, "node_modules")))

    def test_3195_keep_holds_an_idle_worktree(self):
        wt = self.worktree()
        out = self.go(wt, keep=lambda candidate: "lot #12 qa")
        self.assertEqual((out["entries"], out["plan"]), ([], []))
        self.assertIn("artifacts: 1 idle worktree(s) kept (open lot or turn in progress): "
                      "%s: lot #12 qa" % wt, out["skipped"])

        def unreadable(candidate):
            raise RuntimeError("db down")

        out = self.go(wt, keep=unreadable)
        self.assertEqual(out["entries"], [])
        self.assertIn("open lot not ruled out (RuntimeError)", " ".join(out["skipped"]))
        self.assertTrue(os.path.isdir(os.path.join(wt, "node_modules")))
        self.assertEqual(len(self.go(wt, keep=lambda candidate: "")["entries"]), 3)

    def test_shared_link_and_read_only_dirs(self):
        wt = self.worktree()
        shared = os.path.join(self.base, "cache", "nm", "k", "node_modules")
        _write(os.path.join(shared, "dep.js"))
        os.symlink(shared, os.path.join(wt, "src", "node_modules"))
        os.symlink(shared, os.path.join(wt, "node_modules", "linked"))
        ro = os.path.join(wt, "node_modules", "left-pad")
        os.chmod(ro, 0o500)
        os.chmod(os.path.join(wt, "web", "node_modules", "react"), 0)
        out = self.go(wt)
        self.assertEqual([e["action"] for e in out["entries"]], ["deleted"] * 3)
        self.assertFalse(os.path.lexists(os.path.join(wt, "node_modules")))
        self.assertTrue(os.path.islink(os.path.join(wt, "src", "node_modules")))
        self.assertTrue(os.path.exists(os.path.join(shared, "dep.js")))

    def test_git_dir_other_root_inside_are_left_and_partial(self):
        # (b) a subtree holding a .git, or a managed root, is never deleted
        wt = self.worktree()
        _write(os.path.join(wt, "node_modules", "dep-from-git", ".git", "HEAD"))
        root = os.path.join(wt, "web", "node_modules", "cache-root")
        _write(os.path.join(root, "kept.bin"))
        out = self.go(wt, skip_roots=[root])
        actions = {os.path.relpath(e["path"], wt): e["action"] for e in out["entries"]}
        self.assertEqual(actions.get("node_modules"), "partial")
        self.assertNotIn("web/node_modules", actions)   # holds a managed root: refused
        self.assertIn("holds a managed root", " ".join(out["skipped"]))
        self.assertTrue(os.path.exists(os.path.join(root, "kept.bin")))
        trash = [n for n in os.listdir(wt) if n.startswith(ba.TRASH_PREFIX)]
        self.assertEqual(len(trash), 1)
        self.assertTrue(os.path.exists(os.path.join(wt, trash[0], "dep-from-git", ".git", "HEAD")))
        self.assertFalse(os.path.exists(os.path.join(wt, trash[0], "left-pad")))
        self.assertIn("holds a .git", actions and out["entries"][0]["detail"] + str(
            [e["data"]["left"] for e in out["entries"]]))

    def test_tracked_files_keep_the_artifact(self):
        # (c) a file tracked by git inside: not regenerable
        wt = self.worktree()
        out = self.go(wt, tracked="1 file(s) tracked by git inside (node_modules/patch.js)")
        self.assertEqual(out["entries"], [])
        self.assertIn("tracked by git", " ".join(out["skipped"]))
        self.assertTrue(os.path.isdir(os.path.join(wt, "node_modules")))

    def test_tracked_reads_git(self):
        repo = os.path.join(self.base, "repo")
        os.makedirs(repo)
        _git("init", "-q", cwd=repo)
        _write(os.path.join(repo, "node_modules", "patched.js"))
        _write(os.path.join(repo, "target", "x"))
        _git("add", "node_modules/patched.js", cwd=repo)
        self.assertIn("1 file(s) tracked", ba.tracked(repo, os.path.join(repo, "node_modules")))
        self.assertEqual(ba.tracked(repo, os.path.join(repo, "target")), "")

    def test_errors_make_a_partial_entry(self):
        # (i) never `deleted` when something stayed
        wt = self.worktree()
        real_unlink = os.unlink

        def unlink(name, *args, **kw):
            if name == "index.js" and kw.get("dir_fd") is not None:
                raise PermissionError(13, "Permission denied")
            return real_unlink(name, *args, **kw)

        with mock.patch.object(ba.os, "unlink", side_effect=unlink):
            out = self.go(wt)
        actions = {os.path.relpath(e["path"], wt): e["action"] for e in out["entries"]}
        self.assertEqual(actions, {"node_modules": "partial", "web/node_modules": "partial",
                                   "desktop/host/target": "deleted"})
        self.assertIn("Permission denied", out["entries"][0]["detail"] + out["entries"][1]["detail"])

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
        with mock.patch.object(ba, "tracked", return_value=""):
            self.assertEqual(ba.run([{"path": wt, "locked": True}], ba.rules_of(None),
                                    idle_s=DAY, protected=(), dry_run=False,
                                    now=time.time() + LATER)["entries"], [])
        self.assertEqual(self.go(wt, rules=())["entries"], [])
        self.assertTrue(os.path.isdir(os.path.join(wt, "node_modules")))

    def test_3956_cwd_of_an_agent_is_no_longer_blanket_protected(self):
        """#3956 : le dossier de travail d'un agent n'est plus exempté par son
        seul statut (ses node_modules restaient à vie et ont rempli le
        disque) ; un worktree qui CONTIENT un dossier protégé reste
        intouchable."""
        wt = self.worktree()
        out = self.go(wt, protected=[wt])
        self.assertEqual({os.path.relpath(e["path"], wt) for e in out["entries"]},
                         {"node_modules", "web/node_modules", "desktop/host/target"})
        wt2 = self.worktree("sous")
        self.assertEqual(self.go(wt2, protected=[os.path.join(wt2, "src")])["entries"], [])
        self.assertTrue(os.path.isdir(os.path.join(wt2, "node_modules")))

    def test_3956_artifacts_of_finished_lots_go_earlier(self):
        """#3956 : tous les lots cités finis = plus rien ne s'y reconstruit ;
        inactif depuis 2 h, ses sorties partent dès `artifact_ended_idle`,
        sans attendre l'inactivité de 24 h d'un worktree sans lot fini."""
        wt = self.worktree()
        out = self.go(wt, now=time.time() + 2 * 3600, ended_idle_s=3600,
                      ended_of=lambda _wt: True)
        self.assertEqual(len(out["entries"]), 3)
        self.assertIn("lots finished", out["entries"][0]["detail"])
        other = self.worktree("sans-lot")
        out = self.go(other, now=time.time() + 2 * 3600, ended_idle_s=3600,
                      ended_of=lambda _wt: False)
        self.assertEqual(out["entries"], [])
        self.assertIn("active within", " ".join(out["skipped"]))

    def test_3956_finished_lot_worktrees_are_scanned_first(self):
        """#3956 : une passe coupée par son budget ne doit pas repasser
        toujours les mêmes worktrees ; ceux d'un lot fini passent d'abord, le
        reste (trié par chemin) attend la passe suivante."""
        finished = self.worktree("fini")
        other = self.worktree("autre")
        seen = []

        def survey(path, *_args, **_kw):
            seen.append(path)
            return {"latest": 0.0, "artifacts": [], "complete": True, "why": "",
                    "real": os.path.realpath(path), "identity": None}

        ticks = iter([0.0, 0.0, 10.0 ** 6])
        with mock.patch.object(ba, "survey", side_effect=survey):
            out = ba.run([{"path": other}, {"path": finished}], ba.rules_of(None),
                         idle_s=DAY, ended_idle_s=3600.0,
                         ended_of=lambda wt: wt["path"] == finished,
                         protected=(), dry_run=True, now=time.time(),
                         budget_s=1.0, clock=lambda: next(ticks),
                         journal_path=self.journal_path)
        self.assertEqual(seen, [finished])
        self.assertEqual((out["entries"], out["plan"]), ([], []))
        self.assertIn("time budget", " ".join(out["skipped"]))

    def test_process_arriving_before_the_rename_stops_it(self):
        wt = self.worktree()
        real = platform.user_paths
        calls = []

        def scan(allow=()):
            calls.append(1)
            if len(calls) == 2:
                self.fake_process(40, cwd=wt, comm="npm")
            return real(allow=allow)

        with mock.patch.object(platform, "user_paths", side_effect=scan):
            out = self.go(wt)
        self.assertEqual(out["entries"], [])
        self.assertIn("40 npm", " ".join(out["skipped"]))
        self.assertEqual([n for n in os.listdir(wt) if n.startswith(ba.TRASH_PREFIX)], [])

    def test_cargo_toml_checked_again_right_before_deleting(self):
        # (g) the rule is checked again through the parent descriptor
        wt = self.worktree()
        os.unlink(os.path.join(wt, "desktop", "host", "Cargo.toml"))
        with self.assertRaises(ValueError):
            self.remove(os.path.join(wt, "desktop", "host", "target"), wt)
        self.assertTrue(os.path.isdir(os.path.join(wt, "desktop", "host", "target")))

    def test_rename_then_delete_and_trash_resumed(self):
        # (d) renamed atomically first; (h) a deadline inside the deletion
        # leaves the trash, finished by the next pass whatever the activity
        wt = self.worktree()

        def clock():   # the budget runs out once the first artifact is renamed
            return 10.0 ** 6 if any(n.startswith(ba.TRASH_PREFIX) for n in os.listdir(wt)) \
                else 0.0

        out = self.go(wt, budget_s=1.0, clock=clock)
        self.assertTrue(any(e["action"] == "partial" for e in out["entries"]))
        trash = [n for n in os.listdir(wt) if n.startswith(ba.TRASH_PREFIX)]
        self.assertTrue(trash)
        self.assertFalse(os.path.lexists(os.path.join(wt, "node_modules")))
        out = self.go(wt, now=time.time())   # active now: only the trash goes
        self.assertTrue(all(e["data"]["rule"] == "trash" and e["action"] == "deleted"
                            for e in out["entries"]))
        self.assertEqual([n for n in os.listdir(wt) if n.startswith(ba.TRASH_PREFIX)], [])

    def test_time_budget_leaves_the_rest(self):
        wt = self.worktree()
        ticks = iter([0.0, 0.0] + [10.0 ** 6] * 10 ** 4)   # spent during the first walk
        out = self.go(wt, budget_s=1.0, clock=lambda: next(ticks))
        self.assertEqual(out["entries"], [])
        self.assertIn("time budget", " ".join(out["skipped"]))
        self.assertTrue(os.path.isdir(os.path.join(wt, "node_modules")))

    def test_one_deleting_pass_at_a_time(self):
        # (h) a host-wide flock between the runner and the CLI
        wt = self.worktree()
        lock_path = os.path.join(self.base, "state", "artifacts.lock")
        held = ba._lock(lock_path)
        self.addCleanup(held.close)
        out = self.go(wt, lock_path=lock_path)
        self.assertEqual(out["entries"], [])
        self.assertIn("another pass", " ".join(out["skipped"]))
        held.close()
        self.assertEqual(len(self.go(wt, lock_path=lock_path)["entries"]), 3)

    def test_remove_artifact_refuses_links_and_outside(self):
        wt = self.worktree()
        os.symlink(os.path.join(wt, "node_modules"), os.path.join(wt, "src", "node_modules"))
        with self.assertRaises(ValueError):
            self.remove(os.path.join(wt, "src", "node_modules"), wt)
        with self.assertRaises(ValueError):
            self.remove(os.path.join(wt, "node_modules"), os.path.join(wt, "src"))
        # (a) an ancestor swapped for a link after the plan: the descriptor walk refuses it
        outside = os.path.join(self.base, "outside")
        _write(os.path.join(outside, "node_modules", "precious.js"))
        os.rename(os.path.join(wt, "web"), os.path.join(wt, "web.old"))
        os.symlink(outside, os.path.join(wt, "web"))
        with self.assertRaises(OSError):
            self.remove(os.path.join(wt, "web", "node_modules"), wt)
        self.assertTrue(os.path.exists(os.path.join(outside, "node_modules", "precious.js")))
        self.assertTrue(os.path.isdir(os.path.join(wt, "node_modules")))

    def test_r2_1_root_reopened_must_be_the_surveyed_one(self):
        # r2 (1): the root is opened by its path; an ancestor swapped after
        # the survey must not redirect the deletion
        anc = os.path.join(self.base, "anc")
        wt = self.worktree(os.path.join("anc", "wt"))
        root = self.root(wt)
        fd = os.open(wt, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        self.assertEqual(platform.fd_path(fd), root[0])
        # same inode, another place: the kernel's path of the descriptor differs
        os.rename(anc, anc + ".moved")
        os.symlink(anc + ".moved", anc)
        with self.assertRaisesRegex(ValueError, "now resolves to"):
            ba.remove_artifact(os.path.join(wt, "node_modules"), wt, root=root,
                               journal=ba.Journal(self.journal_path), rules=ba.rules_of(None))
        # another directory behind the same path: another inode
        os.unlink(anc)
        os.makedirs(anc)
        _write(os.path.join(anc, "wt", "node_modules", "precious.js"))
        with self.assertRaisesRegex(ValueError, "no longer the surveyed"):
            ba.remove_artifact(os.path.join(wt, "node_modules"), wt, root=root,
                               journal=ba.Journal(self.journal_path), rules=ba.rules_of(None))
        self.assertTrue(os.path.exists(os.path.join(anc, "wt", "node_modules", "precious.js")))
        self.assertTrue(os.path.isdir(os.path.join(anc + ".moved", "wt", "node_modules")))
        self.assertEqual(self.journal(), [])

    def test_r2_4_only_a_journaled_trash_is_resumed(self):
        # r2 (4): a generated name recorded before the rename; anything else
        # named .ameesh-trash-* is an ordinary directory
        wt = self.worktree()
        _write(os.path.join(wt, ".ameesh-trash-backup", "unique.txt"))
        forged = ".ameesh-trash-1-2-" + "0" * 16
        _write(os.path.join(wt, forged, "unique.txt"))
        real_rename = os.rename
        journaled = []

        def rename(src, dst, **kw):
            journaled.append([os.path.basename(r["trash"]) for r in self.journal()])
            self.assertIn(dst, journaled[-1])
            return real_rename(src, dst, **kw)

        def clock():   # the budget runs out once the first artifact is renamed
            return 10.0 ** 6 if len(self.trash(wt)) > 2 else 0.0

        with mock.patch.object(ba.os, "rename", side_effect=rename):
            out = self.go(wt, budget_s=1.0, clock=clock)
        self.assertEqual(len(journaled), 1)
        self.assertRegex(journaled[0][0], ba.TRASH_RE)
        record, = self.journal()
        self.assertEqual(record["kind"], "artifact")
        self.assertEqual(out["entries"][0]["action"], "partial")
        out = self.go(wt, now=time.time())   # active now: only the journaled trash goes
        self.assertEqual([e["data"]["rule"] for e in out["entries"]], ["trash"])
        self.assertEqual(self.journal(), [])
        self.assertEqual(sorted(self.trash(wt)), sorted([".ameesh-trash-backup", forged]))
        self.assertTrue(os.path.exists(os.path.join(wt, ".ameesh-trash-backup", "unique.txt")))
        self.assertTrue(os.path.exists(os.path.join(wt, forged, "unique.txt")))
        # a record whose directory is not the one renamed (another inode): left alone
        os.rename(os.path.join(wt, forged), os.path.join(wt, forged + "x"))
        os.makedirs(os.path.join(wt, forged))
        _write(os.path.join(wt, forged, "unique.txt"))
        ba.Journal(self.journal_path).add({
            "trash": os.path.join(os.path.realpath(wt), forged), "parent": os.path.realpath(wt),
            "parent_id": list(ba.identity(wt)), "id": [0, 0], "kind": "artifact"})
        out = self.go(wt, now=time.time())
        self.assertEqual(out["entries"], [])
        self.assertIn("not the directory renamed", " ".join(out["skipped"]))
        self.assertTrue(os.path.exists(os.path.join(wt, forged, "unique.txt")))
        self.assertEqual(self.journal(), [])

    def test_r2_5_deadline_and_limit_inside_one_directory(self):
        # r2 (5): checked at every entry, dry-run sizing included
        wt = self.worktree()
        for i in range(40):
            _write(os.path.join(wt, "flat", "f%02d" % i), 1)
            _write(os.path.join(wt, "node_modules", "flat", "f%02d" % i), 4096)
        calls = []

        def clock():
            calls.append(1)
            return 0.0 if len(calls) < 6 else 10.0 ** 6

        found = ba.survey(wt, ba.rules_of(None), budget=ba.Budget(1.0, clock=clock))
        self.assertFalse(found["complete"])
        self.assertIn("time budget", found["why"])
        self.assertLess(len(calls), 10)
        nm = os.path.join(wt, "node_modules")
        whole, complete = ba.size_of(nm)
        self.assertTrue(complete)
        part, complete = ba.size_of(nm, limit=10)
        self.assertFalse(complete)
        self.assertLess(part, whole)
        calls.clear()
        part, complete = ba.size_of(nm, budget=ba.Budget(1.0, clock=clock))
        self.assertFalse(complete)
        self.assertLess(len(calls), 10)
        with mock.patch.object(ba.size_of, "__defaults__", (10, None)):
            out = self.go(wt, dry_run=True)
        self.assertTrue(any("size counted in part" in p["detail"] for p in out["plan"]))

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
        _write(os.path.join(self.main, ".gitignore"), text="node_modules/\n.env\n")
        _git("add", ".gitignore", cwd=self.main)
        _git("commit", "-q", "-m", "ignore", cwd=self.main)
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


class OrphanGuardsTest(unittest.TestCase):
    def test_named_or_recent_worktree_is_kept(self):
        with tempfile.TemporaryDirectory(prefix="ameesh-1665-") as root:
            now = time.time() + 3 * 86400
            for label, branch, owner, state, recent in (
                ("other-active", "lot/7-x", "other", "build", False),
                ("other-closed", "lot/7-x", "other", "closed", False),
                ("other-open-candidate", "review/other-6-7", "other",
                 {7: "closed", 6: "build"}, False),
                ("other-unknown-candidate", "review/other-6-7", "other",
                 {7: "closed", 6: None}, False),
                ("other-closed-candidates", "review/other-6-7", "other",
                 {7: "closed", 6: "merged"}, False),
                ("other-merged-kept", "lot/7-x", "other", "merged", False),
                ("other-merged-kept-recent", "lot/7-x", "other", "merged", True),
                ("other-unpushed-equivalent", "lot/7-x", "other", "closed", False),
                ("unknown", "lot/7-x", None, None, False),
                ("unreadable", "lot/7-x", None, RuntimeError("offline"), False),
                ("named-directory", "", "other", "build", False),
                ("recent-source", "scratch", None, None, True),
                ("orphan", "scratch", None, None, False),
                ("own-closed", "lot/7-x", "a1", "closed", False),
                ("own-active", "lot/7-x", "a1", "build", False),
                ("own-closed-recent", "lot/7-x", "a1", "closed", True),
            ):
                with self.subTest(label=label):
                    path = os.path.join(root, "a1-7" if label == "named-directory" else label)
                    os.makedirs(os.path.join(path, "src"))
                    source = os.path.join(path, "src", "app.py")
                    with open(source, "w") as out:
                        out.write("value = 1\n")
                    if recent:
                        os.utime(source, (now - 60, now - 60))
                    row = dict(id=1, path=path, agent="a1", lot=None, branch=branch,
                               repo=root, detail=menage.UNATTRIBUTED, status="active",
                               created_ts=now - 3 * 86400)
                    if label.startswith("other-merged-kept"):
                        row.update(status="kept", checked_ts=now - 2 * DAY,
                                   detail="lot #7 merged (assigned to another agent): "
                                          "kept — not integrated")
                    lookup = mock.Mock(return_value=dict(assignee=owner, state=state)
                                       if owner else None)
                    if isinstance(state, Exception):
                        lookup.side_effect = state
                    elif isinstance(state, dict):
                        lookup.side_effect = lambda ident: dict(
                            assignee=owner, state=state.get(ident))
                    store = SimpleNamespace(work=SimpleNamespace(get=lookup),
                        housekeeping=SimpleNamespace(worktrees=mock.Mock(return_value=[row])))
                    git = dict(exists=True, clean=True, pushed=True, integrated=False,
                               locked=False, submodules="", gitdir="", reason="not integrated")
                    if label == "other-unpushed-equivalent":
                        # git cherry / a squash can prove equivalent content
                        # on main while the original HEAD is still unpushed.
                        git.update(pushed=False, integrated=True, reason="")
                    # Git is simulated here: the #2180 marker and repository
                    # guards are too (their own witnesses use real worktrees)
                    with mock.patch.object(menage.storage, "of", return_value=store), \
                            mock.patch.object(menage, "worktree_state", return_value=git), \
                            mock.patch.object(menage, "worktree_holders",
                                              return_value=(False, "")), \
                            mock.patch.object(menage, "current_branch",
                                              return_value=(True, row["branch"])), \
                            mock.patch.object(menage, "ignored_leftovers", return_value=[]), \
                            mock.patch.object(hd, "_marker", return_value="linked"), \
                            mock.patch.object(menage, "same_repository", return_value=True), \
                            mock.patch.object(platform, "user_paths", return_value=platform.UserPaths()):
                        entries, plan = menage._worktree_pass(None, "pc", dry_run=True,
                                                             protected=set(), now=now)
                    self.assertEqual(entries, [])
                    # #2174: the pass reads every tracked worktree, uncapped
                    self.assertIsNone(store.housekeeping.worktrees.call_args.kwargs["limit"])
                    # #2174: another agent's ENDED lot follows the orphan
                    # rule; its open, unknown or unreadable lot proves nothing.
                    # #4442: a foreign copy whose content is already on main
                    # (`integrated`, even with a local HEAD not pushed) is no
                    # longer pinned by the early foreign guard — the direct
                    # removal applies like for an own copy.
                    self.assertEqual(plan[0]["action"] == "retrait",
                                     label in ("orphan", "own-closed", "other-closed",
                                               "other-closed-candidates", "other-merged-kept",
                                               "other-unpushed-equivalent"), plan)
                    if recent:
                        outcome, freed, reason, built = menage._remove_kept_worktree(
                            path, root, "", artifacts=None, allow=(),
                            lock_path=os.path.join(root, "artifacts.lock"),
                            journal_path=os.path.join(root, "artifacts.journal"),
                            idle_before=now - DAY)
                        self.assertEqual((outcome, freed, built), ("garde", 0, []))
                        self.assertIn("active within 24 h", reason)


class _Store:
    def __init__(self, rows, lots):
        self.rows, self.lots, self.statuses = rows, lots, []
        self.housekeeping = self
        self.work = SimpleNamespace(get=lambda ident: {"state": self.lots.get(ident)})

    def worktrees(self, host, statuses=None, limit=500):
        return [r for r in self.rows if r["status"] in (statuses or (r["status"],))]

    def set_worktree_status(self, ident, status, detail):
        self.statuses.append((ident, status, detail))

    def set_worktree_attribution(self, ident, agent, lot, detail):
        # #3266 : la passe ne persiste une attribution que pour une ligne sans
        # lot ; les scénarios de ce module partent tous d'un lot posé.
        raise AssertionError("unexpected attribution of %s to lot %s" % (ident, lot))


class MergedKeptTest(_Repo):
    def row(self, path, *, checked_ago, detail=None, lot="7"):
        st = menage.worktree_state(path)
        # #2180: `repo` porte le common dir du dépôt (comme les lignes écrites
        # par `register_worktree`), pas le chemin du worktree principal.
        return {"id": 1, "path": path, "agent": "dev2", "lot": lot,
                "repo": menage.common_dir(self.main),
                "status": "kept", "created_ts": time.time() - 3 * DAY,
                "checked_ts": time.time() - checked_ago,
                # #4442: the stored reason is the NORMALIZED one (no SHA, no
                # file count), so the 24 h grace matches after a volatile change
                "detail": detail if detail is not None else
                menage.kept_detail("lot #%s merged" % lot, st)}

    def run_pass(self, rows, lots, *, dry_run=False, allow=()):
        store = _Store(rows, lots)
        _age(rows[0]["path"], 2 * 3600)
        with mock.patch.object(menage.storage, "of", return_value=store), \
                mock.patch.object(menage, "worktree_holders", return_value=(False, "")):
            entries, plan = menage._worktree_pass(
                None, "pc", dry_run=dry_run, protected=set(),
                now=time.time(), allow=allow,
                lock_path=os.path.join(self.base, "state", "artifacts.lock"),
                journal_path=self.journal_path)
        return store, entries, plan

    def test_pushed_not_merged_removed_after_a_day_branch_kept(self):
        wt = self.add_worktree("lot-7")
        _write(os.path.join(wt, "node_modules", "a.js"))   # ignored build output: fine
        _store, _e, plan = self.run_pass([self.row(wt, checked_ago=DAY - 60)], {7: "merged"})
        self.assertEqual(plan[0]["action"], "garde")
        _store, _e, plan = self.run_pass([self.row(wt, checked_ago=DAY + 60)], {7: "merged"},
                                         dry_run=True)
        self.assertEqual(plan[0]["action"], "retrait")
        self.assertTrue(os.path.isdir(wt))
        store, entries, _plan = self.run_pass([self.row(wt, checked_ago=DAY + 60)], {7: "merged"})
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(store.statuses[0][1], "removed")
        self.assertIn("clean and pushed", entries[0]["detail"])
        self.assertIn("lot-7", _git("branch", "--list", "lot-7", cwd=self.main))

    def test_ignored_files_or_process_keep_it(self):
        # (f) an ignored .env is not build output; (e) the strict process check
        wt = self.add_worktree("lot-6")
        _write(os.path.join(wt, ".env"), text="SECRET=1\n")
        plan = self.run_pass([self.row(wt, checked_ago=2 * DAY, lot="6")], {6: "merged"})[2]
        self.assertEqual(plan[0]["action"], "garde")
        self.assertIn(".env", plan[0]["detail"])
        os.unlink(os.path.join(wt, ".env"))
        self.fake_process(50, maps=[os.path.join(wt, "f.txt")], comm="vim")
        plan = self.run_pass([self.row(wt, checked_ago=2 * DAY, lot="6")], {6: "merged"})[2]
        self.assertIn("50 vim", plan[0]["detail"])
        shutil.rmtree(os.path.join(self.proc, "50"))
        if os.getuid() != 0:
            self.fake_process(51, comm="btop", unreadable=True)
            plan = self.run_pass([self.row(wt, checked_ago=2 * DAY, lot="6")], {6: "merged"})[2]
            self.assertIn("unreadable", plan[0]["detail"])
            plan = self.run_pass([self.row(wt, checked_ago=2 * DAY, lot="6")], {6: "merged"},
                                 allow=("btop",))[2]
            self.assertEqual(plan[0]["action"], "retrait")
        self.assertFalse(os.path.exists(wt))

    def test_r2_2_nested_repository_in_build_output_keeps_it(self):
        # r2 (2): the build output goes through _delete_tree first (a `.git`
        # inside is left), then nothing ignored may remain
        wt = self.add_worktree("lot-11")
        _write(os.path.join(wt, "node_modules", "a.js"))
        _write(os.path.join(wt, "node_modules", "dep", ".git", "HEAD"), text="unpushed\n")
        _write(os.path.join(wt, "node_modules", "dep", "work.js"))
        store, entries, plan = self.run_pass([self.row(wt, checked_ago=2 * DAY, lot="11")],
                                             {11: "merged"})
        self.assertEqual(plan[0]["action"], "garde")
        self.assertIn("holds a .git", plan[0]["detail"])
        self.assertEqual(store.statuses, [])
        self.assertTrue(os.path.isdir(wt))
        trash, = [n for n in os.listdir(wt) if n.startswith(ba.TRASH_PREFIX)]
        self.assertTrue(os.path.exists(os.path.join(wt, trash, "dep", ".git", "HEAD")))
        self.assertTrue(os.path.exists(os.path.join(wt, trash, "dep", "work.js")))
        self.assertFalse(os.path.exists(os.path.join(wt, trash, "a.js")))
        self.assertEqual([e["action"] for e in entries], ["partial"])
        # the trash left on purpose makes the tree unclean: kept for good
        plan = self.run_pass([self.row(wt, checked_ago=2 * DAY, lot="11")], {11: "merged"})[2]
        self.assertEqual(plan[0]["action"], "garde")
        self.assertTrue(os.path.isdir(wt))

    def test_r2_3_rechecked_renamed_journaled_and_locked(self):
        # r2 (3): host lock, processes read again right before the rename,
        # the worktree renamed to a journaled trash, an interrupted deletion
        # finished from the journal
        wt = self.add_worktree("lot-12")
        _write(os.path.join(wt, "node_modules", "a.js"))
        rows = lambda: [self.row(wt, checked_ago=2 * DAY, lot="12")]   # noqa: E731
        held = ba._lock(os.path.join(self.base, "state", "artifacts.lock"))
        plan = self.run_pass(rows(), {12: "merged"})[2]
        held.close()
        self.assertEqual(plan[0]["action"], "attend")
        self.assertIn("another pass", plan[0]["detail"])
        self.assertTrue(os.path.isdir(os.path.join(wt, "node_modules")))
        real = platform.user_paths
        calls = []

        def scan(allow=()):   # a process comes in once the build output is gone
            calls.append(1)
            if not os.path.exists(os.path.join(wt, "node_modules")):
                self.fake_process(60, cwd=wt, comm="claude")
            return real(allow=allow)

        with mock.patch.object(platform, "user_paths", side_effect=scan):
            store, _e, plan = self.run_pass(rows(), {12: "merged"})
        self.assertIn("60 claude", plan[0]["detail"])
        self.assertTrue(os.path.isfile(os.path.join(wt, "f.txt")))
        self.assertEqual(store.statuses, [])
        shutil.rmtree(os.path.join(self.proc, "60"))
        real_finish = ba._finish_at
        seen = []

        def interrupted(pfd, record, journal, **kw):   # the pass dies after the rename
            seen.append([r["trash"] for r in journal.records()])
            if record["kind"] == "worktree":
                return dict(real_finish(pfd, record, journal, avoid=kw["avoid"],
                                        budget=ba.Budget(-1.0)))
            return real_finish(pfd, record, journal, **kw)

        with mock.patch.object(ba, "_finish_at", side_effect=interrupted):
            store, entries, _plan = self.run_pass(rows(), {12: "merged"})
        self.assertFalse(os.path.exists(wt))
        record, = self.journal()
        self.assertEqual((record["kind"], record["origin"]), ("worktree", wt))
        self.assertIn(record["trash"], seen[-1])   # journaled before the rename
        self.assertEqual(entries[-1]["action"], "partial")
        self.assertNotIn(os.path.basename(wt), _git("worktree", "list", cwd=self.main))
        out = ba.run([], ba.rules_of(None), idle_s=DAY, protected=(), dry_run=False,
                     now=time.time(), journal_path=self.journal_path)
        self.assertEqual([e["action"] for e in out["entries"]], ["deleted"])
        self.assertFalse(os.path.exists(record["trash"]))
        self.assertEqual(self.journal(), [])
        self.assertIn("lot-12", _git("branch", "--list", "lot-12", cwd=self.main))

    def test_r3_1_submodules_keep_it_and_are_reported(self):
        # r3 (1): prune would delete <gitdir>/modules (submodule objects)
        wt = self.add_worktree("lot-13")
        gitdir = _git("rev-parse", "--absolute-git-dir", cwd=wt).strip()
        _write(os.path.join(gitdir, "modules", "sub", "HEAD"), text="ref: refs/heads/local\n")
        old = "lot #13 merged: kept — HEAD not integrated (pushed but not merged)"
        store, entries, plan = self.run_pass(
            [self.row(wt, checked_ago=2 * DAY, lot="13", detail=old)], {13: "merged"})
        self.assertEqual(plan[0]["action"], "garde")
        self.assertIn("submodules", plan[0]["detail"])
        self.assertEqual(store.statuses[0][1], "kept")   # reported: worktree_kept
        self.assertIn("modules", store.statuses[0][2])
        self.assertEqual(entries[0]["action"], "kept")
        # the same reason a day later: still kept, nothing deleted
        store, _e, plan = self.run_pass([self.row(wt, checked_ago=2 * DAY, lot="13")],
                                        {13: "merged"})
        self.assertEqual((plan[0]["action"], store.statuses), ("garde", []))
        self.assertTrue(os.path.exists(os.path.join(gitdir, "modules", "sub", "HEAD")))
        shutil.rmtree(os.path.join(gitdir, "modules"))
        _write(os.path.join(wt, ".gitmodules"), text="[submodule \"s\"]\n")
        _git("add", ".gitmodules", cwd=wt)
        _git("commit", "-q", "-m", "submodules", cwd=wt)
        _git("push", "-q", "origin", "lot-13", cwd=wt)
        plan = self.run_pass([self.row(wt, checked_ago=2 * DAY, lot="13")], {13: "merged"})[2]
        self.assertIn(".gitmodules", plan[0]["detail"])
        self.assertTrue(os.path.isdir(wt))

    def test_r3_2_git_lock_checked_again_right_before_the_rename(self):
        # r3 (2): `git worktree lock` done during the cleanup of the build output
        wt = self.add_worktree("lot-14")
        _write(os.path.join(wt, "node_modules", "a.js"))
        real = platform.user_paths

        def scan(allow=()):
            if not os.path.exists(os.path.join(wt, "node_modules")) \
                    and "locked" not in _git("worktree", "list", "--porcelain", cwd=self.main):
                _git("worktree", "lock", wt, cwd=self.main)
            return real(allow=allow)

        with mock.patch.object(platform, "user_paths", side_effect=scan):
            store, _e, plan = self.run_pass([self.row(wt, checked_ago=2 * DAY, lot="14")],
                                            {14: "merged"})
        self.assertEqual(plan[0]["action"], "garde")
        self.assertIn("locked", plan[0]["detail"])
        self.assertEqual(store.statuses, [])
        self.assertTrue(os.path.isfile(os.path.join(wt, "f.txt")))
        self.assertEqual(self.journal(), [])

    def test_closed_dirty_unpushed_or_changed_reason_stay_kept(self):
        wt = self.add_worktree("lot-8")
        self.assertEqual(self.run_pass([self.row(wt, checked_ago=2 * DAY, lot="8")],
                                       {8: "closed"})[2][0]["action"], "garde")
        self.assertEqual(self.run_pass([self.row(wt, checked_ago=2 * DAY, lot="8",
                                                 detail="lot #8 merged: kept — other")],
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

    def test_state_writes_nothing_in_git(self):
        # (j) squash detection through a scratch object directory
        wt = self.add_worktree("lot-10")
        _git("merge", "-q", "--squash", "origin/lot-10", cwd=self.main)
        _git("commit", "-q", "-m", "squash", cwd=self.main)
        _git("push", "-q", "origin", "main", cwd=self.main)
        _write(os.path.join(self.main, "more.txt"))
        _git("add", "more.txt", cwd=self.main)
        _git("commit", "-q", "-m", "more", cwd=self.main)
        _git("push", "-q", "origin", "main", cwd=self.main)
        objects = os.path.join(self.main, ".git", "objects")

        def snapshot():
            return sorted(os.path.join(c, f) for c, _d, fs in os.walk(objects) for f in fs)

        before = snapshot()
        st = menage.worktree_state(wt)
        self.assertTrue(st["integrated"], st)
        self.assertEqual(snapshot(), before)


class ArtifactPassTest(_Repo):
    def artifact_pass(self, lots, *, running=(), dry_run=False, rows=()) -> dict:
        with mock.patch.object(menage.storage, "of", return_value=_Store(list(rows), lots)):
            return menage._artifact_pass(None, "pc", menage.Policy(),
                                         cwd_of={"dev2": self.main}, dry_run=dry_run,
                                         now=time.time() + LATER,
                                         lock_path=os.path.join(self.base, "a.lock"),
                                         journal_path=self.journal_path,
                                         running=running, known_agents=("dev1", "dev2"))

    def test_pass_covers_agents_repositories_and_owner(self):
        wt = self.add_worktree("lot-5")
        _write(os.path.join(wt, "node_modules", "a.js"))
        _write(os.path.join(self.main, "node_modules", "main.js"))
        out = self.artifact_pass({})
        # #3195: a worktree of the clone dev2 works in is dev2's
        self.assertEqual([(os.path.realpath(e["path"]), e["agent"]) for e in out["entries"]],
                         [(os.path.realpath(os.path.join(wt, "node_modules")), "dev2")])
        self.assertTrue(os.path.exists(os.path.join(self.main, "node_modules", "main.js")))

    def test_3195_idle_worktree_purged_open_lot_or_turn_kept(self):
        """The lot's witness: an idle worktree with node_modules is purged, the
        worktree of an open lot (named by its directory or its branch) and the
        worktrees of an agent in turn are not; the dry run lists them."""
        ended = self.add_worktree("lot-5")
        named = self.add_worktree("lot-6")
        branch = os.path.join(self.base, "copy")
        _git("worktree", "add", "-q", "-b", "lot/7-slug", branch, cwd=self.main)
        for wt in (ended, named, branch):
            _write(os.path.join(wt, "node_modules", "a.js"))
        lots = {5: "merged", 6: "qa", 7: "intake"}
        out = self.artifact_pass(lots, running={"dev2"})
        self.assertEqual(out["entries"], [])
        self.assertIn("3 idle worktree(s) kept", " ".join(out["skipped"]))
        self.assertIn("turn in progress for dev2", " ".join(out["skipped"]))
        out = self.artifact_pass(lots, running={"dev1"}, dry_run=True)
        self.assertEqual([os.path.realpath(p["path"]) for p in out["plan"]],
                         [os.path.realpath(os.path.join(ended, "node_modules"))])
        out = self.artifact_pass(lots, running={"dev1"})
        self.assertEqual([os.path.realpath(e["path"]) for e in out["entries"]],
                         [os.path.realpath(os.path.join(ended, "node_modules"))])
        self.assertFalse(os.path.lexists(os.path.join(ended, "node_modules")))
        skipped = " ".join(out["skipped"])
        self.assertIn("lot #6 qa", skipped)
        self.assertIn("lot #7 intake", skipped)
        for wt in (named, branch):
            self.assertTrue(os.path.exists(os.path.join(wt, "node_modules", "a.js")))

    def test_3195_r2_the_recorded_lot_of_a_tracked_worktree_keeps_it(self):
        """R2 B2 (cx1): a tracked worktree whose branch and directory name no
        lot (`scratch`) keeps its build output while the lot it was
        registered for is open; an unreadable record keeps it too; once the
        lot is merged, the output goes."""
        scratch = os.path.join(self.base, "scratch")
        _git("worktree", "add", "-q", "-b", "scratch", scratch, cwd=self.main)
        _write(os.path.join(scratch, "node_modules", "a.js"))

        def row(lot):
            return {"id": 9, "path": scratch, "agent": "dev2", "lot": lot,
                    "repo": menage.common_dir(self.main), "status": "active",
                    "branch": "scratch", "detail": ""}
        out = self.artifact_pass({123: "qa"}, running={"dev1"}, rows=[row("123")])
        self.assertEqual(out["entries"], [])
        self.assertIn("lot #123 qa", " ".join(out["skipped"]))
        out = self.artifact_pass({}, running={"dev1"}, rows=[row("#12x")])
        self.assertEqual(out["entries"], [])
        self.assertIn("recorded lot '#12x' unreadable", " ".join(out["skipped"]))
        out = self.artifact_pass({123: "merged"}, running={"dev1"}, rows=[row("123")])
        self.assertEqual([os.path.realpath(e["path"]) for e in out["entries"]],
                         [os.path.realpath(os.path.join(scratch, "node_modules"))])


if __name__ == "__main__":
    unittest.main()
