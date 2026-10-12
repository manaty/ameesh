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
import types
import unittest
from unittest import mock

from ameesh import db as db_mod, migrations, work
from ameesh.db import DbError
from ameesh.storage.postgres import work as pg_work

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

    def test_tout_nom_numerote_compte(self):
        """#443 r1 P2 : `377_receipt+fix.sql` occupe 377, même hors du format."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _depot(os.path.join(tmp, "nexlink"), "manaty/nexlink", NEXLINK.directory,
                          ["376_a.sql", "377_receipt+fix.sql", "378_x.SQL", "README.md",
                           "v379_pas_un_numero.sql"])
            self.assertEqual(sorted(migrations.repo_numbers(repo, NEXLINK)), [376, 377, 378])

    def test_origin_hote_exact(self):
        """#443 r1 P2 : l'hôte doit être github.com, pas une sous-chaîne."""
        for url in ("https://github.com/manaty/nexlink.git", "https://github.com/Manaty/Nexlink",
                    "https://user@github.com/manaty/nexlink.git/", "git@github.com:manaty/nexlink.git",
                    "ssh://git@github.com/manaty/nexlink", "github.com:manaty/nexlink"):
            with self.subTest(url=url):
                self.assertEqual(migrations.github_repo(url), "manaty/nexlink")
        for url in ("https://notgithub.com/manaty/nexlink.git",
                    "https://github.com.evil.example/manaty/nexlink.git",
                    "git@gitlab.com:manaty/nexlink.git", "https://example.com/github.com/manaty/nexlink",
                    "https://github.com/manaty/nexlink/tree/x", "file:///srv/github.com/manaty/nexlink",
                    "/srv/github.com:manaty/nexlink", ""):
            with self.subTest(url=url):
                self.assertIsNone(migrations.github_repo(url))

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
        # #443 r1 P2 : un hôte qui contient « github.com » n'est pas github.com
        _git(self.nexlink, "remote", "set-url", "origin", "https://notgithub.com/manaty/nexlink.git")
        with self.assertRaises(work.WorkError) as ctx:
            self._nx(nx)
        self.assertIn("no GitHub origin", str(ctx.exception))
        bare = work.add(self.db, title="sans issue", actor="mesh-design")
        with self.assertRaises(work.WorkError) as ctx:
            work.reserve_migration(self.db, bare["id"], repo=self.ameesh)
        self.assertIn("no repository", str(ctx.exception))
        unknown = self._lot("x", "manaty/inconnu")
        with self.assertRaises(work.WorkError) as ctx:
            work.reserve_migration(self.db, unknown["id"], repo=self.ameesh)
        self.assertIn("migrations directory unknown", str(ctx.exception))
        self.assertIsNone(work.get(self.db, nx["id"])["migration"])

    def test_cle_de_depot_en_minuscules(self):
        """#443 r1 P2 : `Manaty/Nexlink` et `manaty/nexlink` sont une seule suite,
        avec la disposition de nexlink, même déclarée en majuscules à l'hôte."""
        cfg = types.SimpleNamespace(github=None, radar={"projects": {"nx": {
            "repo": self.nexlink, "github": "Manaty/Nexlink",
            "migrations": NEXLINK.directory}}})
        upper = self._lot("nx", "Manaty/Nexlink", 3)
        got = work.reserve_migration(self.db, upper["id"], cfg=cfg)
        self.assertEqual((got["migration"], got["migration_repo"]), (378, "manaty/nexlink"))
        self.assertEqual(got["migration_file"], "supabase/migrations/378_<name>.sql")
        lower = self._lot("nx2", "manaty/nexlink", 4)
        with self.assertRaises(work.WorkError) as ctx:
            work.reserve_migration(self.db, lower["id"], 378, cfg=cfg)
        self.assertIn("lot #%d" % upper["id"], str(ctx.exception))
        self.assertEqual(work.reserve_migration(self.db, lower["id"], cfg=cfg)["migration"], 379)
        # en base aussi : un dépôt écrit en majuscules tombe dans la même suite
        with self.assertRaises(DbError):
            self.db.execute("UPDATE work_items SET migration = 379,"
                            " migration_repo = 'MANATY/nexlink' WHERE id = %d"
                            % self._lot("nx3", n=5)["id"])

    def test_pas_d_espace_de_noms_invente(self):
        """#443 r1 P2 : sans dépôt d'issue, un client d'avant #443 est refusé
        (pas de `unknown/<app>`) ; une issue entourée d'espaces garde son dépôt."""
        held = work.add(self.db, title="nx", app="nexlink", issue_ref=" manaty/nexlink#9 ",
                        actor="mesh-design")
        self.db.execute("UPDATE work_items SET migration = 389 WHERE id = %d" % held["id"])
        self.assertEqual(work.get(self.db, held["id"])["migration_repo"], "manaty/nexlink")
        with self.assertRaises(work.WorkError) as ctx:
            self._nx(self._lot("nx2", "manaty/nexlink", 10), 389)
        self.assertIn("lot #%d" % held["id"], str(ctx.exception))
        bare = work.add(self.db, title="sans issue", app="nexlink", actor="mesh-design")
        with self.assertRaises(DbError) as ctx:
            self.db.execute("UPDATE work_items SET migration = 390 WHERE id = %d" % bare["id"])
        self.assertIn("without a repository", str(ctx.exception))
        rows = self.db.query("SELECT work_items_issue_repo(%s) AS a, work_items_issue_repo(%s) AS b,"
                             " work_items_issue_repo(%s) AS c",
                             (" Manaty/Nexlink#3\n", "", "manaty/nexlink"))
        self.assertEqual((rows[0]["a"], rows[0]["b"], rows[0]["c"]),
                         ("manaty/nexlink", None, None))

    def _rejoue_0390(self, *rows):
        """Les réservations `rows` (migration, dépôt, issue) posées avant 0390,
        puis 0390 rejouée : son déclencheur, sa contrainte et son index sont
        retirés d'abord, la colonne reste (un dépôt déjà donné). Le schéma de la
        classe est rendu tel quel ensuite, réservations libérées."""
        path = os.path.join(migrations.MIGRATIONS_DIR, "0390_work_item_migration_repo.sql")
        with open(path, encoding="utf-8") as fh:
            sql = fh.read()
        undo = ("DROP TRIGGER IF EXISTS work_items_migration_repo_fill ON work_items;"
                " ALTER TABLE work_items DROP CONSTRAINT IF EXISTS work_items_migration_repo_check;"
                " DROP INDEX IF EXISTS work_items_migration_repo_idx;")
        ids = []

        def restore():
            self.db.script(undo)
            for i in ids:
                self.db.execute("UPDATE work_items SET migration = NULL, migration_repo = NULL"
                                " WHERE id = %s", (i,))
            self.db.script(sql)

        self.addCleanup(restore)
        self.db.script(undo)
        for number, repo, issue in rows:
            lot = work.add(self.db, title="avant 0390", app="nexlink", issue_ref=issue,
                           actor="mesh-design")
            ids.append(lot["id"])
            self.db.execute("UPDATE work_items SET migration = %s, migration_repo = %s"
                            " WHERE id = %s", (number, repo, lot["id"]))
        self.db.script(sql)
        return [work.get(self.db, i)["migration_repo"] for i in ids]

    def test_0390_garde_un_depot_deja_donne(self):
        """#443 r2 P2 : un dépôt déjà donné sans issue ne fait pas échouer 0390,
        un dépôt en majuscules est ramené en minuscules (la contrainte l'accepte),
        un dépôt vide prend celui de l'issue."""
        self.assertEqual(
            self._rejoue_0390((389, "manaty/nexlink", None),
                              (390, "Manaty/Nexlink", "manaty/nexlink#7"),
                              (391, " ", "Manaty/Nexlink#8"),
                              (None, "manaty/nexlink", None)),
            ["manaty/nexlink", "manaty/nexlink", "manaty/nexlink", None])
        with self.assertRaises(DbError):
            self.db.execute("UPDATE work_items SET migration = 390,"
                            " migration_repo = 'MANATY/NEXLINK' WHERE id = %d"
                            % self._lot("nx", "manaty/nexlink", 9)["id"])

    def test_0390_refuse_une_reservation_sans_depot(self):
        with self.assertRaises(DbError) as ctx:
            self._rejoue_0390((389, None, None))
        self.assertIn("without a repository", str(ctx.exception))

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


