# SPDX-License-Identifier: AGPL-3.0-only
"""L151 : migrer avant d'installer — reconnaître une migration additive."""
from __future__ import annotations

import unittest

from ameesh import migrations


def _migration(sql: str) -> migrations.Migration:
    return migrations.Migration(version=9999, name="essai", path="", sql=sql, checksum="")


class NonAdditiveTest(unittest.TestCase):
    def test_migrations_du_depot_additives(self):
        # aucune fausse alerte sur ce qui est livré : vues recréées,
        # contraintes remplacées, colonnes nullables ou avec défaut
        self.assertEqual(
            {m.label: migrations.non_additive(m) for m in migrations.discover()
             if migrations.non_additive(m) and m.version != 518}, {})

    def test_t5_switch_requires_explicit_non_additive_authorization(self):
        switch = next(m for m in migrations.discover() if m.version == 518)
        self.assertIn("marked non-additive", migrations.non_additive(switch))

    def test_ajouts_additifs(self):
        for sql in (
            "alter table t add column if not exists c bigint check (c is null or c >= 0);",
            "alter table t add column c text not null default '';",
            "create table if not exists u (id bigserial primary key, x text not null);",
            "create index if not exists u_x on u (x);",
            "-- ameesh: constraint not tightened\nalter table t drop constraint if exists t_c;"
            " alter table t add constraint t_c check (c > 0);",
            "alter table t drop constraint t_c;",
            "create table u (id bigint); alter table u add constraint k check (id > 0);",
            "create or replace function f(a text) returns int language sql as 'select 1';"
            " revoke all on function f(text) from public; grant execute on function f(text) to r;",
            "create table u (id bigint); revoke insert on table u from public;",
            "alter table t add column revoked_at timestamptz;",
            "drop view if exists v; create view v as select * from t;",
            "drop trigger if exists g on t; create trigger g after insert on t "
            "for each row execute function f();",
            "-- on ne fait pas de drop table ici\nalter table t add column d text;",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(migrations.non_additive(_migration(sql)), [])

    def test_gestes_qui_cassent_le_code_d_avant(self):
        for sql, reason in (
            ("drop table t;", "drop table"),
            ("alter table t drop column c;", "drop column"),
            ("alter table t rename column c to d;", "rename"),
            ("alter table t alter column c type bigint;", "type"),
            ("alter table t alter column c set data type text;", "type"),
            ("alter table t alter column c set not null;", "set not null"),
            ("alter table t add column c text not null;", "without default"),
            ("truncate t;", "truncate"),
            ("delete from t where c is null;", "delete"),
            ("drop view if exists v;", "drop view v"),
            ("drop function f();", "drop function f"),
            ("-- ameesh: non additive\nalter table t add constraint k check (c > 0);",
             "marked"),
            ("revoke insert, update on schema_migrations from r;", "revoke on schema_migrations"),
            ("revoke all on function f(text) from public;", "revoke on f"),
            ("revoke all on all tables in schema s from r;", "revoke on all"),
            ("revoke r from u;", "privilege removed (revoke)"),
            ("alter table t add constraint k check (c > 0);", "add constraint k"),
            ("alter table t drop constraint k; alter table t add constraint k check (c > 0);",
             "add constraint k"),
            ("alter table only t add column d int, add constraint k unique (c);",
             "add constraint k"),
        ):
            with self.subTest(sql=sql):
                reasons = migrations.non_additive(_migration(sql))
                self.assertTrue(any(reason in r for r in reasons), reasons)

    def test_les_versions_livrees_gardent_leur_classement(self):
        # 0524 retire un droit : le reclasser marquerait une migration appliquée
        grant = next(m for m in migrations.discover() if m.version == 524)
        self.assertEqual(migrations.non_additive(grant), [])


if __name__ == "__main__":
    unittest.main()
