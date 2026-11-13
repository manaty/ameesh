# SPDX-License-Identifier: AGPL-3.0-only
"""#7007 — un trou de numéro de migration est refusé à la composition.

Constat : paquet Nexlink 570 (11/11 22:52Z), migration 784 de #6953 sur une
cible arrêtée à 782 alors que 783 était réservé par #6943 sans être fusionné.
La sélection ne cherchait que les collisions : le paquet a été refusé par la
vérification SQL à 23:14Z, puis scindé et renuméroté à la main (1 h 30 de
chemin critique). Le contrôle de composition refuse désormais le candidat
(`migration-gap`) : la suite au-dessus de la tête doit être contiguë, sinon la
migration réservée arrive plus tard sous le sommet du registre et n'est jamais
appliquée.

Témoins : 784 sur une cible à 782 est écarté avec le numéro manquant ; 783
apporté par un autre lot prêt comble le trou et les deux restent prêts ; un
candidat écarté pour un numéro sous la tête rouvre le trou qu'il comblait
(point fixe) ; un numéro juste au-dessus de la tête et un lot sans migration
restent prêts.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from ameesh import packet_candidates

MIG = "src/ameesh/migrations"
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")


def _git(root: str, *args: str) -> str:
    return subprocess.run(["git", "-C", root, *args], check=True, env=ENV,
                          capture_output=True, text=True).stdout.strip()


def _commit(root: str, files: dict[str, str], message: str) -> str:
    for path, text in files.items():
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


class MigrationGapTest(unittest.TestCase):
    """Le banc : `main` porte 0780 et 0782 ; les branches lots ajoutent au-dessus."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-7007-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.name", "banc")
        _git(self.repo, "config", "user.email", "banc@example.invalid")
        _git(self.repo, "config", "commit.gpgsign", "false")
        self.base = _commit(self.repo, {
            MIG + "/0780_old.sql": "-- 780\n",
            MIG + "/0782_landed.sql": "-- 782\n",
        }, "cible : 782")

    def branch(self, lot: int, files: dict[str, str]) -> str:
        _git(self.repo, "switch", "-q", "-c", "lot/%d" % lot, "main")
        tip = _commit(self.repo, files, "#%d" % lot)
        _git(self.repo, "switch", "-q", "main")
        return tip

    def check(self, sources: list[tuple[int, str]]) -> dict[int, dict]:
        candidates = [{
            "work_item": lot,
            "ready": True,
            "error": "",
            "causes": [],
            "source": tip,
            "files": [],
            "priority": 2,
            "created_ts": float(lot),
        } for lot, tip in sources]
        packet_candidates._migration_collisions(
            None, candidates, where=self.repo, package_base=self.base,
            directory=MIG, slug="manaty/ameesh", target_branch="main")
        return {entry["work_item"]: entry for entry in candidates}

    def gaps(self, entry: dict) -> list[dict]:
        return [cause for cause in entry["causes"]
                if cause["code"] == "migration-gap"]

    def test_number_784_on_a_target_at_782_is_excluded(self):
        # Le témoin : 783 réservé par un autre lot, non fusionné et pas prêt.
        tip = self.branch(7007, {MIG + "/0784_initial.sql": "-- 784\n"})
        entry = self.check([(7007, tip)])[7007]
        self.assertFalse(entry["ready"])
        (cause,) = self.gaps(entry)
        self.assertEqual(
            (cause["case"], cause["number"], cause["missing"], cause["target_head"]),
            ("gap", 784, 783, 782))
        self.assertIn("leaves a gap after main (782), missing 783", cause["detail"])
        self.assertIn("0784_initial.sql", cause["detail"])
        self.assertEqual(entry["migrations"], {"784": [MIG + "/0784_initial.sql"]})

    def test_a_ready_candidate_at_783_fills_the_gap(self):
        # Tous les porteurs de migration d'un paquet voyagent ensemble : 783
        # apporté par un lot prêt ne laisse pas de trou devant 784.
        filler = self.branch(7007, {MIG + "/0783_filler.sql": "-- 783\n"})
        later = self.branch(7008, {MIG + "/0784_initial.sql": "-- 784\n"})
        entries = self.check([(7007, filler), (7008, later)])
        self.assertTrue(entries[7007]["ready"], entries[7007]["causes"])
        self.assertTrue(entries[7008]["ready"], entries[7008]["causes"])
        self.assertEqual(self.gaps(entries[7008]), [])

    def test_excluding_a_filler_reopens_the_gap(self):
        # 7007 comble 783 mais porte aussi 770, sous la tête : il est écarté.
        # 784 n'a alors plus de 783 devant lui et doit être écarté au tour
        # suivant, même si le premier calcul le voyait contigu.
        filler = self.branch(7007, {
            MIG + "/0770_backfill.sql": "-- 770\n",
            MIG + "/0783_filler.sql": "-- 783\n",
        })
        later = self.branch(7008, {MIG + "/0784_initial.sql": "-- 784\n"})
        entries = self.check([(7007, filler), (7008, later)])
        self.assertFalse(entries[7007]["ready"])
        self.assertFalse(entries[7008]["ready"])
        (behind,) = self.gaps(entries[7007])
        self.assertEqual((behind["case"], behind["number"]), ("behind", 770))
        (gap,) = self.gaps(entries[7008])
        self.assertEqual((gap["case"], gap["number"], gap["missing"]),
                         ("gap", 784, 783))

    def test_number_just_above_the_head_stays_ready(self):
        tip = self.branch(7007, {MIG + "/0783_next.sql": "-- 783\n"})
        entry = self.check([(7007, tip)])[7007]
        self.assertTrue(entry["ready"], entry["causes"])

    def test_a_plain_candidate_is_not_read(self):
        tip = self.branch(7007, {"src/ameesh/plain.py": "x\n"})
        entry = self.check([(7007, tip)])[7007]
        self.assertTrue(entry["ready"], entry["causes"])
        self.assertEqual(entry["migrations"], {})


if __name__ == "__main__":
    unittest.main()
