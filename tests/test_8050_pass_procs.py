# SPDX-License-Identifier: AGPL-3.0-only
"""#8050 : la table des processus est lue au plus une fois par passage de
ménage, et seulement quand un dossier temporaire est à juger.

Constat (#8048) : sur un runner CI chargé, la lecture ajoutée par le balayage
de /tmp et /dev/shm (#8020) coûtait 5 à 6 s à un passage dont les racines
étaient vides. Le journal du passage porte désormais le nombre de lectures et
leur durée (`tmp_sweep.procs_reads`, `procs_s`).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from ameesh import housekeeping as menage
from ameesh import platform

from .support import PgTestCase
from .test_l73_housekeeping import _Cfg


def _counting():
    calls = []

    def lecture(*args, **kwargs):
        calls.append(kwargs.get("allow"))
        return platform.UserPaths()
    return calls, lecture


class LectureParesseuseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ameesh-8050-")
        self.addCleanup(subprocess.run, ["rm", "-rf", self.base], check=False)
        self.tmp = os.path.join(self.base, "tmp")
        os.makedirs(self.tmp)
        self.now = time.time()
        self.pol = menage.Policy(sweep_age=3600, unreadable_ok=("btop",))
        patcher = mock.patch.object(menage, "tmp_holder", lambda *a, **k: "")
        patcher.start()
        self.addCleanup(patcher.stop)

    def dossier(self, nom: str) -> str:
        path = os.path.join(self.tmp, nom)
        os.makedirs(path)
        return path

    def scan(self, vieux: set):
        def lecture(path, limit=menage.WALK_LIMIT, budget=None, now=None):
            return {"bytes": 10, "complete": True, "why": "",
                    "latest": self.now - (7200 if path in vieux else 10)}
        return lecture

    def test_racines_sans_candidat_ne_lisent_pas_la_table(self):
        self.dossier("ameesh-recent")
        self.dossier("autre-old")
        calls, lecture = _counting()
        procs = menage.PassProcs(self.pol.unreadable_ok)
        with mock.patch.object(menage.platform, "user_paths", lecture), \
                mock.patch.object(menage, "scan_activity", self.scan(set())):
            out = menage.tmp_sweep(self.pol, roots=[self.tmp], now=self.now,
                                   procs=procs)
        self.assertEqual(calls, [])
        self.assertEqual(procs.reads, 0)
        self.assertEqual(out["doubt"], "")

    def test_une_seule_lecture_pour_plusieurs_candidats_et_le_passage(self):
        vieux = {self.dossier("ameesh-a"), self.dossier("ameesh-b"),
                 self.dossier("ameesh-c")}
        calls, lecture = _counting()
        procs = menage.PassProcs(self.pol.unreadable_ok)
        with mock.patch.object(menage.platform, "user_paths", lecture), \
                mock.patch.object(menage, "scan_activity", self.scan(vieux)):
            procs.get()                     # phase 1 du passage
            out = menage.tmp_sweep(self.pol, roots=[self.tmp], now=self.now,
                                   procs=procs)
        self.assertEqual(calls, [("btop",)])
        self.assertEqual(procs.reads, 1)
        self.assertEqual({d["path"] for d in out["deleted"]}, vieux)

    def test_table_illisible_au_premier_candidat_rien_supprime(self):
        vieux = self.dossier("ameesh-old")
        with mock.patch.object(menage.platform, "user_paths",
                               return_value=platform.UserPaths(available=False)), \
                mock.patch.object(menage, "scan_activity", self.scan({vieux})):
            out = menage.tmp_sweep(self.pol, roots=[self.tmp], now=self.now,
                                   procs=menage.PassProcs())
        self.assertTrue(os.path.isdir(vieux))
        self.assertIn("unreadable", out["doubt"])
        self.assertEqual(out["deleted"], [])

    def test_une_lecture_qui_leve_est_un_doute(self):
        vieux = self.dossier("ameesh-old")
        with mock.patch.object(menage.platform, "user_paths",
                               side_effect=OSError("proc")), \
                mock.patch.object(menage, "scan_activity", self.scan({vieux})):
            out = menage.tmp_sweep(self.pol, roots=[self.tmp], now=self.now)
        self.assertTrue(os.path.isdir(vieux))
        self.assertIn("unreadable", out["doubt"])


class JournalDuPassageTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for table in ("housekeeping_log", "managed_worktrees", "host_resources"):
            self.db.execute("DELETE FROM " + table)
        sandbox = tempfile.TemporaryDirectory(dir=self.tmp)
        self.addCleanup(sandbox.cleanup)
        self.root = sandbox.name
        system_tmp = os.path.join(self.root, "system-tmp")
        os.mkdir(system_tmp)
        patch = mock.patch.dict(os.environ, AMEESH_SYSTEM_TMP=system_tmp)
        patch.start()
        self.addCleanup(patch.stop)
        self.cfgl = _Cfg(os.path.join(self.root, "state"))
        self.policy = menage.Policy(worktree_roots=(self.root,), artifacts=(),
                                    deps=False)

    def test_un_passage_sans_dossier_a_juger_ne_lit_pas_la_table(self):
        calls, lecture = _counting()
        with mock.patch.object(menage.platform, "user_paths", lecture):
            report = menage.run_pass(self.cfgl, self.db, self.policy, host="pc",
                                     actor="test", now=time.time(),
                                     artifact_scan=False)
        self.assertEqual(calls, [])
        sweep = report["measure"]["tmp_sweep"]
        self.assertEqual(sweep["procs_reads"], 0)
        self.assertEqual(sweep["procs_s"], 0)


if __name__ == "__main__":
    unittest.main()
