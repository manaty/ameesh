# SPDX-License-Identifier: AGPL-3.0-only
"""L122 : numéros de migration uniques, réservés par les lots.

Le premier test ne demande pas de base : il refuse deux migrations de même
numéro dans `src/ameesh/migrations` (le 2026-10-10, quatre lots ont pris
0048 à 0050) et un fichier `.sql` que `migrations.discover` ignorerait.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

from ameesh import migrations, work

from .support import PgTestCase


class NumerosUniquesTest(unittest.TestCase):
    def test_aucun_numero_en_double(self):
        names = os.listdir(migrations.MIGRATIONS_DIR)
        doubles = {"%04d" % n: files for n, files in migrations.numbered(names).items()
                   if len(files) > 1}
        self.assertEqual(doubles, {}, "migrations de même numéro : renuméroter la dernière "
                                      "venue (`ameesh work migration <lot>`, L122)")

    def test_aucun_fichier_sql_hors_format(self):
        names = [n for n in os.listdir(migrations.MIGRATIONS_DIR) if n.endswith(".sql")]
        ignored = sorted(set(names) - {f for files in migrations.numbered(names).values()
                                       for f in files})
        self.assertEqual(ignored, [], "fichier de migration hors format NNNN_nom.sql")

    def test_numbered_voit_les_doublons(self):
        self.assertEqual(migrations.numbered(["0048_a.sql", "0048_b.sql", "README", "x/0049_c.sql"]),
                         {48: ["0048_a.sql", "0048_b.sql"], 49: ["0049_c.sql"]})


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x"))


class PlusHautConnuTest(unittest.TestCase):
    def test_branches_du_depot_comptent(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = os.path.join(tmp, "paquet")
            os.makedirs(empty)
            with open(os.path.join(empty, "0003_c.sql"), "w") as fh:
                fh.write("--")
            repo = os.path.join(tmp, "depot")
            mig = os.path.join(repo, migrations.REPO_PATH)
            os.makedirs(mig)
            _git(repo, "init", "-q", "-b", "main")
            with open(os.path.join(mig, "0005_e.sql"), "w") as fh:
                fh.write("--")
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", "init")
            _git(repo, "checkout", "-qb", "lot/autre")
            with open(os.path.join(mig, "0009_i.sql"), "w") as fh:
                fh.write("--")
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", "autre")
            _git(repo, "checkout", "-q", "main")
            # 0009 n'existe que sur une autre branche : il compte quand même
            self.assertEqual(migrations.highest_known(repo, directory=empty), 9)
            # hors d'un dépôt ameesh : le paquet seul
            self.assertEqual(migrations.highest_known(empty, directory=empty), 3)


class ReservationTest(PgTestCase):
    def _lot(self, title="un lot"):
        return work.add(self.db, title=title, actor="mesh-design")

    def setUp(self):
        # aucune réservation héritée d'un autre test de la classe
        self.db.execute("UPDATE work_items SET migration = NULL")
        self.floor = migrations.highest_known(self.tmp) + 1

    def test_prochain_numero_libre_puis_idempotent(self):
        a, b = self._lot("a"), self._lot("b")
        ra = work.reserve_migration(self.db, a["id"], repo=self.tmp, actor="dev1")
        self.assertEqual(ra["migration"], self.floor)
        # le lot garde sa réservation ; le suivant prend le numéro d'après
        self.assertEqual(work.reserve_migration(self.db, a["id"], repo=self.tmp)["migration"],
                         self.floor)
        rb = work.reserve_migration(self.db, b["id"], repo=self.tmp, actor="dev2")
        self.assertEqual(rb["migration"], self.floor + 1)
        notes = [e["note"] for e in work.events(self.db, a["id"])]
        self.assertIn("migration %04d réservée" % self.floor, notes)
        # visible dans work show
        proc = self.mesh("work", "show", str(b["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("migration: %04d réservée" % (self.floor + 1), proc.stdout)

    def test_numero_precis_refuse_s_il_est_pris(self):
        a, b = self._lot("a"), self._lot("b")
        work.reserve_migration(self.db, a["id"], 9000)
        with self.assertRaises(work.WorkError) as ctx:
            work.reserve_migration(self.db, b["id"], 9000)
        self.assertIn("lot #%d" % a["id"], str(ctx.exception))
        # le prochain libre passe au-delà des numéros réservés
        self.assertEqual(work.reserve_migration(self.db, b["id"], repo=self.tmp)["migration"],
                         9001)

    def test_liberer_et_lot_ferme(self):
        a = self._lot()
        work.reserve_migration(self.db, a["id"], 9100)
        self.assertIsNone(work.reserve_migration(self.db, a["id"], release=True)["migration"])
        self.assertIsNone(work.reserve_migration(self.db, a["id"], release=True)["migration"])
        work.close(self.db, a["id"], abandoned=True)
        with self.assertRaises(work.WorkError):
            work.reserve_migration(self.db, a["id"], repo=self.tmp)

    def test_cli(self):
        a = self._lot()
        proc = self.mesh("work", "migration", str(a["id"]), "--number", "9200")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("migration 9200 réservée", proc.stdout)
        proc = self.mesh("work", "migration", str(a["id"]), "--release")
        self.assertIn("aucune migration réservée", proc.stdout)


if __name__ == "__main__":
    unittest.main()
