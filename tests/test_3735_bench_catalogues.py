# SPDX-License-Identifier: AGPL-3.0-only
"""#3735 : banc ameesh-pg — orphelins t_*, gonflement, signal doctor.

Témoins du lot : un schéma `t_*` assez vieux et **prouvé** orphelin (marqueur
d'un propriétaire mort) est purgé ; un schéma tenu par une session active, un
schéma récent, un schéma sans marqueur (client au repos, ou suite psql vivante
entre deux échanges) et un schéma dont la preuve est illisible sont gardés ;
les schémas sans marqueur ne sont purgés que par une maintenance qui tient le
verrou **exclusif** du banc, jamais par un démarrage de suite. Un dernier
autovacuum ancien est signalé. La base de la suite (`AMEESH_TEST_DSN`) est le
banc partagé de l'hôte : avec un âge minimal abaissé, les purges des témoins
sont bornées à leurs propres schémas (`only`), jamais ceux des suites
voisines.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from unittest import mock

from ameesh import bench as bench_mod
from ameesh import db as db_mod

from .support import PgTestCase


def _name(tag: str) -> str:
    return "t_%s_%s" % (tag, uuid.uuid4().hex[:8])


def _dead_pid() -> int:
    """PID d'un processus révolu : sa preuve de propriété ne protège plus."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class BenchCataloguesTest(PgTestCase):
    """Le banc réel : âge, garde de session, purges, lecture et signaux."""

    def _create(self, name: str) -> None:
        self.db.execute('CREATE SCHEMA "%s"' % name)
        self.db.execute('CREATE TABLE "%s".x (id integer)' % name)
        self.db.execute('INSERT INTO "%s".x VALUES (1)' % name)
        self.addCleanup(self._drop, name)

    def _drop(self, name: str) -> None:
        self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % name)

    def _exists(self, name: str) -> bool:
        row = self.db.query(
            "SELECT to_regnamespace(%s) IS NOT NULL AS present", (name,))[0]
        return bool(row["present"])

    def test_old_orphan_purged_active_and_young_kept(self) -> None:
        old = _name("orphan")
        active = _name("active")
        young = _name("young")
        self._create(old)
        self._create(active)
        # #3735 B/P1 : un schéma sans marqueur garde son doute ; pour le
        # témoin de purge, `old` porte la preuve d'un propriétaire mort.
        bench_mod.claim_owner(self.db, old, pid=_dead_pid(), start=0)
        mine = {old, active, young}
        # l'âge lu est celui de la première relation : une seconde de marge
        # suffit pour distinguer « vieux » de « créé après la mesure ».
        time.sleep(1.1)
        other = db_mod.connect(self.cfg)
        try:
            with other.transaction() as tx:
                tx.query('SELECT * FROM "%s".x' % active)
                found = {orphan.schema for orphan in bench_mod.orphan_schemas(
                    self.db, min_age_s=0.5, only=mine)}
                self.assertIn(old, found)
                self.assertNotIn(active, found)   # session qui tient un verrou
                dry = bench_mod.purge_orphans(self.db, min_age_s=0.5, dry_run=True,
                                             only=mine)
                self.assertIn(old, [schema for schema, _ in dry.kept])
                self.assertTrue(self._exists(old))
                result = bench_mod.purge_orphans(self.db, min_age_s=0.5, only=mine)
        finally:
            other.close()
        self.assertIn(old, result.dropped)
        self.assertNotIn(active, result.dropped)
        self.assertFalse(self._exists(old))
        self.assertTrue(self._exists(active))
        # le schéma créé après la mesure est trop jeune : jamais purgé
        self._create(young)
        fresh = bench_mod.purge_orphans(self.db, min_age_s=3600.0, only=mine)
        self.assertNotIn(young, fresh.dropped)
        self.assertTrue(self._exists(young))

    def test_purge_scoped_to_only_spares_neighbours(self) -> None:
        mine = _name("mine")
        neighbour = _name("neighbour")   # la classe d'une autre suite, inactive
        self._create(mine)
        self._create(neighbour)
        # la purge exige une mort prouvée (#3735 B/P1) : `mine` porte la
        # preuve, `neighbour` reste sans marqueur et hors de `only`.
        bench_mod.claim_owner(self.db, mine, pid=_dead_pid(), start=0)
        time.sleep(1.1)
        result = bench_mod.purge_orphans(self.db, min_age_s=0.5, only={mine})
        self.assertIn(mine, result.dropped)
        self.assertNotIn(neighbour, result.dropped)
        self.assertTrue(self._exists(neighbour))

    def test_owner_marker_protects_idle_old_schema(self) -> None:
        """Le cas du verdict codex : propriétaire vivant, aucune session active.

        Une suite au repos entre deux transactions n'a ni verrou ni requête
        qui nomme son schéma ; seule la preuve de propriété (PID/start du
        processus) le garde. Un propriétaire mort ne protège plus.
        """
        owned = _name("owned")
        gone = _name("gone")
        self._create(owned)
        self._create(gone)
        bench_mod.claim_owner(self.db, owned)  # le processus de la suite vit
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        bench_mod.claim_owner(self.db, gone, pid=child.pid, start=0)
        # la dernière requête de la connexion ne doit nommer aucun des deux
        # schémas (sinon la garde _in_use suffirait au lieu de la preuve).
        self.db.query("SELECT 1")
        time.sleep(1.1)
        result = bench_mod.purge_orphans(self.db, min_age_s=0.5, only={owned, gone})
        self.assertNotIn(owned, result.dropped)
        self.assertTrue(self._exists(owned))
        self.assertIn(gone, result.dropped)
        self.assertFalse(self._exists(gone))

    def test_unmarked_old_schema_kept_by_second_suite_start(self) -> None:
        """#3735 B/P1 : un non marqué survit au démarrage d'une seconde suite.

        Une suite ancienne (lancée avant ce patch, ou dont `claim_owner` a
        échoué) laisse un schéma sans commentaire ; elle peut n'avoir aucun
        client backend entre deux échanges psql, ou une session au repos qui
        ne nomme pas son schéma. Le démarrage d'une seconde suite fait une
        passe ordinaire : le doute ne supprime rien, aucun DROP.
        """
        unmarked = _name("unmarked")
        self._create(unmarked)
        other = db_mod.connect(self.cfg)
        try:
            other.query("SELECT 1")  # client vivant, session au repos
            time.sleep(1.1)
            found = {orphan.schema for orphan in bench_mod.orphan_schemas(
                self.db, min_age_s=0.5, only={unmarked})}
            self.assertNotIn(unmarked, found)
            result = bench_mod.purge_orphans(self.db, min_age_s=0.5,
                                             only={unmarked})
        finally:
            other.close()
        self.assertNotIn(unmarked, result.dropped)
        self.assertTrue(self._exists(unmarked))

    def test_marker_without_start_protects_a_live_owner(self) -> None:
        """#3735 B/P1 : starttime indisponible -> marqueur valide sans start.

        L'ancien marqueur « pid=N start= » n'était pas relu : le propriétaire
        vivant passait pour mort. Le marqueur sans start garde le schéma
        (aucun DROP).
        """
        owned = _name("nostart")
        self._create(owned)
        with mock.patch.object(
                bench_mod.platform_mod, "start_time",
                side_effect=bench_mod.platform_mod.NotAvailable(
                    "process start time", "test: starttime indisponible")):
            marker = bench_mod.owner_marker()
            self.assertEqual(bench_mod.parse_owner(marker), (os.getpid(), None))
            self.assertTrue(bench_mod.owner_alive(marker))
            bench_mod.claim_owner(self.db, owned)
        time.sleep(1.1)
        result = bench_mod.purge_orphans(self.db, min_age_s=0.5, only={owned})
        self.assertNotIn(owned, result.dropped)
        self.assertTrue(self._exists(owned))

    def test_unreadable_owner_marker_keeps_the_schema(self) -> None:
        """#3735 B/P1 : preuve illisible = garder (le doute ne supprime rien).

        Le marqueur tronqué écrit par l'ancien chemin (« pid=N start= ») n'est
        pas relu ; il garde le schéma même si son PID est vivant.
        """
        legacy = _name("legacy")
        self._create(legacy)
        marker = "ameesh-bench-owner pid=%d start=" % os.getpid()
        self.db.execute('COMMENT ON SCHEMA "%s" IS %s'
                        % (legacy, "'" + marker + "'"))
        self.assertIsNone(bench_mod.parse_owner(marker))
        self.assertEqual(bench_mod.owner_state(marker), bench_mod.OWNER_UNKNOWN)
        self.assertFalse(bench_mod.owner_alive(marker))
        time.sleep(1.1)
        result = bench_mod.purge_orphans(self.db, min_age_s=0.5, only={legacy})
        self.assertNotIn(legacy, result.dropped)
        self.assertTrue(self._exists(legacy))

    def test_suite_upkeep_never_bootstraps(self) -> None:
        """#3735 B/P1 : le démarrage d'une suite ne lève jamais le doute.

        `_bench_upkeep` est une passe ordinaire : elle purge les propriétaires
        prouvés morts et garde les schémas sans marqueur. Leur purge est
        réservée à la maintenance qui tient le verrou exclusif.
        """
        from . import support
        support.BENCH_UPKEEP_DONE = False
        try:
            with mock.patch.object(
                    support.bench_mod, "purge_orphans",
                    return_value=bench_mod.Purge(0, (), ())) as purge:
                support._bench_upkeep(self.db)
        finally:
            support.BENCH_UPKEEP_DONE = True
        purge.assert_called_once()
        self.assertIs(purge.call_args.args[0], self.db)
        self.assertNotIn("bootstrap", purge.call_args.kwargs)

    def test_owner_marker_round_trip(self) -> None:
        marker = bench_mod.owner_marker()
        self.assertEqual(bench_mod.parse_owner(marker)[0], os.getpid())
        self.assertTrue(bench_mod.owner_alive(marker))
        self.assertFalse(bench_mod.owner_alive("un commentaire étranger"))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        dead = "ameesh-bench-owner pid=%d start=0" % child.pid
        self.assertFalse(bench_mod.owner_alive(dead))

    def test_pgtestcase_schema_has_live_owner(self) -> None:
        """Le socle pose bien la preuve de propriété de son propre schéma."""
        row = self.db.query(
            "SELECT obj_description(n.oid, 'pg_namespace') AS comment "
            "FROM pg_namespace n WHERE n.nspname = %s", (self.schema,))[0]
        self.assertTrue(bench_mod.owner_alive(row["comment"]), row["comment"])

    def test_read_counts_orphans_and_catalogs(self) -> None:
        reading = bench_mod.read(self.db)
        self.assertGreaterEqual(reading.orphan_schemas, 1)  # le schéma de la classe
        catalogs = dict(reading.catalogs)
        self.assertIn("pg_attribute", catalogs)
        self.assertGreater(catalogs["pg_attribute"], 0)
        self.assertIn("catalogs", reading.summary())

    def test_old_autovacuum_and_bloat_are_signalled(self) -> None:
        now = 1_800_000_000.0
        reading = bench_mod.Reading(
            database="banc",
            size_bytes=23 * 1024 ** 3,
            catalogs=(("pg_attribute", 9 * 1024 ** 3),
                      ("pg_class", 3 * 1024 ** 3)),
            orphan_schemas=341,
            last_autovacuum=now - 9 * 86400,
            server_start=now - 10 * 86400,
            now=now,
        )
        lines = reading.doctor_lines()
        self.assertTrue(any("last autovacuum 9d" in line for line in lines), lines)
        self.assertTrue(any("catalogs 12.0 GiB" in line for line in lines), lines)
        self.assertTrue(any("341 t_*" in line for line in lines), lines)
        self.assertTrue(any(line.startswith("warning") for line in lines), lines)

    def test_fresh_bench_has_no_warning(self) -> None:
        now = 1_800_000_000.0
        reading = bench_mod.Reading(
            database="banc", size_bytes=64 * 1024 ** 2,
            catalogs=(("pg_attribute", 8 * 1024 ** 2),),
            orphan_schemas=0, last_autovacuum=now - 60,
            server_start=now - 600, now=now)
        self.assertEqual(reading.warnings(), [])

    def test_schema_name_is_validated_before_any_drop(self) -> None:
        self.assertTrue(bench_mod.SCHEMA_RE.match("t_bench_0123abcd"))
        self.assertFalse(bench_mod.SCHEMA_RE.match('t_x"; DROP TABLE x; --'))
        self.assertFalse(bench_mod.SCHEMA_RE.match("public"))


