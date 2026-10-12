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
import threading
import time
import unittest

from ameesh import db as db_mod, migrations, work
from ameesh.db import DbError

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


def _commit(repo, directory, *names):
    os.makedirs(os.path.join(repo, directory), exist_ok=True)
    for name in names:
        with open(os.path.join(repo, directory, name), "w") as fh:
            fh.write("--")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", " ".join(names) or "vide", "--allow-empty")


def _depot(path, slug, directory, base=(), branches=None):
    """Un clone de `slug` (origin GitHub, sans réseau) : `base` sur la branche
    principale, puis chaque branche `{nom: fichiers}` ; reste sur la principale."""
    os.makedirs(path)
    _git(path, "init", "-q", "-b", "develop")
    _git(path, "remote", "add", "origin", "git@github.com:%s.git" % slug)
    _commit(path, directory, *base)
    for branch, names in (branches or {}).items():
        _git(path, "checkout", "-qb", branch)
        _commit(path, directory, *names)
        _git(path, "checkout", "-q", "develop")
    return path


NEXLINK = migrations.LAYOUTS["manaty/nexlink"]
AMEESH = migrations.LAYOUTS[migrations.AMEESH_REPO]


class NumerosDuDepotTest(unittest.TestCase):
    """#443 : les numéros se lisent dans le dépôt du lot, avec son dossier."""

    def test_nexlink_trois_chiffres_toutes_les_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _depot(os.path.join(tmp, "nexlink"), "manaty/nexlink", NEXLINK.directory,
                          ["375_a.sql", "376_b.sql", "376_restore.sql", "RESERVATIONS.md"],
                          {"agent/x-405": ["377_receipt.sql"]})
            # une branche distante compte aussi, sans fetch
            _git(repo, "update-ref", "refs/remotes/origin/agent/y-417",
                 "refs/heads/agent/x-405")
            found = migrations.repo_numbers(repo, NEXLINK)
            self.assertEqual(sorted(found), [375, 376, 377])
            self.assertIn("origin/agent/y-417:377_receipt.sql", found[377])
            # la branche du lot ne compte pas (locale ni distante)
            mine = migrations.repo_numbers(repo, NEXLINK, skip_branch="agent/x-405")
            self.assertEqual(sorted(mine), [375, 376, 377])
            _git(repo, "update-ref", "-d", "refs/remotes/origin/agent/y-417")
            mine = migrations.repo_numbers(repo, NEXLINK, skip_branch="agent/x-405")
            self.assertEqual(sorted(mine), [375, 376])
            self.assertEqual(migrations.origin_repo(repo), "manaty/nexlink")

    def test_le_paquet_ameesh_ne_compte_que_pour_ameesh(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = os.path.join(tmp, "paquet")
            os.makedirs(package)
            open(os.path.join(package, "0903_c.sql"), "w").close()
            ameesh = _depot(os.path.join(tmp, "ameesh"), "manaty/ameesh", AMEESH.directory,
                            ["0005_e.sql"])
            nexlink = _depot(os.path.join(tmp, "nexlink"), "manaty/nexlink",
                             NEXLINK.directory, ["012_x.sql"])
            self.assertEqual(sorted(migrations.repo_numbers(ameesh, AMEESH, package=package)),
                             [5, 903])
            self.assertEqual(sorted(migrations.repo_numbers(nexlink, NEXLINK, package=package)),
                             [12])

    def test_disposition(self):
        self.assertEqual(NEXLINK.file(378), "supabase/migrations/378_<name>.sql")
        self.assertEqual(AMEESH.file(378), "src/ameesh/migrations/0378_<name>.sql")
        self.assertIsNone(migrations.layout_for("manaty/inconnu"))


class ReservationTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.nexlink = _depot(os.path.join(self.tmp, "nexlink"), "manaty/nexlink",
                              NEXLINK.directory, ["375_a.sql", "376_b.sql"],
                              {"agent/autre": ["377_c.sql"]})
        self.ameesh = _depot(os.path.join(self.tmp, "ameesh"), "manaty/ameesh",
                             AMEESH.directory, ["0005_e.sql"])
        self.floor = max(migrations.repo_numbers(self.ameesh, AMEESH)) + 1

    def _lot(self, title="un lot", repo="manaty/ameesh", n=1):
        return work.add(self.db, title=title, issue_ref="%s#%d" % (repo, n), actor="mesh-design")

    def _nx(self, lot, number=None, **kw):
        return work.reserve_migration(self.db, lot["id"], number, repo=self.nexlink, **kw)

    def _am(self, lot, number=None, **kw):
        return work.reserve_migration(self.db, lot["id"], number, repo=self.ameesh, **kw)

    def test_prochain_numero_libre_du_depot_puis_idempotent(self):
        a, b = self._lot("a"), self._lot("b")
        ra = self._am(a, actor="dev1")
        self.assertEqual((ra["migration"], ra["migration_repo"]), (self.floor, "manaty/ameesh"))
        self.assertEqual(ra["migration_file"],
                         "src/ameesh/migrations/%04d_<name>.sql" % self.floor)
        # le lot garde sa réservation ; le suivant prend le numéro d'après
        self.assertEqual(self._am(a)["migration"], self.floor)
        self.assertEqual(self._am(b, actor="dev2")["migration"], self.floor + 1)
        notes = [e["note"] for e in work.events(self.db, a["id"])]
        self.assertIn("migration %04d reserved (manaty/ameesh)" % self.floor, notes)
        proc = self.mesh("work", "show", str(b["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("migration: %04d reserved in manaty/ameesh" % (self.floor + 1),
                      proc.stdout)

    def test_lot_nexlink_dossier_et_numerotation_de_nexlink(self):
        lot = self._lot("nx", "manaty/nexlink", 169)
        # 377 n'existe que sur une autre branche : il compte quand même
        got = self._nx(lot)
        self.assertEqual((got["migration"], got["migration_repo"]), (378, "manaty/nexlink"))
        self.assertEqual(got["migration_file"], "supabase/migrations/378_<name>.sql")
        proc = self.mesh("work", "migration", str(self._lot("nx2", "manaty/nexlink", 170)["id"]),
                         "--repo", self.nexlink)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("migration 379 reserved in manaty/nexlink — file "
                      "supabase/migrations/379_<name>.sql", proc.stdout)
        self.assertNotIn("src/ameesh", proc.stdout)

    def test_suites_independantes_par_depot(self):
        """Le cas 0377 : même numéro, deux dépôts, deux réservations légitimes ;
        dans un même dépôt, la seconde est refusée."""
        am, nx = self._lot("am"), self._lot("nx", "manaty/nexlink")
        self.assertEqual(self._am(am, 9000)["migration"], 9000)
        self.assertEqual(self._nx(nx, 9000)["migration"], 9000)
        other = self._lot("nx bis", "manaty/nexlink", 2)
        with self.assertRaises(work.WorkError) as ctx:
            self._nx(other, 9000)
        self.assertIn("lot #%d" % nx["id"], str(ctx.exception))
        self.assertIn("manaty/nexlink", str(ctx.exception))
        # le prochain libre de nexlink passe au-delà de ses réservations, pas de celles d'ameesh
        self.assertEqual(self._nx(other)["migration"], 9001)
        self.assertEqual(self._am(self._lot("am2"))["migration"], 9001)

    def test_numero_present_dans_le_depot_cible_refuse(self):
        lot = self._lot("nx", "manaty/nexlink")
        for number in (376, 377):     # la branche principale, puis une autre branche
            with self.assertRaises(work.WorkError) as ctx:
                self._nx(lot, number)
            self.assertIn("already in manaty/nexlink", str(ctx.exception))
        # le même numéro est libre dans ameesh
        self.assertEqual(self._am(self._lot("am"), 376)["migration"], 376)

    def test_clone_d_un_autre_depot_ou_lot_sans_depot_refuse(self):
        nx = self._lot("nx", "manaty/nexlink")
        with self.assertRaises(work.WorkError) as ctx:
            work.reserve_migration(self.db, nx["id"], repo=self.ameesh)
        self.assertIn("clone of manaty/ameesh, not of manaty/nexlink", str(ctx.exception))
        bare = work.add(self.db, title="sans issue", actor="mesh-design")
        with self.assertRaises(work.WorkError) as ctx:
            work.reserve_migration(self.db, bare["id"], repo=self.ameesh)
        self.assertIn("no repository", str(ctx.exception))
        unknown = self._lot("x", "manaty/inconnu")
        with self.assertRaises(work.WorkError) as ctx:
            work.reserve_migration(self.db, unknown["id"], repo=self.ameesh)
        self.assertIn("migrations directory unknown", str(ctx.exception))
        self.assertIsNone(work.get(self.db, nx["id"])["migration"])

    def test_la_base_garantit_l_unicite(self):
        """Hors de l'outil, la contrainte refuse un doublon (dépôt, numéro)."""
        a, b = self._lot("a"), self._lot("b")
        self.db.execute("UPDATE work_items SET migration = 42, migration_repo = 'manaty/ameesh'"
                        " WHERE id = %d" % a["id"])
        with self.assertRaises(DbError) as ctx:
            self.db.execute("UPDATE work_items SET migration = 42,"
                            " migration_repo = 'manaty/ameesh' WHERE id = %d" % b["id"])
        self.assertIn("work_items_migration_repo_idx", str(ctx.exception))

    def test_client_d_avant_443(self):
        """Un poste pas encore à jour pose le seul numéro : le déclencheur
        prend le dépôt de l'issue, et la libération efface les deux."""
        nx, am = self._lot("nx", "manaty/nexlink"), self._lot("am")
        for lot in (nx, am):
            self.db.execute("UPDATE work_items SET migration = 500 WHERE id = %d" % lot["id"])
        self.assertEqual(work.get(self.db, nx["id"])["migration_repo"], "manaty/nexlink")
        self.assertEqual(work.get(self.db, am["id"])["migration_repo"], "manaty/ameesh")
        with self.assertRaises(DbError):
            self.db.execute("UPDATE work_items SET migration = 500 WHERE id = %d"
                            % self._lot("am2", n=2)["id"])
        self.db.execute("UPDATE work_items SET migration = NULL WHERE id = %d" % nx["id"])
        self.assertIsNone(work.get(self.db, nx["id"])["migration_repo"])

    def test_liberer_et_lot_ferme(self):
        a = self._lot()
        self._am(a, 9100)
        released = work.reserve_migration(self.db, a["id"], release=True)
        self.assertEqual((released["migration"], released["migration_repo"]), (None, None))
        self.assertIsNone(work.reserve_migration(self.db, a["id"], release=True)["migration"])
        work.close(self.db, a["id"], abandoned=True)
        with self.assertRaises(work.WorkError):
            self._am(a)

    def test_cli(self):
        a = self._lot()
        proc = self.mesh("work", "migration", str(a["id"]), "--number", "9200",
                         "--repo", self.ameesh)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("migration 9200 reserved in manaty/ameesh", proc.stdout)
        proc = self.mesh("work", "migration", str(a["id"]), "--release")
        self.assertIn("no migration reserved", proc.stdout)


class ReservationsSimultaneesTest(PgTestCase):
    """#443 : deux réservations au même instant, sur les deux pilotes."""

    def setUp(self):
        super().setUp()
        self.nexlink = _depot(os.path.join(self.tmp, "nexlink"), "manaty/nexlink",
                              NEXLINK.directory, ["376_b.sql"])

    def _lots(self, n):
        return [work.add(self.db, title="lot %d" % i, issue_ref="manaty/nexlink#%d" % i,
                         actor="mesh-design")["id"] for i in range(n)]

    def _blocked(self, timeout=60.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.db.query(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                " AND datname = current_database()"
                " AND query LIKE '%UPDATE work_items SET migration = greatest(%'")
            if rows[0]["n"]:
                return True
            time.sleep(0.05)
        return False

    def test_la_perdante_recalcule(self):
        """A tient 377 sans avoir validé ; B lit le même maximum, tente 377,
        attend sur l'index, échoue au commit de A et prend 378."""
        for holder, other in (("psql", "psycopg"), ("psycopg", "psql"),
                              ("psycopg", "psycopg"), ("psql", "psql")):
            with self.subTest(holder=holder, other=other):
                self.db.execute("UPDATE work_items SET migration = NULL, migration_repo = NULL")
                a, b = self._lots(2)
                held, done, out = threading.Event(), threading.Event(), {}

                def hold():
                    conn = db_mod.connect(self.cfg, driver=holder)
                    try:
                        with conn.transaction() as tx:
                            tx.query("UPDATE work_items SET migration = 377,"
                                     " migration_repo = 'manaty/nexlink' WHERE id = %s"
                                     " RETURNING id", (a,))
                            held.set()
                            done.wait(60)   # jusqu'à ce que B attende sur l'index
                    finally:
                        conn.close()

                def reserve():
                    conn = db_mod.connect(self.cfg, driver=other)
                    try:
                        out["b"] = work.reserve_migration(conn, b, repo=self.nexlink)
                    except Exception as exc:  # noqa: BLE001 - rapporté par l'assertion
                        out["b"] = exc
                    finally:
                        conn.close()

                ta = threading.Thread(target=hold)
                ta.start()
                self.assertTrue(held.wait(30))
                tb = threading.Thread(target=reserve)
                tb.start()
                # B est réellement bloquée sur l'index avant que A ne valide
                self.assertTrue(self._blocked(), "B never waited on A's reservation")
                done.set()
                ta.join(30)
                tb.join(60)
                self.assertIsInstance(out.get("b"), dict, out.get("b"))
                self.assertEqual(out["b"]["migration"], 378)
                self.assertEqual(work.get(self.db, a)["migration"], 377)

    def test_rafale_sur_les_deux_pilotes(self):
        ids = self._lots(8)
        start, out = threading.Barrier(len(ids)), {}

        def reserve(item_id, driver):
            conn = db_mod.connect(self.cfg, driver=driver)
            try:
                start.wait(30)
                out[item_id] = work.reserve_migration(conn, item_id,
                                                      repo=self.nexlink)["migration"]
            except Exception as exc:  # noqa: BLE001 - rapporté par l'assertion
                out[item_id] = exc
            finally:
                conn.close()

        threads = [threading.Thread(target=reserve, args=(i, ("psql", "psycopg")[k % 2]))
                   for k, i in enumerate(ids)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        self.assertEqual(sorted(out.values()), list(range(377, 377 + len(ids))), out)


if __name__ == "__main__":
    unittest.main()
