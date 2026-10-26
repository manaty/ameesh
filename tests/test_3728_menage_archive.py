# SPDX-License-Identifier: AGPL-3.0-only
"""#3728 : un worktree d'un lot fini, gardé pour des fichiers modifiés ou non
suivis, est archivé (patch, tar des non suivis, manifeste) puis retiré au-delà
de `dirty_worktree_after` ; un lot ouvert, des commits non poussés ou une
politique non déclarée le gardent. Le bilan 24 h compte un relevé
`unremovable` une fois par entrée."""
from __future__ import annotations

import json
import os
import subprocess
import tarfile
import time
import unittest
from unittest import mock

from ameesh import housekeeping_archive as ha
from ameesh import menage, storage

from .support import PgTestCase
from .test_l230_build_artifacts import DAY, LATER, _Repo, _Store, _git, _write

AFTER = 6 * 3600.0
RETENTION = 14 * DAY


class _EndedStore(_Store):
    """Le faux registre dit aussi quand le lot a fini (`closed_ts`)."""

    def __init__(self, rows, lots, ended_ts):
        super().__init__(rows, lots)
        self.work.get = lambda ident: {"state": self.lots.get(ident), "closed_ts": ended_ts}


def _touch(top: str, moment: float) -> None:
    """Every mtime under `top` set to `moment` (a ctime cannot be set back:
    the pass runs at a later "now", as in the L230 tests)."""
    for current, dirs, files in os.walk(top):
        for name in dirs + files:
            os.utime(os.path.join(current, name), (moment, moment), follow_symlinks=False)
    os.utime(top, (moment, moment))


