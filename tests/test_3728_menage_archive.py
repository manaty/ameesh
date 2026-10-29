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


class _ArchiveRepo(_Repo):
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


class ArchiveThenRemoveTest(_ArchiveRepo):
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

    def test_unpushed_commits_archived_with_the_policy_kept_without(self):
        """#4442 : la politique déclarée couvre aussi les commits absents de
        la forge (`HEAD.bundle`) ; sans elle, le worktree reste gardé."""
        wt = self.dirty("lot-9")
        _write(os.path.join(wt, "g.txt"))
        _git("add", "g.txt", cwd=wt)
        _git("commit", "-q", "-m", "local only", cwd=wt)
        _store, entries, plan = self.run_pass(wt, {9: "merged"})
        self.assertEqual(plan[0]["action"], "retrait", plan)
        archived, = [e for e in entries if e["action"] == "archived"]
        self.assertEqual(archived["data"]["commits"], 1)
        self.assertTrue(os.path.isfile(os.path.join(
            archived["data"]["archive"], "HEAD.bundle")))
        self.assertFalse(os.path.exists(wt))
        # sans politique, rien n'est archivé ni retiré
        wt2 = self.dirty("lot-19")
        _write(os.path.join(wt2, "g.txt"))
        _git("add", "g.txt", cwd=wt2)
        _git("commit", "-q", "-m", "local only", cwd=wt2)
        _store, entries, plan = self.run_pass(wt2, {19: "merged"}, archive=False)
        self.assertEqual(plan[0]["action"], "garde", plan)
        self.assertIn("commits not pushed", plan[0]["detail"])
        self.assertTrue(os.path.isfile(os.path.join(wt2, "g.txt")))

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
            store, entries, plan = self.run_pass(wt, {11: "merged"})
        self.assertEqual(plan[0]["action"], "garde", plan)
        self.assertIn("not archived", plan[0]["detail"])
        # dev3 N3: the refusal is persisted, not only planned
        self.assertEqual(store.statuses[-1][1], "kept")
        self.assertIn("not archived", store.statuses[-1][2])
        kept, = [e for e in entries if e["action"] == "kept"]
        self.assertIn("not archived", kept["detail"])
        self.assertTrue(os.path.isfile(os.path.join(wt, "notes", "todo.md")))
        self.assertFalse(os.path.exists(self.archives) and os.listdir(self.archives))
        subprocess.run(["git", "init", "-q", os.path.join(wt, "notes", "inner")], check=True)
        _store, _e, plan = self.run_pass(wt, {11: "merged"})
        self.assertEqual(plan[0]["action"], "garde", plan)
        self.assertIn("neither a file nor a link", plan[0]["detail"])
        self.assertTrue(os.path.isdir(wt))
        self.assertFalse(os.path.exists(self.archives) and os.listdir(self.archives))


    def test_archive_root_unusable_keeps_and_pass_goes_on(self):
        # dev3 B1: a file where the archive root goes (or ENOSPC, EACCES)
        wt = self.dirty("lot-13")
        os.makedirs(os.path.dirname(self.archives), exist_ok=True)
        _write(self.archives, text="in the way\n")
        store, _e, plan = self.run_pass(wt, {13: "merged"})
        self.assertEqual(plan[0]["action"], "garde", plan)
        self.assertIn("archive directory not created", plan[0]["detail"])
        self.assertTrue(os.path.isfile(os.path.join(wt, "notes", "todo.md")))
        self.assertEqual(store.statuses[-1][1], "kept")

    def test_single_layer_archives_restore_with_the_manifest_command(self):
        # dev3 B2: `git apply` of an empty patch exits 128; dev3 N1: the
        # patches count against the size limit
        for label, staged, unstaged, untracked in (
                ("unstaged only", False, True, False), ("staged only", True, False, False),
                ("untracked only", False, False, True)):
            with self.subTest(label):
                branch = "lot-%d" % (20 + len(label))
                wt = self.add_worktree(branch)
                head = _git("rev-parse", "HEAD", cwd=wt).strip()
                if staged or unstaged:
                    _write(os.path.join(wt, "f.txt"), text="%s\n" % label)
                if staged:
                    _git("add", "f.txt", cwd=wt)
                if untracked:
                    _write(os.path.join(wt, "notes.md"), text="draft\n")
                with self.assertRaisesRegex(ha.ArchiveRefused, "bytes to archive"):
                    ha.archive(wt, self.archives, retention_s=RETENTION, max_bytes=4)
                made = ha.archive(wt, self.archives, retention_s=RETENTION, head=head)
                with open(os.path.join(made["archive"], "manifest.json")) as fh:
                    command = json.load(fh)["restore"]
                target = os.path.join(self.base, "restored-" + branch)
                proc = subprocess.run(["bash", "-c", command.replace("<path>", target)],
                                      cwd=self.main, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, (command, proc.stderr))
                self.assertEqual(
                    _git("status", "--porcelain", cwd=target),
                    _git("status", "--porcelain", cwd=wt))

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


