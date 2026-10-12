# SPDX-License-Identifier: AGPL-3.0-only
"""#500: the tmp quota eviction counts only what it can remove, and never
evicts the files of a git work tree.

Incident of 2026-10-12 (22:12, 22:18, laptop): codex3's TMPDIR held about
3.81 GiB of root-owned files (container leftovers, wine prefixes in
root-owned directories). `evict_lru` summed them into the total, failed
to unlink them (OSError, skipped), so the total never got under the
4 GiB quota and every turn end evicted every file older than an hour
(260,273 files): worktrees inside the directory became prunable.
"""
from __future__ import annotations

import os
import subprocess
import time
import unittest
from unittest import mock

from ameesh import exploitation, menage, notify, registry

from .support import PgTestCase

HOUR = 3600.0
#: one file of one block: its size on disk is its length (sizes count blocks)
B = 4096


class _Cfg:
    def __init__(self, state_dir: str, dsn: str = "", schema: str = "public"):
        self.state_dir, self.dsn, self.schema, self.host = state_dir, dsn, schema, "pc"


def _write(path: str, size: int = B, age_s: float = 3 * HOUR) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"z" * size)
    if age_s:
        at = time.time() - age_s
        os.utime(path, (at, at))


def _read_only(directory: str):
    """`directory` made unwritable (its files cannot be unlinked) for the
    test, restored at cleanup so the temporary tree can be removed."""
    os.chmod(directory, 0o555)

    def restore() -> None:
        if os.path.isdir(directory):
            os.chmod(directory, 0o755)
    return restore


def _git(*args: str, cwd: str) -> None:
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


class EvictionTest(unittest.TestCase):

    def setUp(self) -> None:
        import tempfile
        self.base = tempfile.mkdtemp(prefix="t500-")
        self.addCleanup(subprocess.run, ["rm", "-rf", self.base])
        self.root = os.path.join(self.base, "tmp")
        menage._ensure_root(self.root)
        self.mine = os.path.join(self.root, "codex3")

    def _foreign(self, paths: set):
        """Files of `paths` look owned by another user (root, in the incident)."""
        real = os.lstat

        class _St:
            def __init__(self, st):
                self._st = st

            def __getattr__(self, name):
                if name == "st_uid":
                    return os.geteuid() + 1
                return getattr(self._st, name)

        def lstat(path, *a, **kw):
            st = real(path, *a, **kw)
            return _St(st) if os.fspath(path) in paths else st
        return mock.patch.object(menage.os, "lstat", lstat)

    def test_unremovable_part_does_not_count_toward_the_quota(self):
        # RED before #500: 3 foreign blocks + 3 of ours, quota 4 blocks: the
        # total stayed at 6 > 4 and all of OUR files went
        foreign = {os.path.join(self.mine, "wine", "f%d" % i) for i in range(3)}
        for path in foreign:
            _write(path)
        ours = [os.path.join(self.mine, "work", "o%d" % i) for i in range(3)]
        for path in ours:
            _write(path)
        with self._foreign(foreign):
            result = menage.evict_lru([self.mine], self.root, 4 * B)
        for path in ours:
            self.assertTrue(os.path.exists(path), path)
        self.assertEqual(result["files"], 0)
        self.assertEqual(result["unremovable"], 3 * B)
        self.assertFalse(result["blocked"])
        self.assertFalse(result["over_quota"])

    def test_unremovable_part_alone_over_quota_evicts_nothing(self):
        foreign = {os.path.join(self.mine, "wine", "f%d" % i) for i in range(5)}
        for path in foreign:
            _write(path)
        ours = [os.path.join(self.mine, "o%d" % i) for i in range(10)]
        for path in ours:
            _write(path)
        with self._foreign(foreign):
            result = menage.evict_lru([self.mine], self.root, 4 * B)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["files"], 0)
        self.assertTrue(all(os.path.exists(p) for p in ours))
        entries = menage.eviction_entries(result, kind="tmp", path=self.mine, quota=4 * B,
                                          agent="codex3")
        self.assertEqual([e["action"] for e in entries], ["unremovable"])
        self.assertEqual(entries[0]["data"]["unremovable_bytes"], 5 * B)
        self.assertIn("nothing evicted", entries[0]["detail"])

    @unittest.skipIf(os.geteuid() == 0, "root unlinks in a read-only directory")
    def test_read_only_directory_is_unremovable(self):
        # the real case: files in a directory this user cannot write
        for i in range(5):
            _write(os.path.join(self.mine, "rootdir", "f%d" % i))
        ours = [os.path.join(self.mine, "o%d" % i) for i in range(3)]
        for i, path in enumerate(ours):
            _write(path, age_s=(10 - i) * HOUR)
        self.addCleanup(_read_only(os.path.join(self.mine, "rootdir")))
        result = menage.evict_lru([self.mine], self.root, 4 * B)
        self.assertTrue(result["blocked"])
        self.assertTrue(all(os.path.exists(p) for p in ours))
        # under a larger quota: only our 3 blocks count, nothing to evict
        result = menage.evict_lru([self.mine], self.root, 6 * B)
        self.assertFalse(result["blocked"])
        self.assertEqual(result["unremovable"], 5 * B)
        self.assertEqual(result["files"], 0)
        # writable again: everything counts, the oldest goes first
        os.chmod(os.path.join(self.mine, "rootdir"), 0o755)
        result = menage.evict_lru([self.mine], self.root, 7 * B)
        self.assertEqual(result["files"], 1)                    # 8 blocks -> 7
        self.assertFalse(os.path.exists(ours[0]))

    def test_git_worktree_is_never_evicted_and_is_signaled(self):
        repo = os.path.join(self.base, "repo")
        os.makedirs(repo)
        _git("init", "-q", "-b", "main", cwd=repo)
        _git("commit", "-q", "--allow-empty", "-m", "racine", cwd=repo)
        tree = os.path.join(self.mine, "review-1")
        _git("worktree", "add", "-q", tree, cwd=repo)
        for i in range(5):
            _write(os.path.join(tree, "src", "f%d" % i))
        clone = os.path.join(self.mine, "clone")
        _git("clone", "-q", repo, clone, cwd=self.base)
        _write(os.path.join(clone, "big"), size=4 * B)
        loose = os.path.join(self.mine, "loose")
        _write(loose, size=2 * B)
        old = time.time() - 3 * HOUR
        for top in (tree, clone):
            for current, _dirs, names in os.walk(top):
                for name in names:
                    os.utime(os.path.join(current, name), (old, old))
        result = menage.evict_lru([self.mine], self.root, B)
        self.assertFalse(os.path.exists(loose))
        self.assertTrue(all(os.path.exists(os.path.join(tree, "src", "f%d" % i))
                            for i in range(5)))
        self.assertTrue(os.path.exists(os.path.join(clone, "big")))
        self.assertEqual(sorted(result["worktrees"]), sorted([clone, tree]))
        listed = subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"],
                                capture_output=True, text=True, check=True).stdout
        self.assertNotIn("prunable", listed)
        entries = menage.eviction_entries(result, kind="tmp", path=self.mine, quota=B)
        self.assertEqual(sorted((e["action"], e["path"]) for e in entries),
                         sorted([("evicted", self.mine), ("signaled", clone),
                                 ("signaled", tree)]))

    def test_worktree_whose_gitdir_is_gone_is_evictable(self):
        dead = os.path.join(self.mine, "dead")
        os.makedirs(dead)
        with open(os.path.join(dead, ".git"), "w") as fh:
            fh.write("gitdir: /nowhere/.git/worktrees/dead\n")
        _write(os.path.join(dead, "f"), size=3 * B)
        result = menage.evict_lru([self.mine], self.root, B)
        self.assertEqual(result["worktrees"], [])
        self.assertFalse(os.path.exists(os.path.join(dead, "f")))

    def test_turn_end_logs_unremovable_instead_of_wiping(self):
        cfg = _Cfg(os.path.join(self.base, "state"))
        pol = menage.Policy(tmp_quota=4 * B)
        turn = menage.Turn.begin(cfg, pol, "codex3", None, new_session=False, base_env={})
        tmp = turn.env["TMPDIR"]
        foreign = {os.path.join(tmp, "wine", "f%d" % i) for i in range(5)}
        for path in foreign:
            _write(path)
        ours = os.path.join(tmp, "out.txt")
        _write(ours)
        with self._foreign(foreign):
            entries = turn.end(lot="500", turn_id="t")
        self.assertTrue(os.path.exists(ours))
        self.assertEqual([(e["kind"], e["action"]) for e in entries],
                         [("tmp", "unremovable")])