class ArchiveThenRemoveTest(_Repo):
    def setUp(self) -> None:
        super().setUp()
        self.archives = os.path.join(self.base, "state", ".menage", ha.ARCHIVE_DIR)

    def row(self, path, lot="7"):
        st = menage.worktree_state(path)
        return {"id": 1, "path": path, "agent": "dev2", "lot": lot,
                "repo": menage.common_dir(self.main), "status": "kept",
                "created_ts": time.time() - 3 * DAY, "checked_ts": time.time() - 60,
                "detail": "lot #%s merged: kept — %s" % (lot, st["reason"])}

    def run_pass(self, wt, lots, *, ended_ago=DAY, dry_run=False, archive=True, idle=DAY):
        now = time.time() + LATER
        store = _EndedStore([self.row(wt, lot=str(next(iter(lots))))], lots, now - ended_ago)
        _touch(wt, now - idle)
        with mock.patch.object(menage.storage, "of", return_value=store), \
                mock.patch.object(menage, "worktree_holders", return_value=(False, "")):
            entries, plan = menage._worktree_pass(
                None, "pc", dry_run=dry_run, protected=set(), now=now,
                lock_path=os.path.join(self.base, "state", "artifacts.lock"),
                journal_path=self.journal_path,
                archive={"after": AFTER, "retention": RETENTION, "root": self.archives}
                if archive else None)
        return store, entries, plan

    def dirty(self, branch):
        wt = self.add_worktree(branch)
        # cx2 (B/P1): staged content distinct from the working copy (`MM`)
        _write(os.path.join(wt, "f.txt"), text="staged-only-content\n")
        _git("add", "f.txt", cwd=wt)
        _write(os.path.join(wt, "f.txt"), text="changed\n")          # tracked, modified
        _write(os.path.join(wt, "new.txt"), text="staged new file\n")
        _git("add", "new.txt", cwd=wt)                                # staged only
        _write(os.path.join(wt, "notes", "todo.md"), text="draft\n")  # untracked
        _write(os.path.join(wt, ".env"), text="SECRET=1\n")           # ignored, not output
        _write(os.path.join(wt, "node_modules", "a.js"))              # build output
        _write(os.path.join(wt, "__pycache__", "m.pyc"))              # cache
        return wt

    def test_merged_dirty_worktree_archived_then_removed(self):
        wt = self.dirty("lot-7")
        head = _git("rev-parse", "HEAD", cwd=wt).strip()
        _store, entries, plan = self.run_pass(wt, {7: "merged"}, dry_run=True)
        self.assertEqual(plan[0]["action"], "archivage", plan)
        self.assertTrue(os.path.isdir(wt))
        self.assertFalse(os.path.exists(self.archives))
        store, entries, plan = self.run_pass(wt, {7: "merged"})
        self.assertEqual(plan[0]["action"], "retrait", plan)
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(store.statuses[0][1], "removed")
        archived, = [e for e in entries if e["action"] == "archived"]
        folder = archived["data"]["archive"]
        self.assertTrue(folder.startswith(self.archives + os.sep))
        self.assertEqual(os.stat(self.archives).st_mode & 0o777, 0o700)
        with open(os.path.join(folder, "staged.patch")) as fh:
            self.assertIn("+staged-only-content", fh.read())
        with open(os.path.join(folder, "unstaged.patch")) as fh:
            self.assertIn("+changed", fh.read())
        with tarfile.open(os.path.join(folder, "untracked.tar.gz")) as tar:
            names = sorted(tar.getnames())
        self.assertEqual(names, [".env", "notes/todo.md"])
        with open(os.path.join(folder, "manifest.json")) as fh:
            manifest = json.load(fh)
        self.assertEqual((manifest["head"], manifest["lot"]), (head, "7"))
        self.assertAlmostEqual(manifest["retain_until_ts"] - manifest["archived_ts"],
                               RETENTION)
        # both layers restore on the recorded HEAD: index and working copy
        again = os.path.join(self.base, "restore")
        _git("worktree", "add", "-q", "--detach", again, head, cwd=self.main)
        _git("apply", "--index", "--binary", os.path.join(folder, "staged.patch"), cwd=again)
        _git("apply", "--binary", os.path.join(folder, "unstaged.patch"), cwd=again)
        self.assertEqual(_git("show", ":f.txt", cwd=again), "staged-only-content\n")
        self.assertEqual(_git("show", ":new.txt", cwd=again), "staged new file\n")
        with open(os.path.join(again, "f.txt")) as fh:
            self.assertEqual(fh.read(), "changed\n")
        self.assertEqual(_git("status", "--porcelain", "--untracked-files=no", cwd=again),
                         "MM f.txt\nA  new.txt\n")
        self.assertIn("lot-7", _git("branch", "--list", "lot-7", cwd=self.main))
        # retention: the archive goes once its time is past, not before
        self.assertEqual(ha.prune(self.archives, now=time.time() + LATER), [])
        gone = ha.prune(self.archives, now=time.time() + LATER + RETENTION + 60)
        self.assertEqual([(g["kind"], g["action"]) for g in gone], [("archive", "expired")])
        self.assertFalse(os.path.exists(folder))

    def test_open_lot_kept(self):
        wt = self.dirty("lot-8")
        for state in ("build", "qa", None):
            store, entries, plan = self.run_pass(wt, {8: state})
            self.assertEqual(plan[0]["action"], "attend", (state, plan))
            self.assertTrue(os.path.isdir(wt))
        self.assertFalse(os.path.exists(self.archives))

    def test_unpushed_commits_kept(self):
        wt = self.dirty("lot-9")
        _write(os.path.join(wt, "g.txt"))
        _git("add", "g.txt", cwd=wt)
        _git("commit", "-q", "-m", "local only", cwd=wt)
        _store, entries, plan = self.run_pass(wt, {9: "merged"})
        self.assertEqual(plan[0]["action"], "garde", plan)
        self.assertIn("modified or untracked", plan[0]["detail"])
        self.assertTrue(os.path.isfile(os.path.join(wt, "g.txt")))
        self.assertFalse(os.path.exists(self.archives))

    def test_recent_end_activity_or_no_policy_kept(self):
        wt = self.dirty("lot-10")
        for label, kw in (("lot ended recently", dict(ended_ago=AFTER - 600)),
                          ("worktree touched recently", dict(idle=AFTER - 600)),
                          ("policy not declared", dict(archive=False))):
            with self.subTest(label):
                _store, entries, plan = self.run_pass(wt, {10: "merged"}, **kw)
                self.assertEqual(plan[0]["action"], "garde", plan)
                self.assertEqual([e for e in entries if e["action"] == "archived"], [])
                self.assertTrue(os.path.isdir(wt))
        self.assertFalse(os.path.exists(self.archives))

    def test_too_big_or_nested_repository_kept_and_nothing_left(self):
        wt = self.dirty("lot-11")
        with mock.patch.object(ha, "MAX_BYTES", 4):
            _store, _e, plan = self.run_pass(wt, {11: "merged"})
        self.assertEqual(plan[0]["action"], "garde", plan)
        self.assertIn("not archived", plan[0]["detail"])
        self.assertTrue(os.path.isfile(os.path.join(wt, "notes", "todo.md")))
        self.assertFalse(os.path.exists(self.archives) and os.listdir(self.archives))
        subprocess.run(["git", "init", "-q", os.path.join(wt, "notes", "inner")], check=True)
        _store, _e, plan = self.run_pass(wt, {11: "merged"})
        self.assertEqual(plan[0]["action"], "garde", plan)
        self.assertIn("neither a file nor a link", plan[0]["detail"])
        self.assertTrue(os.path.isdir(wt))
        self.assertFalse(os.path.exists(self.archives) and os.listdir(self.archives))


    def test_conflicted_index_refused(self):
        wt = self.add_worktree("lot-12")
        _write(os.path.join(self.main, "f.txt"), text="main side\n")
        _git("add", "f.txt", cwd=self.main)
        _git("commit", "-q", "-m", "main side", cwd=self.main)
        _write(os.path.join(wt, "f.txt"), text="lot side\n")
        _git("commit", "-q", "-am", "lot side", cwd=wt)
        subprocess.run(["git", "-C", wt, "merge", "-q", "main"], capture_output=True,
                       env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))
        with self.assertRaisesRegex(ha.ArchiveRefused, "unmerged"):
            ha.archive(wt, self.archives, retention_s=RETENTION)
        self.assertFalse(os.path.exists(self.archives) and os.listdir(self.archives))