class PanierTest(_ArchiveRepo):
    """#3734 (panier de la relecture #3728) : un témoin par ligne."""

    def restore(self, folder: str, target: str) -> None:
        with open(os.path.join(folder, "manifest.json")) as fh:
            command = json.load(fh)["restore"]
        proc = subprocess.run(["bash", "-c", command.replace("<path>", target)],
                              cwd=self.main, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, (command, proc.stderr))

    def test_line1_restore_skips_both_empty_layers(self):
        # ligne 1 : ni couche indexée ni couche de travail, rien que des
        # fichiers ignorés : la commande n'a ni `git apply` ni `&&` qui coupe
        wt = self.add_worktree("lot-31")
        _write(os.path.join(wt, ".env"), text="SECRET=31\n")
        made = ha.archive(wt, self.archives, retention_s=RETENTION,
                          head=_git("rev-parse", "HEAD", cwd=wt).strip())
        with open(os.path.join(made["archive"], "manifest.json")) as fh:
            self.assertNotIn("git -C <path> apply", json.load(fh)["restore"])
        target = os.path.join(self.base, "restored-31")
        self.restore(made["archive"], target)
        with open(os.path.join(target, ".env")) as fh:
            self.assertEqual(fh.read(), "SECRET=31\n")

    def test_line2_ignored_only_worktree_archived_then_removed(self):
        # ligne 2 (dev3 N2) : propre et poussé, gardé pour un `.env` ignoré
        # seulement : archivé puis retrait, comme un worktree modifié
        wt = self.add_worktree("lot-32")
        _write(os.path.join(wt, ".env"), text="SECRET=32\n")
        _store, _e, plan = self.run_pass(wt, {32: "merged"}, dry_run=True)
        self.assertEqual(plan[0]["action"], "archivage", plan)
        self.assertIn("kept for ignored files only", plan[0]["detail"])
        store, entries, plan = self.run_pass(wt, {32: "merged"})
        self.assertEqual(plan[0]["action"], "retrait", plan)
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(store.statuses[-1][1], "removed")
        archived, = [e for e in entries if e["action"] == "archived"]
        folder = archived["data"]["archive"]
        with tarfile.open(os.path.join(folder, "untracked.tar.gz")) as tar:
            self.assertEqual(tar.getnames(), [".env"])
        target = os.path.join(self.base, "restored-32")
        self.restore(folder, target)
        with open(os.path.join(target, ".env")) as fh:
            self.assertEqual(fh.read(), "SECRET=32\n")

    def test_line2_same_guards_keep_it(self):
        wt = self.add_worktree("lot-33")
        _write(os.path.join(wt, ".env"), text="SECRET=33\n")
        for label, lots, kw in (
                ("lot ended recently", {33: "merged"}, dict(ended_ago=AFTER - 600)),
                ("worktree touched recently", {33: "merged"}, dict(idle=AFTER - 600)),
                ("policy not declared", {33: "merged"}, dict(archive=False)),
                ("lot open", {33: "build"}, {})):
            with self.subTest(label):
                _store, entries, plan = self.run_pass(wt, lots, **kw)
                self.assertIn(plan[0]["action"], ("garde", "attend"), plan)
                self.assertEqual([e for e in entries if e["action"] == "archived"], [])
                self.assertTrue(os.path.isfile(os.path.join(wt, ".env")))
        self.assertFalse(os.path.exists(self.archives) and os.listdir(self.archives))
        with mock.patch.object(ha, "MAX_BYTES", 4):
            _store, _e, plan = self.run_pass(wt, {33: "merged"})
        self.assertEqual(plan[0]["action"], "garde", plan)
        self.assertIn("not archived", plan[0]["detail"])
        self.assertTrue(os.path.isfile(os.path.join(wt, ".env")))
        self.assertFalse(os.path.exists(self.archives) and os.listdir(self.archives))

    def test_line3_cache_name_excludes_a_directory_only(self):
        # ligne 3 : un fichier ou un lien nommé comme un cache est archivé ;
        # seul un dossier de ce nom est exclu
        wt = self.add_worktree("lot-34")
        _write(os.path.join(wt, ".cache"), text="a note, not a cache\n")
        _write(os.path.join(wt, "sub", "__pycache__"), text="a file\n")
        _write(os.path.join(wt, "pkg", "__pycache__", "m.pyc"))
        _write(os.path.join(wt, "data", "real", "x"))
        os.symlink("real", os.path.join(wt, "data", ".venv"))
        self.assertEqual(ha.untracked_files(wt, ()),
                         [".cache", "data/.venv", "data/real/x", "sub/__pycache__"])


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
