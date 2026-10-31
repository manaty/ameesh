# SPDX-License-Identifier: AGPL-3.0-only
"""#4234 : le ménage d'hôte ne se tait plus et montre le volume des arbres
gardés pour du travail non commité.

Une passe longue (worktrees mesurés à 394 s sur ameesh-agents-1) ne doit pas
ressembler à une sortie vide ; le relevé de volume est borné, trié du plus
gros au plus petit, et ne supprime rien.
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest

from ameesh import housekeeping as hk


class KeptWorktreeSizesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ameesh-4234-")
        self.addCleanup(temporary.cleanup)
        self.tmp = temporary.name

    def _tree(self, name: str, kib: int) -> str:
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        with open(os.path.join(path, "work.patch"), "wb") as fh:
            fh.write(b"x" * (kib * 1024))
        return path

    def test_biggest_first_includes_clean_kept_and_waiting_worktrees(self):
        small = self._tree("small", 1)
        big = self._tree("big", 64)
        clean = self._tree("clean", 32)
        waiting = self._tree("waiting", 16)
        plan = [
            {"kind": "worktree", "path": small, "action": "garde",
             "detail": "lot #1 merged: kept — 1 file(s) modified or untracked"},
            {"kind": "worktree", "path": big, "action": "garde",
             "detail": "lot #2 merged: kept — 7 file(s) modified or untracked"},
            {"kind": "worktree", "path": clean, "action": "garde",
             "detail": "lot #3 merged: kept — HEAD abcdef0 not integrated"},
            {"kind": "worktree", "path": clean, "action": "attend",
             "detail": "duplicate entry"},
            {"kind": "worktree", "path": waiting, "action": "attend",
             "detail": "lot build"},
            {"kind": "worktree", "path": big, "action": "retrait",
             "detail": "clean and pushed"},
            {"kind": "clone", "path": big, "action": "garde",
             "detail": "3 file(s) modified or untracked"},
        ]
        sizes = hk.kept_worktree_sizes(plan)
        self.assertEqual(sizes["dirty"], 2)
        self.assertEqual(sizes["kept"], 4)
        self.assertEqual(sizes["measured"], 4)
        self.assertEqual(sizes["unmeasured"], 0)
        self.assertEqual([row["path"] for row in sizes["top"]], [big, clean, waiting, small])
        self.assertGreater(sizes["top"][0]["bytes"], sizes["top"][1]["bytes"])
        self.assertEqual(sizes["total_bytes"],
                         sum(row["bytes"] for row in sizes["top"]))

    def test_the_scan_is_bounded_and_says_what_it_did_not_measure(self):
        first = self._tree("first", 1)
        second = self._tree("second", 1)
        plan = [{"kind": "worktree", "path": path, "action": "garde",
                 "detail": "kept — 1 file(s) modified or untracked"}
                for path in (first, second)]
        sizes = hk.kept_worktree_sizes(plan, seconds=0.0)
        self.assertEqual(sizes["dirty"], 2)
        self.assertEqual(sizes["kept"], 2)
        self.assertEqual(sizes["measured"], 0)
        self.assertEqual(sizes["unmeasured"], 2)
        self.assertEqual(sizes["top"], [])


class ReportShowsKeptVolumeTest(unittest.TestCase):
    def test_clean_nonintegrated_tree_is_visible_without_dirty_trees(self):
        report = {"host": "pc", "dry_run": True, "entries": [], "plan": [],
                  "skipped": [], "worktree_sizes": {
                      "dirty": 0, "kept": 1, "measured": 1, "unmeasured": 0,
                      "total_bytes": 1024, "seconds": 0.1, "top": [],
                      "archive_declared": True}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hk.print_report(report)
        self.assertIn("kept worktrees by volume", out.getvalue())
        self.assertIn("1 of 1 measured", out.getvalue())
        self.assertIn("inactivity and safety guards still apply", out.getvalue())

    def test_the_report_prints_the_biggest_kept_worktrees(self):
        report = {"host": "pc", "dry_run": True, "status": "ok",
                  "entries": [], "plan": [], "skipped": [], "orphans": [],
                  "unowned": [], "measure": None,
                  "worktree_sizes": {
                      "dirty": 2, "measured": 2, "unmeasured": 0,
                      "total_bytes": 5 * 2 ** 30, "seconds": 3.4,
                      "top": [{"path": "/w/big", "bytes": 4 * 2 ** 30,
                               "complete": True,
                               "detail": "lot #1 merged: kept — 3 file(s) "
                                         "modified or untracked"},
                              {"path": "/w/small", "bytes": 1 * 2 ** 30,
                               "complete": True,
                               "detail": "lot #2 merged: kept — 1 file(s) "
                                         "modified or untracked"}],
                      "archive_declared": False}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hk.print_report(report)
        text = out.getvalue()
        self.assertIn("kept dirty worktrees by volume (biggest first", text)
        self.assertLess(text.index("/w/big"), text.index("/w/small"))
        self.assertIn("total measured 5.0 GiB", text)
        self.assertIn("archive policy NOT declared", text)


if __name__ == "__main__":
    unittest.main()