class PassAndAlertTest(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM housekeeping_log")
        self.mine = _Cfg(self.state, self.cfg.dsn, self.cfg.schema)
        self.pol = menage.Policy(tmp_quota=4 * B)
        registry.upsert(self.db, "codex3", harness="codex", host="pc")

    @unittest.skipIf(os.geteuid() == 0, "root unlinks in a read-only directory")
    def test_pass_evicts_nothing_and_the_alert_follows(self):
        tmp = menage.turn_env(self.mine, self.pol, "codex3", {})["TMPDIR"]
        for i in range(5):
            _write(os.path.join(tmp, "rootdir", "f%d" % i))
        ours = os.path.join(tmp, "out.txt")
        _write(ours)
        restore = _read_only(os.path.join(tmp, "rootdir"))
        self.addCleanup(restore)
        report = menage.run_pass(self.mine, self.db, self.pol, host="pc", actor="t")
        self.assertTrue(os.path.exists(ours))
        self.assertTrue(any("cannot remove" in s for s in report["skipped"]))
        self.assertIn(("tmp", "unremovable"),
                      [(e["kind"], e["action"]) for e in report["entries"]])
        alerts = [a for a in exploitation._housekeeping_alerts([], self.db)
                  if a["type"] == "tmp_unremovable"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["path"], tmp)
        self.assertEqual(alerts[0]["value"], 5 * B)
        self.assertIn("tmp_unremovable", notify.TYPE_LABELS)
        # the owner removes the leftovers: the next pass under quota writes
        # nothing, an eviction line clears the alert
        restore()
        for i in range(5):
            os.unlink(os.path.join(tmp, "rootdir", "f%d" % i))
        for i in range(6):
            _write(os.path.join(tmp, "new%d" % i))
        menage.run_pass(self.mine, self.db, self.pol, host="pc", actor="t")
        self.assertEqual([a for a in exploitation._housekeeping_alerts([], self.db)
                          if a["type"] == "tmp_unremovable"], [])


if __name__ == "__main__":
    unittest.main()
