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


class _ForeignDev:
    """#500 r3: an `os.stat` result whose `st_dev` is another filesystem."""

    def __init__(self, st, dev: int):
        self._st, self._dev = st, dev

    def __getattr__(self, name):
        return self._dev if name == "st_dev" else getattr(self._st, name)


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

    def test_unlink_failure_reclassifies_and_stops_the_eviction(self):
        # r2 (codex P1): a candidate whose unlink fails stays, its bytes become
        # unremovable, and NO more recent file is deleted to make up for it
        ours = [os.path.join(self.mine, "o%d" % i) for i in range(4)]
        for i, path in enumerate(ours):
            _write(path, age_s=(10 - i) * HOUR)          # o0 is the oldest

        def refuse(path, root, **kw):
            raise OSError(1, "operation not permitted", path)

        with mock.patch.object(menage, "_unlink_in_root", refuse):
            result = menage.evict_lru([self.mine], self.root, 2 * B)
        self.assertEqual(result["files"], 0)
        self.assertTrue(all(os.path.exists(path) for path in ours))
        self.assertEqual(result["unremovable"], B)
        self.assertEqual(result["failed"], [ours[0]])
        self.assertFalse(result["blocked"])              # one block, quota two
        entries = menage.eviction_entries(result, kind="tmp", path=self.mine,
                                          quota=2 * B)
        self.assertEqual([e["action"] for e in entries], ["unremovable"])
        self.assertIn("deletion refused", entries[0]["detail"])

    def test_git_root_recognised_before_the_file_limit(self):
        # r2 (codex P1): the work tree is found at the directory level, before
        # its files are listed, so WALK_LIMIT can no longer hide it
        repo = os.path.join(self.base, "repo-limit")
        os.makedirs(repo)
        _git("init", "-q", "-b", "main", cwd=repo)
        _git("commit", "-q", "--allow-empty", "-m", "racine", cwd=repo)
        clone = os.path.join(self.mine, "clone")
        _git("clone", "-q", repo, clone, cwd=self.base)
        for i in range(12):
            _write(os.path.join(clone, "src", "f%d" % i))
        loose = os.path.join(self.mine, "loose")
        _write(loose, size=2 * B)
        with mock.patch.object(menage, "WALK_LIMIT", 5):
            result = menage.evict_lru([self.mine], self.root, B)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["worktrees"], [clone])
        self.assertEqual(result["files"], 1)
        self.assertFalse(os.path.exists(loose))
        self.assertTrue(os.path.exists(os.path.join(clone, "src", "f11")))

    def test_truncated_listing_evicts_nothing_and_is_signaled(self):
        # r2 (codex P1): a walk stopped by WALK_LIMIT proves nothing about what
        # is removable: nothing is evicted, and the journal says so
        many = [os.path.join(self.mine, "many", "f%d" % i) for i in range(6)]
        for path in many:
            _write(path)
        with mock.patch.object(menage, "WALK_LIMIT", 3):
            result = menage.evict_lru([self.mine], self.root, B)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncated_paths"], [self.mine])
        self.assertEqual(result["files"], 0)
        self.assertEqual(result["evicted"], 0)
        self.assertTrue(all(os.path.exists(path) for path in many))
        entries = menage.eviction_entries(result, kind="tmp", path=self.mine, quota=B)
        self.assertEqual([e["action"] for e in entries], ["truncated"])
        self.assertIn("truncated", entries[0]["detail"])

    def test_parent_replaced_by_symlink_never_deletes_outside(self):
        # r2 (codex P1): the unlink is directory-relative (O_DIRECTORY|
        # O_NOFOLLOW at every level), so a parent swapped for a symbolic link
        # after the `_located_in` check cannot reach outside the root
        outside = os.path.join(self.base, "outside")
        victim = os.path.join(outside, "f")
        _write(victim)
        inside = os.path.join(self.mine, "dir")
        _write(os.path.join(inside, "f"))
        real = menage._unlink_in_root

        def swapping(path, root, **kw):
            os.unlink(os.path.join(inside, "f"))
            os.rmdir(inside)
            os.symlink(outside, inside)
            return real(path, root, **kw)

        with mock.patch.object(menage, "_unlink_in_root", swapping):
            result = menage.evict_lru([self.mine], self.root, 0)
        self.assertTrue(os.path.exists(victim))
        self.assertEqual(result["files"], 0)
        self.assertEqual(result["unremovable"], B)
        self.assertEqual(result["failed"], [os.path.join(inside, "f")])

    def test_candidate_replaced_between_the_scan_and_the_unlink_is_skipped(self):
        # r3 (codex P1): the candidate is re-verified just before its unlink
        # ((st_dev, st_ino, st_mtime_ns, st_size) against the scan); a file
        # replaced by a fresh one is skipped, not counted, and the pass goes on
        ours = [os.path.join(self.mine, "o%d" % i) for i in range(2)]
        for i, path in enumerate(ours):
            _write(path, age_s=(10 - i) * HOUR)          # o0 is the oldest
        real = menage._unlink_in_root

        def replacing(path, root, **kw):
            if path == ours[0]:
                os.unlink(path)
                _write(path, size=B, age_s=0)            # a fresh file, same name
            return real(path, root, **kw)

        with mock.patch.object(menage, "_unlink_in_root", replacing):
            result = menage.evict_lru([self.mine], self.root, B)
        self.assertTrue(os.path.exists(ours[0]))         # the replacement stays
        self.assertFalse(os.path.exists(ours[1]))        # the next one goes
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["evicted"], B)
        self.assertEqual(result["unremovable"], 0)
        self.assertEqual(result["failed"], [])

    def test_git_marker_appearing_before_the_unlink_protects_the_subtree(self):
        # r3 (codex P1): a `.git` that appears in a directory opened on the way
        # to the unlink leaves that whole subtree out of the eviction
        # r4 (codex P2 r3): the protected bytes leave `counted` too, so the
        # newer `loose` is not evicted: what is still evictable (1 block)
        # already fits the quota (1 block)
        repo = os.path.join(self.mine, "repo")
        files = [os.path.join(repo, "f%d" % i) for i in range(2)]
        for i, path in enumerate(files):
            _write(path, age_s=(10 - i) * HOUR)
        loose = os.path.join(self.mine, "loose")
        _write(loose, age_s=2 * HOUR)                    # newer than the repo's
        real = menage._unlink_in_root

        def appearing(path, root, **kw):
            marker = os.path.join(repo, ".git")
            if not os.path.exists(marker):
                os.mkdir(marker)                      # a clone appears
            return real(path, root, **kw)

        with mock.patch.object(menage, "_unlink_in_root", appearing):
            result = menage.evict_lru([self.mine], self.root, B)
        self.assertTrue(all(os.path.exists(path) for path in files))
        self.assertTrue(os.path.exists(loose))           # quota already held
        self.assertEqual(result["files"], 0)
        self.assertEqual(result["evicted"], 0)
        self.assertFalse(result["over_quota"])

    def test_candidate_gone_mid_pass_frees_its_bytes(self):
        # r4 (codex P2 r3): a candidate that disappeared between the scan and
        # the unlink must not keep its bytes in `counted`: the next file is
        # not evicted to make up for a file that is no longer there
        ours = [os.path.join(self.mine, "o%d" % i) for i in range(2)]
        for i, path in enumerate(ours):
            _write(path, age_s=(10 - i) * HOUR)          # o0 is the oldest
        real = menage._unlink_in_root

        def vanishing(path, root, **kw):
            if path == ours[0]:
                os.unlink(path)                          # gone on its own
            return real(path, root, **kw)

        with mock.patch.object(menage, "_unlink_in_root", vanishing):
            result = menage.evict_lru([self.mine], self.root, B)
        self.assertFalse(os.path.exists(ours[0]))
        self.assertTrue(os.path.exists(ours[1]))         # quota already held
        self.assertEqual(result["files"], 0)
        self.assertEqual(result["evicted"], 0)
        self.assertFalse(result["over_quota"])

    def test_scan_and_unlink_refuse_a_foreign_filesystem(self):
        # r3 (codex P1): every opened directory must be on the managed root's
        # filesystem; the scan does not cross a mount point, the unlink
        # refuses one, and the prune neither opens nor removes one
        outside = os.path.join(self.mine, "f")
        _write(outside)
        empty = os.path.join(self.mine, "empty")
        sub = os.path.join(empty, "sub")
        os.makedirs(sub)
        dev = os.lstat(self.mine).st_dev
        self.assertEqual(list(menage._files(self.mine, device=dev + 1)), [])
        with self.assertRaises(menage.StaleCandidate) as caught:
            menage._unlink_in_root(outside, self.root, device=dev + 1)
        self.assertEqual(caught.exception.reason, "mount point")
        self.assertEqual(caught.exception.path, self.root)
        menage._prune_empty_dirs(empty, dev + 1)
        self.assertTrue(os.path.isdir(sub))
        menage._prune_empty_dirs(empty, dev)
        self.assertFalse(os.path.exists(sub))

        real_stat = os.stat

        def foreign(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if isinstance(path, (str, bytes, os.PathLike)) \
                    and os.fspath(path) == self.root:
                return _ForeignDev(st, st.st_dev + 1)
            return st

        with mock.patch.object(menage.os, "stat", foreign):
            result = menage.evict_lru([self.mine], self.root, 0)
        self.assertEqual(result["files"], 0)
        self.assertEqual(result["before"], 0)
        self.assertTrue(os.path.exists(outside))

    def test_prune_never_enters_a_git_protected_tree(self):
        # r4 (codex P2 r3): after an eviction, the empty-directory pruning must
        # not touch a work tree protected by its `.git` marker; an unprotected
        # empty directory is still pruned
        repo = os.path.join(self.base, "repo-prune")
        os.makedirs(repo)
        _git("init", "-q", "-b", "main", cwd=repo)
        _git("commit", "-q", "--allow-empty", "-m", "racine", cwd=repo)
        tree = os.path.join(self.mine, "wt")
        _git("worktree", "add", "-q", tree, cwd=repo)
        keep = os.path.join(tree, "src", "empty")
        os.makedirs(keep)
        prune = os.path.join(self.mine, "scratch", "empty")
        os.makedirs(prune)
        loose = os.path.join(self.mine, "loose")
        _write(loose)
        result = menage.evict_lru([self.mine], self.root, 0)
        self.assertEqual(result["files"], 1)
        self.assertFalse(os.path.exists(loose))
        self.assertTrue(os.path.isdir(keep), "the work tree was pruned")
        self.assertFalse(os.path.exists(prune), "an unprotected empty directory is pruned")

    def test_sparse_file_counts_only_the_blocks_it_occupies(self):
        # r3 (codex P2): st_blocks*512 always, 0 meaning 0 — a 1 TB sparse file
        # no longer counts its logical size and no longer evicts the others
        sparse = os.path.join(self.mine, "sparse")
        os.makedirs(self.mine, exist_ok=True)
        with open(sparse, "wb") as fh:
            fh.truncate(1 << 40)
        old = time.time() - 3 * HOUR
        os.utime(sparse, (old, old))
        real = os.path.join(self.mine, "real")
        _write(real, size=B, age_s=2 * HOUR)
        self.assertLess(menage.size_of(sparse), 4096)    # blocks, not 1 TB
        found = {os.path.basename(path): size
                 for path, size, _m, _st in menage._files(self.mine)}
        self.assertLess(found["sparse"], 4096)           # blocks, not 1 TB
        self.assertEqual(found["real"], B)
        result = menage.evict_lru([self.mine], self.root, 0)
        self.assertEqual(result["before"], B)
        self.assertEqual(result["evicted"], B)
        self.assertEqual(result["files"], 2)

    def test_git_marker_symlink_protects_the_subtree_and_is_signaled(self):
        # r2 (codex P2): a `.git` that is a symbolic link is not followed; the
        # subtree is protected by prudence and reported
        target = os.path.join(self.base, "gitdir-target")
        os.makedirs(target)
        _write(os.path.join(target, "HEAD"), size=10)
        fake = os.path.join(self.mine, "fake")
        os.makedirs(fake)
        os.symlink(target, os.path.join(fake, ".git"))
        _write(os.path.join(fake, "data"), size=2 * B)
        result = menage.evict_lru([self.mine], self.root, B)
        self.assertEqual(result["worktrees"], [])
        self.assertEqual(result["worktree_symlinks"], [fake])
        self.assertTrue(os.path.exists(os.path.join(fake, "data")))
        self.assertTrue(os.path.exists(os.path.join(target, "HEAD")))
        entries = menage.eviction_entries(result, kind="tmp", path=self.mine, quota=B)
        signaled = [e for e in entries if e["action"] == "signaled"]
        self.assertEqual([e["path"] for e in signaled], [fake])
        self.assertIn("symbolic link", signaled[0]["detail"])

    def test_unremovable_bytes_reported_even_without_eviction(self):
        # r2 (codex P2): the bytes are in the journal and in `data` as soon as
        # they exist, even when the quota still holds (nothing evicted)
        foreign = {os.path.join(self.mine, "wine", "f%d" % i) for i in range(3)}
        for path in foreign:
            _write(path)
        ours = [os.path.join(self.mine, "o%d" % i) for i in range(3)]
        for path in ours:
            _write(path)
        with self._foreign(foreign):
            result = menage.evict_lru([self.mine], self.root, 5 * B)
        self.assertEqual(result["files"], 0)
        self.assertEqual(result["unremovable"], 3 * B)
        self.assertFalse(result["blocked"])
        entries = menage.eviction_entries(result, kind="tmp", path=self.mine, quota=5 * B)
        self.assertEqual([e["action"] for e in entries], ["unremovable"])
        self.assertFalse(entries[0]["data"]["blocked"])
        self.assertEqual(entries[0]["data"]["unremovable_bytes"], 3 * B)

    def test_hard_links_and_blocks_count_once(self):
        # r2 (codex P2): sizes are st_blocks*512 and a hard link is not a
        # second file
        first = os.path.join(self.mine, "a")
        _write(first, size=5)                    # 5 bytes: one 4096 block
        os.link(first, os.path.join(self.mine, "b"))
        self.assertEqual(menage.size_of(self.mine), B)
        found = list(menage._files(self.mine))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1], B)
        result = menage.evict_lru([self.mine], self.root, 0)
        self.assertEqual(result["files"], 1)
        self.assertEqual(sum(os.path.exists(os.path.join(self.mine, name))
                             for name in ("a", "b")), 1)

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
        # the owner removes the leftovers: with no new file and no eviction,
        # the next pass writes the lifting line (`resolved`) and the alert goes
        restore()
        for i in range(5):
            os.unlink(os.path.join(tmp, "rootdir", "f%d" % i))
        report = menage.run_pass(self.mine, self.db, self.pol, host="pc", actor="t")
        self.assertIn(("tmp", "resolved"),
                      [(e["kind"], e["action"]) for e in report["entries"]])
        self.assertEqual([a for a in exploitation._housekeeping_alerts([], self.db)
                          if a["type"] == "tmp_unremovable"], [])

    @unittest.skipIf(os.geteuid() == 0, "root unlinks in a read-only directory")
    def test_episode_since_is_stable_across_passes(self):
        # r2 (codex P2): `since` is the first observation of the episode, so
        # `--follow` does not announce the same alert again at every pass
        tmp = menage.turn_env(self.mine, self.pol, "codex3", {})["TMPDIR"]
        for i in range(5):
            _write(os.path.join(tmp, "rootdir", "f%d" % i))
        self.addCleanup(_read_only(os.path.join(tmp, "rootdir")))
        menage.run_pass(self.mine, self.db, self.pol, host="pc", actor="t")
        first = [a for a in exploitation._housekeeping_alerts([], self.db)
                 if a["type"] == "tmp_unremovable"]
        self.assertEqual(len(first), 1)
        time.sleep(1.1)                       # a different at_ts, same episode
        menage.run_pass(self.mine, self.db, self.pol, host="pc", actor="t")
        second = [a for a in exploitation._housekeeping_alerts([], self.db)
                  if a["type"] == "tmp_unremovable"]
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["since"], first[0]["since"])
        self.assertEqual(exploitation.alert_key(second[0]),
                         exploitation.alert_key(first[0]))

    def test_truncated_listing_alerts_and_evicts_nothing(self):
        # r2 (codex P1): nothing is evicted on a truncated listing, and the
        # tmp_unscanned alert says it
        tmp = menage.turn_env(self.mine, self.pol, "codex3", {})["TMPDIR"]
        many = [os.path.join(tmp, "many", "f%d" % i) for i in range(6)]
        for path in many:
            _write(path)
        with mock.patch.object(menage, "WALK_LIMIT", 3):
            report = menage.run_pass(self.mine, self.db, self.pol, host="pc", actor="t")
        self.assertTrue(all(os.path.exists(path) for path in many))
        self.assertIn(("tmp", "truncated"),
                      [(e["kind"], e["action"]) for e in report["entries"]])
        self.assertTrue(any("truncated" in note for note in report["skipped"]))
        alerts = [a for a in exploitation._housekeeping_alerts([], self.db)
                  if a["type"] == "tmp_unscanned"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["path"], tmp)
        self.assertIn("tmp_unscanned", notify.TYPE_LABELS)


if __name__ == "__main__":
    unittest.main()
