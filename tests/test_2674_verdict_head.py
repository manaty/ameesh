# SPDX-License-Identifier: AGPL-3.0-only
"""#2674 — un verdict nomme la tête relue, ou il est refusé à l'écriture.

Cas #2520 : l'avis `ok` de dev7 du 19/10 04:12 portait un `sha` vide.
`merge_queue.review_gate` ne compte que les avis sur la tête gelée, donc le
lot a été écarté des paquets h-0700 et h-0900 pour avis manquant, et
l'auditeur a cru qu'un avis DeepSeek manquait. Le jalon verdict exige
désormais une tête complète, ou une abréviation d'au moins 12 caractères
résolue dans un dépôt (ou dans la tête gelée courante) ; sans elle, le refus
est nommé et propose la tête gelée. Les anciens avis sans SHA restent
lisibles, et `work show` les signale une fois « avis non rattaché à une
tête ».
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from ameesh import storage, work

from .support import PgTestCase

FROZEN = "4f2519bf3ae4" + "0" * 28
OTHER = "abcdef123456" + "0" * 28
ENV_GIT = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")


def git(repo, *args):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          env=ENV_GIT)
    if proc.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


class ReviewedHeadTest(PgTestCase):
    """Le jalon verdict exige une tête, et la refuse quand elle ne compte pas."""

    def setUp(self):
        super().setUp()
        self.lot = work.add(self.db, title="un lot", app="ameesh",
                            actor="auteur")["id"]
        work.move(self.db, self.lot, "build")
        work.milestone(self.db, self.lot, "frozen", sha=FROZEN, actor="auteur")

    def milestone(self, *args):
        return self.mesh("work", "milestone", str(self.lot), "verdict", *args,
                         "--actor", "relecteur")

    def verdicts(self):
        return [m for m in work.milestones(self.db, self.lot)
                if m["kind"] == "verdict"]

    def test_sans_sha_le_jalon_est_refuse_et_la_tete_gelee_proposee(self):
        proc = self.milestone("ok")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("head-missing", proc.stderr)
        self.assertIn("--sha", proc.stderr)
        self.assertIn(FROZEN[:12], proc.stderr)
        self.assertEqual(self.verdicts(), [])

    def test_une_abreviation_trop_courte_est_refusee(self):
        proc = self.milestone("ok", "--sha", FROZEN[:7])
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("head-missing", proc.stderr)
        self.assertIn("12 hex", proc.stderr)
        self.assertIn(FROZEN[:12], proc.stderr)
        self.assertEqual(self.verdicts(), [])

    def test_une_abreviation_non_resolue_est_refusee(self):
        proc = self.milestone("ok", "--sha", "deadbeefdead")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("head-unresolved", proc.stderr)
        self.assertIn("deadbeefdead", proc.stderr)
        self.assertIn(FROZEN[:12], proc.stderr)
        self.assertEqual(self.verdicts(), [])

    def test_une_abreviation_sans_depot_ni_gel_est_refusee(self):
        lot = work.add(self.db, title="pas encore gelé", app="ameesh",
                       actor="auteur")["id"]
        proc = self.mesh("work", "milestone", str(lot), "verdict", "ok",
                         "--sha", "deadbeefdead", "--actor", "relecteur")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("head-unresolved", proc.stderr)
        self.assertIn("no frozen head yet", proc.stderr)

    def test_une_abreviation_sur_la_tete_gelee_est_acceptee(self):
        proc = self.milestone("ok", "--sha", FROZEN[:12])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([m["sha"] for m in self.verdicts()], [FROZEN[:12]])

    def test_une_tete_complete_est_acceptee_sans_depot(self):
        # Le cas d'un relecteur qui n'a pas le dépôt : la tête complète suffit.
        proc = self.milestone("blocked", "--sha", OTHER.upper())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([m["sha"] for m in self.verdicts()], [OTHER])

    def test_directement_le_milestone_refuse_aussi(self):
        with self.assertRaises(work.WorkError) as caught:
            work.milestone(self.db, self.lot, "verdict", verdict="ok")
        self.assertIn("head-missing", str(caught.exception))

    def test_avec_un_depot_nomme_l_abreviation_doit_s_y_resoudre(self):
        repo = tempfile.mkdtemp(prefix="ameesh-2674-")
        self.addCleanup(shutil.rmtree, repo, True)
        subprocess.run(["git", "init", "-q", repo], check=True, env=ENV_GIT,
                       capture_output=True)
        with open(os.path.join(repo, "f"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        git(repo, "add", "f")
        git(repo, "commit", "-qm", "un")
        head = git(repo, "rev-parse", "HEAD")
        work.milestone(self.db, self.lot, "frozen", sha=head, actor="auteur")

        proc = self.milestone("ok", "--sha", head[:12], "--repo", repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([m["sha"] for m in self.verdicts()], [head[:12]])

        proc = self.milestone("blocked", "--sha", "deadbeefdead", "--repo", repo)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("head-unresolved", proc.stderr)
        self.assertIn(repo, proc.stderr)
        self.assertIn(head[:12], proc.stderr)


class LegacyVerdictShowTest(PgTestCase):
    """Les avis d'avant la règle restent lisibles, signalés une seule fois."""

    def setUp(self):
        super().setUp()
        self.lot = work.add(self.db, title="un lot ancien", app="ameesh",
                            actor="auteur")["id"]

    def legacy(self, sha=""):
        return storage.of(self.db).work.add_milestone(
            self.lot, "verdict", sha=sha, actor="dev7", verdict="ok", note="")

    def test_work_show_signale_les_anciens_avis_sans_sha(self):
        self.legacy()
        self.legacy()
        proc = self.mesh("work", "show", str(self.lot))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("avis non rattaché à une tête ×2", proc.stdout)
        self.assertIn("porte de fusion", proc.stdout)

    def test_un_avis_qui_nomme_une_tete_n_est_pas_signale(self):
        self.legacy(sha=FROZEN[:12])
        self.legacy()                       # seul celui sans sha est signalé
        proc = self.mesh("work", "show", str(self.lot))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("avis non rattaché à une tête", proc.stdout)
        self.assertNotIn("×2", proc.stdout)


if __name__ == "__main__":
    unittest.main()