#: #3735 B/P1 : scénario hors processus — une suite vivante **sans client
#: backend** (comme `PsqlDriver` entre deux échanges) tient le verrou partagé ;
#: la maintenance ne peut alors pas admettre un schéma sans marqueur. Une fois
#: la suite morte, la même maintenance, sous verrou exclusif, le purge. Le
#: schéma est propre au témoin (`only`) et supprimé dans tous les cas.
_LOCK_SCENARIO = r'''
import dataclasses
import os
import subprocess
import sys
import time

from ameesh import bench
from ameesh import config as config_mod
from ameesh import db as db_mod

dsn, name = sys.argv[1], sys.argv[2]
holder = subprocess.Popen(
    [sys.executable, "-c",
     "import time\n"
     "from ameesh import bench\n"
     "fh = bench.suite_lock()\n"
     "print('ok' if fh else 'none', flush=True)\n"
     "time.sleep(60)\n"],
    stdout=subprocess.PIPE, text=True)
try:
    print("holder=%s" % holder.stdout.readline().strip(), flush=True)
    cfg = dataclasses.replace(config_mod.Config(), dsn=dsn, schema="public",
                              backend="pg", statement_timeout_ms=120000.0)
    database = db_mod.connect(cfg)
    try:
        database.execute('CREATE SCHEMA "%s"' % name)
        database.execute('CREATE TABLE "%s".x (id integer)' % name)
        time.sleep(1.1)
        first = bench.purge_orphans(database, min_age_s=0.5, only={name},
                                    bootstrap=True)
        print("first=%s" % ",".join(first.dropped), flush=True)
        holder.kill()
        holder.wait()
        second = bench.purge_orphans(database, min_age_s=0.5, only={name},
                                     bootstrap=True)
        print("second=%s" % ",".join(second.dropped), flush=True)
        present = database.query(
            "SELECT to_regnamespace(%s) IS NOT NULL AS present", (name,))[0]
        print("exists=%s" % bool(present["present"]), flush=True)
    finally:
        database.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % name)
        database.close()
finally:
    holder.kill()
'''


class BenchMaintenanceLockTest(unittest.TestCase):
    """#3735 B/P1 : la purge des non marqués exige le verrou exclusif.

    Classe sans `PgTestCase` : le scénario tourne dans un processus séparé,
    qui ne tient pas le verrou partagé de la suite de tests.
    """

    def test_live_suite_without_client_blocks_the_unmarked_purge(self) -> None:
        name = _name("locked")
        tmp = tempfile.mkdtemp(prefix="ameesh-bench-lock-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ)
        env["AMEESH_BENCH_LOCK"] = os.path.join(tmp, "bench.lock")
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = os.pathsep.join(
            [os.path.join(here, "src"), env.get("PYTHONPATH", "")])
        proc = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(_LOCK_SCENARIO),
             bench_mod.bench_dsn(), name],
            capture_output=True, text=True, env=env, timeout=240)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = dict(line.split("=", 1) for line in proc.stdout.splitlines()
                     if "=" in line)
        output = proc.stdout + proc.stderr
        self.assertEqual(lines.get("holder"), "ok", output)
        self.assertEqual(lines.get("first"), "", output)
        self.assertEqual(lines.get("second"), name, output)
        self.assertEqual(lines.get("exists"), "False", output)


if __name__ == "__main__":
    unittest.main()