def _drivers():
    """Les pilotes présents : la passe psql de la CI tourne sans psycopg."""
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return ("psql",)
    return ("psql", "psycopg")


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
        for holder, other in [(h, o) for h in _drivers() for o in _drivers()]:
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

    def test_rafale_de_20_sur_les_deux_pilotes(self):
        """#478 : 20 réservations simultanées, 20 numéros distincts — la
        rafale de qa-443 épuisait les 8 essais de _RESERVE_ATTEMPTS."""
        ids = self._lots(20)
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

        drivers = _drivers()
        threads = [threading.Thread(target=reserve, args=(i, drivers[k % len(drivers)]))
                   for k, i in enumerate(ids)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(180)
        self.assertEqual(sorted(out.values()), list(range(377, 377 + len(ids))), out)


#: l'index unique (dépôt, numéro) de L122 : lui seul fait une contention
_INDEX = "work_items_migration_repo_idx"


def _violation(*, constraint=_INDEX, state="23505") -> DbError:
    """La violation d'unicité de `constraint` telle qu'un pilote la rend : le
    code SQLSTATE 23505 et le nom exact de la contrainte — ceux de psycopg
    (`diag.constraint_name`) comme ceux de psql (`VERBOSITY=verbose`). Le
    message n'entre pas dans la classification (codex #478 r2)."""
    return DbError('psql: ERROR:  duplicate key value violates unique constraint "%s"'
                   % constraint, sqlstate=state, constraint=constraint)


def _psql_verbose(constraint=_INDEX, *, severity="ERROR", state="23505",
                  message=None) -> str:
    """Le rapport d'erreur de psql en `VERBOSITY=verbose` : le code SQLSTATE
    sur la ligne d'erreur, le nom exact dans son champ. `severity` et
    `message` suivent `lc_messages` du serveur (« ERREUR », message
    français) ; le code et le champ CONSTRAINT NAME, non."""
    if message is None:
        message = 'duplicate key value violates unique constraint "%s"' % constraint
    return ("%s:  %s: %s\n"
            "DETAIL:  Key (migration_repo, migration)=(manaty/nexlink, 377)"
            " already exists.\n"
            "SCHEMA NAME:  public\n"
            "TABLE NAME:  work_items\n"
            "CONSTRAINT NAME:  %s\n"
            "LOCATION:  _bt_check_unique, nbtinsert.c:677\n"
            % (severity, state, message, constraint))


def _failed(stderr: str) -> subprocess.CompletedProcess:
    """Un psql en échec (code 3, ON_ERROR_STOP) dont le `_fail` lit stderr."""
    return subprocess.CompletedProcess([], 3, "", stderr)


class _FakeTime:
    """Horloge fausse d'un test de budget : `monotonic()` ne bouge que par
    `sleep()`, qui est enregistré au lieu d'attendre (codex #478 r1)."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class RetryReservationTest(PgTestCase):
    """#478 (codex r1) : le budget de réessais de la réservation, sans
    ordonnancement réel ni verrou : les violations d'unicité sont injectées
    et l'horloge est fausse."""

    def setUp(self):
        super().setUp()
        self.nexlink = _depot(os.path.join(self.tmp, "nexlink"), "manaty/nexlink",
                              NEXLINK.directory, ["376_b.sql"])

    def _lot(self):
        return work.add(self.db, title="lot", issue_ref="manaty/nexlink#1",
                        actor="mesh-design")["id"]

    def _reserve(self, exc, *, budget):
        """`reserve_migration` sur un lot neuf, chaque essai levant `exc` :
        rend (résultat ou exception, horloge, essais, tirages d'attente)."""
        lot = self._lot()
        clock = _FakeTime()
        attempts, draws = [], []

        def transaction():
            attempts.append(clock.now)
            raise exc

        def uniform(low, high):
            draws.append((low, high))
            return high

        with mock.patch.object(pg_work, "time", clock), \
                mock.patch.object(pg_work, "_RESERVE_BUDGET_S", budget), \
                mock.patch.object(pg_work, "random",
                                  types.SimpleNamespace(uniform=uniform)), \
                mock.patch.object(self.db, "transaction", side_effect=transaction):
            try:
                out = work.reserve_migration(self.db, lot, repo=self.nexlink)
            except Exception as got:  # noqa: BLE001 - rapporté par l'assertion
                out = got
        return out, clock, attempts, draws

    def test_l_attente_est_bornee_par_le_budget_restant(self):
        # codex #478 r1 : avec 1 ms de budget, l'ancienne attente de 5 ms
        # entamait une transaction après l'échéance ; l'attente est bornée au
        # restant, et rien n'est retenté une fois l'échéance passée
        got, clock, attempts, _ = self._reserve(_violation(), budget=0.001)
        self.assertIsInstance(got, work.WorkError, got)
        self.assertEqual(str(got), "reservation contention, retry")
        self.assertEqual(attempts, [0.0], "une seule transaction, avant l'échéance")
        self.assertEqual(clock.slept, [0.001], "l'attente tient dans le budget")

    def test_budget_nul_ne_retente_pas(self):
        # codex #478 r1 : budget épuisé, la première violation rend la
        # contention sans deuxième transaction ni attente
        got, clock, attempts, _ = self._reserve(_violation(), budget=0.0)
        self.assertIsInstance(got, work.WorkError, got)
        self.assertEqual(attempts, [0.0])
        self.assertEqual(clock.slept, [])

    def test_une_erreur_qui_nomme_l_index_sans_unicite_remonte(self):
        # codex #478 r1 : « permission denied for relation
        # work_items_migration_repo_idx » n'est pas une violation d'unicité :
        # elle remonte telle quelle, jamais en WorkError de contention
        denied = DbError("psql: ERROR:  permission denied for relation %s" % _INDEX)
        got, _, _, _ = self._reserve(denied, budget=0.05)
        self.assertIs(got, denied, got)

    def test_la_violation_se_reconnait_au_code_sqlstate(self):
        # codex #478 r1/r2 : psycopg donne le code 23505 et le nom exact par
        # ses diagnostics ; c'est la contention de l'index
        got, _, _, _ = self._reserve(_violation(), budget=0.0)
        self.assertIsInstance(got, work.WorkError, got)
        self.assertEqual(str(got), "reservation contention, retry")

    def test_une_autre_contrainte_au_nom_proche_ne_retente_pas(self):
        # codex #478 r2 : `work_items_migration_repo_idx_other` contient le
        # nom de l'index sans être lui ; sa violation remonte telle quelle et
        # n'entame aucune deuxième transaction (l'ancien test par sous-chaîne
        # la retentait)
        other = _violation(constraint=_INDEX + "_other")
        got, _, attempts, _ = self._reserve(other, budget=10.0)
        self.assertIs(got, other, got)
        self.assertEqual(attempts, [0.0], "aucune deuxième transaction")

    def test_psql_verbose_reconnait_la_violation_meme_en_francais(self):
        # codex #478 r2 : un serveur en `lc_messages=fr` écrit « ERREUR » et
        # un message français ; le code SQLSTATE et le champ CONSTRAINT NAME
        # ne sont pas traduits, et c'est tout ce que la classification lit
        exc = db_mod.PsqlDriver._fail(None, _failed(_psql_verbose(
            severity="ERREUR",
            message="la valeur d'une clé dupliquée rompt la contrainte unique"
                    " « %s »" % _INDEX)))
        self.assertEqual(exc.sqlstate, "23505")
        self.assertEqual(exc.constraint, _INDEX)
        got, _, _, _ = self._reserve(exc, budget=0.0)
        self.assertIsInstance(got, work.WorkError, got)
        self.assertEqual(str(got), "reservation contention, retry")

    def test_psql_verbose_autre_contrainte_ne_retente_pas(self):
        # codex #478 r2 : le champ CONSTRAINT NAME tranche exactement, même
        # quand le message (traduit) nomme l'index par sous-chaîne
        exc = db_mod.PsqlDriver._fail(None, _failed(_psql_verbose(
            constraint=_INDEX + "_other",
            message='duplicate key value violates unique constraint "%s"' % _INDEX)))
        self.assertEqual(exc.constraint, _INDEX + "_other")
        got, _, attempts, _ = self._reserve(exc, budget=10.0)
        self.assertIs(got, exc, got)
        self.assertEqual(attempts, [0.0], "aucune deuxième transaction")

    def test_le_code_et_la_contrainte_du_pilote_survivent_a_la_dberror(self):
        # codex #478 r1/r2 : le code SQLSTATE et le nom de contrainte du
        # pilote (psycopg) survivent à la traduction en DbError — seuls eux
        # distinguent 23505 d'une autre erreur qui nomme le même index
        class Diag:
            constraint_name = _INDEX

        class DriverError(Exception):
            sqlstate = "23505"
            diag = Diag()

        mapped = db_mod._map_error(DriverError("duplicate key value"))
        self.assertEqual(mapped.sqlstate, "23505")
        self.assertEqual(mapped.constraint, _INDEX)
        self.assertIsNone(db_mod._map_error(Exception("boom")).sqlstate)
        self.assertIsNone(db_mod._map_error(Exception("boom")).constraint)

    def test_l_attente_plein_jitter_part_de_zero(self):
        # codex #478 r1 (P3) : plein jitter — uniform(0, min(cap, base*2^n)) —
        # et non uniform(base, wait) : le premier tirage part de 0, les
        # suivants doublent jusqu'au plafond
        got, _, _, draws = self._reserve(_violation(), budget=10.0)
        self.assertIsInstance(got, work.WorkError, got)
        self.assertEqual(draws[:3], [(0.0, 0.005), (0.0, 0.01), (0.0, 0.02)])

    def test_budget_epuise_rend_workerror_lisible(self):
        """#478 : le budget de réessais épuisé rend un WorkError lisible,
        jamais la DbError brute de l'index. Déterministe (codex #478 r1) :
        violation injectée, horloge fausse — plus de verrou réel ni
        d'attente de 60 s."""
        got, clock, attempts, _ = self._reserve(_violation(), budget=0.0)
        self.assertIsInstance(got, work.WorkError, got)
        self.assertEqual(str(got), "reservation contention, retry")
        self.assertNotIsInstance(got, DbError)
        self.assertEqual(attempts, [0.0])
        self.assertEqual(clock.slept, [])


if __name__ == "__main__":
    unittest.main()
