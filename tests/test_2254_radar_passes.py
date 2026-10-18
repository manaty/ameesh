# SPDX-License-Identifier: AGPL-3.0-only
"""#2254 : le radar ne doit plus alterner `prepared`/`error` d'un passage à
l'autre.

Deux défauts se cumulaient sur un hôte où plusieurs exécuteurs déclarent
`radar.projects` :

* deux passages du même projet partagent le clone privé, le dernier rapport,
  le journal et `.notified` sans exclusion mutuelle : l'un fait son
  `checkout`/`reset` pendant la fusion d'essai de l'autre, dont le rejeu
  rend alors un `error` passager (le journal de l'hôte montre deux passages
  qui se chevauchent, ligne écrite avant celle d'un passage commencé plus
  tôt) ;
* `radar.Cache.replays` gardait cet `error` pour la clé d'entrée (pointes,
  commit de résolution, attributs), donc un exécutant restait sur `error`
  tant que les pointes ne bougeaient pas, pendant qu'un autre, qui avait
  obtenu `prepared`, gardait `prepared` : l'état alternait à chaque passage.

Les preuves ci-dessous sont ciblées : le verrou par projet (un seul passage,
le perdant s'abstient et n'écrit rien) et le cache de rejeu (un `error` est
retenté, un résultat stable est gardé).
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import unittest

from ameesh import radar, resolutions, work
from ameesh.canon import GitError

from .support import PgTestCase
from .test_l160b_radar import MIG, make_repo

LOTS = [{"id": 1, "branch": "lot/1-a", "title": "a"},
        {"id": 2, "branch": "lot/2-b", "title": "b"}]


class PassLockTest(unittest.TestCase):
    """Le verrou d'un passage : exclusif par projet, non bloquant, rendu à la
    fermeture."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2254-lock-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = dataclasses.make_dataclass("Cfg", ["state_dir"])(
            os.path.join(self.tmp, "state"))

    def test_one_pass_per_project(self):
        first = radar.pass_lock(self.cfg, "ameesh")
        self.assertIsNotNone(first)
        try:
            # un autre exécutant de l'hôte, même projet : refusé
            self.assertIsNone(radar.pass_lock(self.cfg, "ameesh"))
            # un autre projet garde son propre passage
            other = radar.pass_lock(self.cfg, "nexlink")
            self.assertIsNotNone(other)
            other.close()
        finally:
            first.close()
        # verrou rendu : le passage suivant le prend
        again = radar.pass_lock(self.cfg, "ameesh")
        self.assertIsNotNone(again)
        again.close()


class RunPassLockTest(PgTestCase):
    """Un passage déjà tenu par un autre exécutant n'est ni rejoué ni écrit :
    pas de second rapport, pas de journal, pas de courrier."""

    def test_a_pass_in_progress_is_not_duplicated(self):
        repo = make_repo(tempfile.mkdtemp(prefix="repo-", dir=self.tmp))
        for row in LOTS:
            work.add(self.db, title=row["title"], branch=row["branch"])
        cfg = dataclasses.replace(self.cfg, radar={"projects": {"ameesh": {
            "repo": repo, "migrations": MIG}}})
        held = radar.pass_lock(cfg, "ameesh")
        self.assertIsNotNone(held)
        try:
            self.assertEqual(radar.run_pass(cfg, self.db), [])
            self.assertIsNone(radar.read_snapshot(cfg, "ameesh"))
            self.assertEqual(
                self.db.query("SELECT count(*) AS n FROM agent_mailbox")[0]["n"], 0)
        finally:
            held.close()
        lines = radar.run_pass(cfg, self.db)
        self.assertEqual(len(lines), 1)
        self.assertIsNone(lines[0]["error"])
        self.assertIsNotNone(radar.read_snapshot(cfg, "ameesh"))


class _FlakyReplayer:
    """Un rejeu dont on choisit la panne : `mode` vaut `raise`, `error` ou
    `prepared`."""

    def __init__(self, mode="raise"):
        self.mode = mode
        self.calls = 0

    def ensure(self):
        pass

    def prime(self, commits, *, attributes_tree=None):
        return []

    def attributes_key(self, attributes_tree):
        return "attrs"

    def replay(self, a, b, commit, *, attributes_tree=None, export=None, generated=()):
        self.calls += 1
        if self.mode == "raise":
            raise GitError("clone disturbed by another pass")
        if self.mode == "error":
            return {"state": "error", "files": [], "remaining": [],
                    "error": "cannot merge"}
        return {"state": "prepared", "files": ["shared.py"], "remaining": []}


class ReplayCacheTest(unittest.TestCase):
    """Le cache de rejeu ne garde pas un non-résultat (#2254)."""

    def report(self):
        return {
            "repo": "/repo",
            "branches": [
                {"work_item": 1, "branch": "lot/1-a", "tip": "a" * 40,
                 "target_head": "c" * 40, "base": "b" * 40},
                {"work_item": 2, "branch": "lot/2-b", "tip": "b" * 40,
                 "target_head": "c" * 40, "base": "b" * 40},
            ],
            "pairs": [{
                "lots": [1, 2], "branches": ["lot/1-a", "lot/2-b"],
                "tips": ["a" * 40, "b" * 40], "target": "main",
                "target_head": "c" * 40, "state": "conflict",
                "shared": ["shared.py"], "files": ["shared.py"],
            }],
        }

    def rows(self):
        return [{"work_item_id": 2, "other_item_id": 1, "commit_sha": "d" * 40,
                 "ref": None, "created_by": "a1", "created_ts": 0.0}]

    def lots(self):
        return {1: {"id": 1, "priority": 1, "state": "build"},
                2: {"id": 2, "priority": 2, "state": "build"}}

    def annotate(self, cache, replayer):
        report = self.report()
        resolutions.annotate(report, self.rows(), self.lots(),
                             replayer=replayer, cache=cache, generated=())
        return report["pairs"][0]

    def test_a_raised_error_is_retried(self):
        cache, replayer = radar.Cache(), _FlakyReplayer("raise")
        first = self.annotate(cache, replayer)
        self.assertEqual((first["state"], first["resolution"]["replay"]),
                         ("conflict", "error"))
        replayer.mode = "prepared"
        second = self.annotate(cache, replayer)
        self.assertEqual((second["state"], second["resolution"]["replay"]),
                         ("prepared", "prepared"))
        self.assertEqual(replayer.calls, 2)
        # un résultat stable, lui, reste en cache
        replayer.mode = "raise"
        third = self.annotate(cache, replayer)
        self.assertEqual((third["state"], third["resolution"]["replay"]),
                         ("prepared", "prepared"))
        self.assertEqual(replayer.calls, 2)

    def test_an_error_result_is_retried(self):
        cache, replayer = radar.Cache(), _FlakyReplayer("error")
        first = self.annotate(cache, replayer)
        self.assertEqual(first["resolution"]["replay"], "error")
        replayer.mode = "prepared"
        second = self.annotate(cache, replayer)
        self.assertEqual((second["state"], second["resolution"]["replay"]),
                         ("prepared", "prepared"))
        self.assertEqual(replayer.calls, 2)


if __name__ == "__main__":
    unittest.main()
