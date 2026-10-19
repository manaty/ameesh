# SPDX-License-Identifier: AGPL-3.0-only
"""#2556 — un passage ne parcourt les copies git d'une entrée /tmp qu'une fois.

N1 de la relecture dev7 de #2525 : sur un dossier d'agent absent du registre,
`_run_pass` lisait `_git_copies_report`, puis `tmp_git_work`,
`tmp_recovery_command` et `recover_tmp_git_copies` le relisaient chacun. Le
rapport (copies, doute) est maintenant calculé une fois et transmis ; les
gardes et leurs réponses ne changent pas.
"""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from ameesh import housekeeping as menage

from .support import PgTestCase
from .test_l73_housekeeping import (_Cfg, _age_tree, _git, _pose_system_tmp,
                                    _repo, _sans_processus, _write)


class CopiesOnceTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        for table in ("housekeeping_log", "managed_worktrees", "host_resources"):
            self.db.execute("DELETE FROM %s" % table)
        self.cfgl = _Cfg(self.state)
        self.cfgl.host = self.cfg.host

    def _orphan(self, dirty: bool) -> tuple:
        """Un dossier de notre mesh, agent absent, avec un worktree (sale ou
        propre et poussé) au fond d'une entrée profonde."""
        _sans_processus(self)
        pol = menage.Policy()
        troot = menage.tmp_root(self.cfgl, pol)
        menage._ensure_root(troot)
        main = _repo(self.tmp)
        root = os.path.join(troot, "a1+abcd2556")
        deep = root
        for index in range(20):          # un parcours coûteux, à ne pas refaire
            deep = os.path.join(deep, "d%02d" % index)
        os.makedirs(deep)
        menage._write_owner(troot, "a1+abcd2556", menage.mesh_key(self.cfgl),
                            menage.ORIGIN_CREATED)
        wt = os.path.join(deep, "wt")
        _git("worktree", "add", "-q", wt, "-b", "wt-2556", cwd=main)
        if dirty:
            _write(os.path.join(wt, "brouillon.txt"), 10)
        else:
            _git("push", "-q", "origin", "wt-2556", cwd=main)
        _age_tree(root, 3 * 3600)
        return pol, troot, root, wt

    def _run_counting(self, pol) -> tuple:
        """Le rapport du passage et les chemins relus par `_git_copies_report`."""
        seen: list[str] = []
        real = menage._git_copies_report

        def counted(path, limit=None):
            seen.append(path)
            return real(path, limit)

        with mock.patch.object(menage, "_git_copies_report", side_effect=counted):
            report = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t",
                                     now=time.time() + 3 * 3600)
        return report, seen

    def test_a_dirty_orphan_reads_its_copies_once(self) -> None:
        pol, _troot, root, _wt = self._orphan(dirty=True)
        report, seen = self._run_counting(pol)
        self.assertEqual(seen.count(root), 1, seen)
        self.assertTrue(os.path.isdir(root))
        kept = [note for note in report["skipped"]
                if note.startswith("tmp %s kept: git work —" % root)]
        self.assertEqual(len(kept), 1, report["skipped"])

    def test_a_safe_orphan_reads_its_copies_once_and_is_recovered(self) -> None:
        pol, _troot, root, _wt = self._orphan(dirty=False)
        report, seen = self._run_counting(pol)
        self.assertEqual(seen.count(root), 1, seen)
        self.assertFalse(os.path.isdir(root), report["skipped"])
        self.assertIn("git worktrees recovered",
                      [e for e in report["entries"] if e.get("path") == root][0]["detail"])

    def test_an_unowned_entry_builds_its_recovery_command_once(self) -> None:
        pol = menage.Policy()
        troot = menage.tmp_root(self.cfgl, pol)
        menage._ensure_root(troot)
        root = os.path.join(troot, "nexlink-dev1-2556")
        os.makedirs(root)
        _write(os.path.join(root, ".git"), 10)
        loin = time.time() + 30 * 24 * 3600
        seen: list[str] = []
        real = menage._git_copies_report

        def counted(path, limit=None):
            seen.append(path)
            return real(path, limit)

        with mock.patch.object(menage, "_git_copies_report", side_effect=counted):
            report = menage.run_pass(self.cfgl, self.db, pol, host="pc", actor="t",
                                     now=loin)
        self.assertEqual(seen.count(root), 1, seen)
        self.assertIn(root, [row["path"] for row in report["unowned"]])
        self.assertTrue(report["unowned"][0]["command"].startswith("ls -la -- ")
                        or "git -C" in report["unowned"][0]["command"])

    def test_helpers_reuse_the_report_and_keep_the_doubt(self) -> None:
        path = os.path.join(self.tmp, "entree")
        os.makedirs(path)
        report = ([], "budget épuisé")
        with mock.patch.object(menage, "_git_copies_report",
                               side_effect=AssertionError("relu")):
            self.assertEqual(menage.tmp_git_work(path, report=report),
                             "copies not fully inspected (budget épuisé)")
            self.assertEqual(menage.tmp_recovery_command(path, report=report),
                             "ls -la -- %s" % path)
            ok, error, repos = menage.recover_tmp_git_copies(path, report=report)
        self.assertFalse(ok)
        self.assertIn("not fully inspected", error)
        self.assertEqual(repos, [])


if __name__ == "__main__":
    unittest.main()
