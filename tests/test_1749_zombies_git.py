# SPDX-License-Identifier: AGPL-3.0-only
"""#1749 : le runner ne laisse plus ses descendants orphelins en zombies.

Constat du bureau (15/10) : des milliers de `git <defunct>` sous les
exécuteurs, nés d'un fetch paresseux de clone partiel dont le transport
survit à son parent, est adopté par le sous-moissonneur, puis n'est jamais
moissonné. Deux témoins, sans réseau ni clone partiel :

* un orphelin adopté est moissonné en N passes du fil `fond` ;
* l'enfant d'un `Popen` vivant garde son statut — la garantie #476, déjà
  couverte par `test_476_ownership_races` ;
* les sondes de présence (`lot_measures.has_commit`, `plan_git._commit`)
  coupent le fetch paresseux qui laissait le transport derrière elles.
"""
from __future__ import annotations

import os
import signal
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from . import support  # noqa: F401
from ameesh import background, lot_measures, plan_git, platform


class AdoptedZombieTest(unittest.TestCase):
    def tracker(self):
        runner = SimpleNamespace(db=None, host="h", harness_pids=set(),
                                 launch_lock=threading.Lock(),
                                 container_runtime=lambda: None)
        return background.Tracker(runner, log=lambda _: None)

    def mes_zombies(self) -> set:
        return {p["pid"] for p in platform.process_table()
                if p["ppid"] == os.getpid() and p["zombie"]}

    @unittest.skipUnless(platform.is_linux() and hasattr(os, "fork"),
                         "sous-moissonneur Linux")
    def test_orphelin_adopte_moissonne_apres_n_passes(self):
        tracker = self.tracker()
        self.assertTrue(tracker.subreaper, "le noyau doit adopter les orphelins")
        lecture, ecriture = os.pipe()
        intermediaire = os.fork()
        if intermediaire == 0:
            petit = os.fork()
            if petit == 0:
                os._exit(0)                     # le petit-fils meurt aussitôt
            os.write(ecriture, str(petit).encode())
            os.close(ecriture)
            os.close(lecture)
            os._exit(0)                         # ... sans le moissonner
        os.close(ecriture)
        with os.fdopen(lecture, "rb") as flux:
            petit = int(flux.read().strip())
        os.waitpid(intermediaire, 0)
        try:
            # le petit-fils est adopté par nous (sous-moissonneur), puis zombie
            fin = time.monotonic() + 5
            while os.waitid(os.P_PID, petit,
                            os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                self.assertLess(time.monotonic(), fin)
                time.sleep(.01)
            avant = self.mes_zombies()
            self.assertIn(petit, avant)
            tracker.thread = mock.Mock()        # mode service : le fil `fond`
            for _ in range(3):                  # N passes du moissonneur
                tracker._strays_at = 0.0
                tracker.tick()
            apres = self.mes_zombies()
            self.assertNotIn(petit, apres, "l'orphelin adopté doit être moissonné")
            self.assertLessEqual(len(apres), len(avant) - 1)
        finally:
            try:
                os.kill(petit, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(petit, 0)
            except ChildProcessError:
                pass


class SondesLocalesTest(unittest.TestCase):
    def sonde(self, appel, repo: str) -> str:
        """Exécute `appel` avec un faux `git` qui note `GIT_NO_LAZY_FETCH`."""
        with tempfile.TemporaryDirectory(prefix="ameesh-1749-") as tmp:
            trace = os.path.join(tmp, "trace")
            faux = os.path.join(tmp, "git")
            with open(faux, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n"
                         "printf '%s' \"$GIT_NO_LAZY_FETCH\" > \"$AMEESH_TRACE\"\n"
                         "exit 1\n")
            os.chmod(faux, 0o755)
            env = dict(os.environ, AMEESH_TRACE=trace,
                       PATH=tmp + os.pathsep + os.environ.get("PATH", ""))
            with mock.patch.dict(os.environ, env, clear=True):
                appel(repo)
            with open(trace, encoding="utf-8") as fh:
                return fh.read()

    def test_les_sondes_de_presence_coupent_le_fetch_paresseux(self):
        # #1749 : sans cela, chaque objet absent d'un clone partiel lançait un
        # fetch promisor dont le transport restait orphelin (git <defunct>).
        sha = "a" * 40
        with tempfile.TemporaryDirectory(prefix="ameesh-1749-repo-") as repo:
            self.assertEqual(self.sonde(
                lambda r: self.assertFalse(lot_measures.has_commit(r, sha)), repo), "1")
            self.assertEqual(self.sonde(
                lambda r: self.assertIsNone(plan_git._commit(r, "HEAD")), repo), "1")