class PolicyTest(unittest.TestCase):
    def test_keys_parsed_and_longest_wins(self):
        parsed, problems, unknown = menage.parse_policy(
            {"dirty_worktree_after": "48h", "dirty_archive_retention": "14d"})
        self.assertEqual((problems, unknown), ([], []))
        self.assertEqual(parsed["dirty_worktree_after"], 48 * 3600.0)
        _parsed, problems, _u = menage.parse_policy({"dirty_worktree_after": "soon"})
        self.assertTrue(problems)
        self.assertIsNone(menage.Policy().dirty_worktree_after)


class UnremovableOnceTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM housekeeping_log")

    def test_unremovable_counts_once_per_entry_with_latest_size(self):
        hk = storage.of(self.db).housekeeping
        line = {"kind": "tmp", "action": "unremovable", "detail": "quota held"}
        for size in (1000, 1200, 1300):
            hk.log("pc", [dict(line, path="/s/.menage/tmp/deepseek6", bytes=size)], actor="t")
        hk.log("pc", [dict(line, path="/s/.menage/tmp/codex1", bytes=70)], actor="t")
        hk.log("pc", [{"kind": "tmp", "action": "deleted", "path": "/s/x", "bytes": 5},
                      {"kind": "tmp", "action": "deleted", "path": "/s/x", "bytes": 5}],
               actor="t")
        rows = {r["action"]: r for r in hk.summary("pc", 86400.0) if r["kind"] == "tmp"}
        self.assertEqual((rows["unremovable"]["n"], rows["unremovable"]["bytes"]), (2, 1370))
        self.assertEqual((rows["deleted"]["n"], rows["deleted"]["bytes"]), (2, 10))


if __name__ == "__main__":
    unittest.main()
