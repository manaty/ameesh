# SPDX-License-Identifier: AGPL-3.0-only
"""#2102 : détacher explicitement la branche obsolète d'un lot.

Cas réel #1239 : PR fermée comme supplantée, branche `lot/1239-…` restée
liée au lot, radar de conflits sur une branche morte. Le détachement est un
geste journalisé qui garde l'état, le titulaire et la vérification restante,
ne touche pas Git, et que le relevé des fusions respecte : la branche
`lot/<id>-…` n'est plus associée implicitement (L118b). Une réassociation se
fait explicitement.
"""
from __future__ import annotations

import dataclasses
import os

from ameesh import plan_git, radar, registry, storage, work

from .support import PgTestCase
from .test_l160b_radar import LOTS, make_repo
from . import test_merged_work_item_tracking as tracking
from .test_merged_work_item_tracking import _Base, _git


class DetachSweepTest(_Base):
    """Détachement puis relevé des fusions, sur le dépôt de `MergeSweepTest`
    (ses aides seulement : ses tests tournent dans leur module)."""

    commit = tracking.MergeSweepTest.commit
    lot = tracking.MergeSweepTest.lot
    sync = tracking.MergeSweepTest.sync

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "depot")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        self.commit("socle")
        _git(self.repo, "checkout", "-q", "-b", "develop")
        _git(self.repo, "config", plan_git.TARGET_GIT_KEY, "develop")
        self.agent("claude1", cwd=self.repo)

    def branch(self, name):
        _git(self.repo, "checkout", "-q", "-b", name, "develop")
        self.commit(name.replace("/", "-"))
        _git(self.repo, "checkout", "-q", "develop")

    def blocked_lot(self):
        lot = self.lot("vérification après déploiement")
        work.move(self.db, lot["id"], "build", actor="claude1")
        work.move(self.db, lot["id"], "blocked", actor="claude1")
        return lot

    def test_detache_puis_releve_ne_reassocie_pas(self):
        lot = self.blocked_lot()
        n = lot["id"]
        self.branch("lot/%d-supplantee" % n)
        self.sync()                                  # L118b : associée implicitement
        self.assertEqual(work.get(self.db, n)["branch"], "lot/%d-supplantee" % n)
        item = work.detach_branch(self.db, n, reason="PR #9 fermée, supplantée par #8",
                                  actor="pilote")
        self.assertIsNone(item["branch"])
        self.assertEqual((item["state"], item["assignee"]), ("blocked", "claude1"))
        note = work.events(self.db, n)[0]["note"]
        self.assertTrue(note.startswith("branch detached: lot/%d-supplantee (last seen " % n),
                        note)
        self.assertIn("PR #9 fermée, supplantée par #8", note)
        self.assertEqual(work.events(self.db, n)[0]["actor"], "pilote")
        report = self.sync()                         # la branche est toujours là
        self.assertIsNone(work.get(self.db, n)["branch"])
        self.assertIsNone(report["results"][0]["branch"])
        self.assertEqual(work.get(self.db, n)["state"], "blocked")
        # Git n'est pas touché
        self.assertTrue(_git(self.repo, "rev-parse", "--verify", "lot/%d-supplantee" % n))

    def test_branche_detachee_fusionnee_en_avance_rapide_ne_ferme_pas(self):
        # sans la garde, la pointe devenue ancêtre de la cible fermait le lot
        lot = self.blocked_lot()
        n = lot["id"]
        self.branch("lot/%d-vieille" % n)
        work.detach_branch(self.db, n, "lot/%d-vieille" % n, reason="supplantée")
        _git(self.repo, "merge", "-q", "--ff-only", "lot/%d-vieille" % n)
        self.assertEqual(self.sync()["merged"], 0)
        self.assertEqual(work.get(self.db, n)["state"], "blocked")

    def test_nom_detache_d_un_lot_sans_branche_et_idempotence(self):
        # le cas #1239 : lien vidé hors CLI, le nom à ne plus deviner est donné
        lot = self.lot()
        n = lot["id"]
        work.detach_branch(self.db, n, "origin/lot/%d-morte" % n, reason="hors CLI")
        before = len(work.events(self.db, n))
        work.detach_branch(self.db, n, "lot/%d-morte" % n, reason="encore")
        self.assertEqual(len(work.events(self.db, n)), before)      # rien de réécrit
        self.branch("lot/%d-morte" % n)
        self.sync()
        self.assertIsNone(work.get(self.db, n)["branch"])
        # un autre nom reste deviné : seul le nom détaché est écarté
        _git(self.repo, "branch", "-q", "-D", "lot/%d-morte" % n)
        self.branch("lot/%d-neuve" % n)
        self.sync()
        self.assertEqual(work.get(self.db, n)["branch"], "lot/%d-neuve" % n)

    def test_prefixe_sans_confusion(self):
        lot = self.lot()
        n = lot["id"]
        work.detach_branch(self.db, n, "lot/%d-a" % n, reason="r")
        self.assertTrue(work.is_detached(storage.of(self.db), n, "lot/%d-a" % n))
        self.assertTrue(work.is_detached(storage.of(self.db), n, "origin/lot/%d-a" % n))
        self.assertFalse(work.is_detached(storage.of(self.db), n, "lot/%d-ab" % n))

    def test_refus(self):
        lot = self.lot()
        n = lot["id"]
        with self.assertRaisesRegex(work.WorkError, "reason"):
            work.detach_branch(self.db, n, "lot/%d-x" % n, reason=" ")
        with self.assertRaisesRegex(work.WorkError, "no branch"):
            work.detach_branch(self.db, n, reason="r")
        work.set_branch(self.db, n, "lot/%d-x" % n, actor="orch")
        with self.assertRaisesRegex(work.WorkError, "carries the branch"):
            work.detach_branch(self.db, n, "lot/%d-y" % n, reason="r")
        self.assertEqual(work.get(self.db, n)["branch"], "lot/%d-x" % n)

    def test_reassociation_explicite_par_la_cli(self):
        lot = self.lot()
        n = lot["id"]
        self.branch("lot/%d-reprise" % n)
        work.set_branch(self.db, n, "lot/%d-reprise" % n, "develop", actor="orch")
        proc = self.mesh("work", "branch", str(n), "--detach", "--reason", "PR fermée",
                         "--actor", "pilote")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("detached", proc.stdout)
        self.assertIsNone(work.get(self.db, n)["branch"])
        self.assertIn(", target develop)", work.events(self.db, n)[0]["note"])
        proc = self.mesh("work", "branch", str(n), "--detach", "--actor", "pilote")
        self.assertNotEqual(proc.returncode, 0)                    # raison exigée
        self.sync()
        self.assertIsNone(work.get(self.db, n)["branch"])
        proc = self.mesh("work", "branch", str(n), "lot/%d-reprise" % n, "--actor", "pilote")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        item = work.get(self.db, n)
        self.assertEqual(item["branch"], "lot/%d-reprise" % n)
        self.assertIn("explicit association after its detachment",
                      work.events(self.db, n)[0]["note"])
        self.sync()
        self.assertEqual(work.get(self.db, n)["branch"], "lot/%d-reprise" % n)


class DetachRadarTest(PgTestCase):
    """Le radar ne lit plus la branche détachée ; il la relit réassociée."""

    def test_radar_sans_la_branche_detachee(self):
        repo = make_repo(self.tmp)
        for name in ("a1", "a2"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
        lots = [work.add(self.db, title=row["title"], branch=row["branch"], assignee=who)
                for row, who in zip(LOTS[:2], ("a1", "a2"))]
        work.detach_branch(self.db, lots[1]["id"], reason="PR supplantée")
        cfg = dataclasses.replace(self.cfg, radar={"projects": {"ameesh": {"repo": repo}}})
        self.assertEqual(radar.run_pass(cfg, self.db, ci=None)[0]["mail"], [])
        work.set_branch(self.db, lots[1]["id"], LOTS[1]["branch"], actor="pilote")
        self.assertEqual(sorted(s["to"] for s in radar.run_pass(cfg, self.db, ci=None)[0]["mail"]),
                         ["a1", "a2"])

