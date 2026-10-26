# SPDX-License-Identifier: AGPL-3.0-only
"""#3735 : banc ameesh-pg — orphelins t_*, gonflement, signal doctor.

Témoins du lot : un schéma `t_*` assez vieux et sans session est purgé ; un
schéma tenu par une session active est gardé ; un schéma récent est gardé ; un
dernier autovacuum ancien est signalé. Le banc réel n'est pas modifié par ces
témoins : ils travaillent sur la base jetable de la suite (`AMEESH_TEST_DSN`).
"""
from __future__ import annotations

import time
import unittest
import uuid

from ameesh import bench as bench_mod
from ameesh import db as db_mod

from .support import PgTestCase


def _name(tag: str) -> str:
    return "t_%s_%s" % (tag, uuid.uuid4().hex[:8])


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
        # l'âge lu est celui de la première relation : une seconde de marge
        # suffit pour distinguer « vieux » de « créé après la mesure ».
        time.sleep(1.1)
        other = db_mod.connect(self.cfg)
        try:
            with other.transaction() as tx:
                tx.query('SELECT * FROM "%s".x' % active)
                found = {orphan.schema for orphan in bench_mod.orphan_schemas(
                    self.db, min_age_s=0.5)}
                self.assertIn(old, found)
                self.assertNotIn(active, found)   # session qui tient un verrou
                dry = bench_mod.purge_orphans(self.db, min_age_s=0.5, dry_run=True)
                self.assertIn(old, [schema for schema, _ in dry.kept])
                self.assertTrue(self._exists(old))
                result = bench_mod.purge_orphans(self.db, min_age_s=0.5)
        finally:
            other.close()
        self.assertIn(old, result.dropped)
        self.assertNotIn(active, result.dropped)
        self.assertFalse(self._exists(old))
        self.assertTrue(self._exists(active))
        # le schéma créé après la mesure est trop jeune : jamais purgé
        self._create(young)
        fresh = bench_mod.purge_orphans(self.db, min_age_s=3600.0)
        self.assertNotIn(young, fresh.dropped)
        self.assertTrue(self._exists(young))

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


if __name__ == "__main__":
    unittest.main()
