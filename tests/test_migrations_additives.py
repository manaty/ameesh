# SPDX-License-Identifier: AGPL-3.0-only
"""L151 : migrer avant d'installer — reconnaître une migration additive."""
from __future__ import annotations

import dataclasses
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
            # forme simple (#174729) : un objet neuf peut perdre un droit, recevoir une contrainte
            "create function f(a text, b timestamptz default now()) returns int language sql"
            " as 'select 1'; revoke all on function f(text, timestamptz) from public;"
            " grant execute on function f(text, timestamptz) to r;",
            "create table n (id int); alter table n add unique (id);",
            "-- en-tête\ncreate table n (id int);\n/* entre deux */ alter table n add unique (id);",
            "alter table t add column c text default 'please add this';",
            "create table u (id bigint, c text default 'x');"
            " revoke insert on table u from public;",
            "alter table t add column revoked_at timestamptz;",
            "-- revoke all on t from r\n/* alter table t add unique (c); */ select 1;",
            "alter table t add column c int check (c > 0) unique;",
            "drop view if exists v; create view v as select * from t;",
            "-- ameesh: trigger compatible with previous code\n"
            "drop trigger if exists g on t; create trigger g after insert on t "
            "for each row execute function f();",
            # #5142 : la table est neuve, le code d'avant n'y écrit pas
            "create table n (id int); create trigger g before insert on n "
            "for each row execute function f();",
            "create table n (id int);"
            " create function f() returns trigger language plpgsql as $$ begin return new; end $$;"
            " create trigger g before insert on n for each row execute function f();",
            "create table n (id int); create rule r as on insert to n do nothing;",
            "create table n (id int); create policy p on n using (true);",
            "create table n (id int); alter table n enable trigger g;",
            "-- ameesh: trigger compatible with previous code\n"
            "do $$ begin execute 'create trigger g after insert on t "
            "for each row execute function f()'; end $$;",
            "comment on trigger g on t is 'garde';",
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
            ("create or replace function f() returns int language sql as 'select 1';"
             " revoke execute on function f() from app;", "revoke on f"),
            ("create table if not exists t (id bigint); revoke insert on t from app;",
             "revoke on t"),
            ("create table if not exists t (id bigint);"
             " alter table t add constraint k check (id > 0);", "add constraint k"),
            ("create function f(a text) returns int language sql as 'select 1';"
             " revoke all on function f(int) from public;", "revoke on f"),
            ("drop function f(); create function f() returns int language sql as 'select 1';"
             " revoke all on function f() from public;", "revoke on f"),
            ("create table u (id bigint); revoke all on u, t from r;", "revoke on u"),
            ("create table f (id bigint); revoke all on function f() from r;", "revoke on f"),
            ("do $$ begin if false then create table t (id bigint); end if; end $$;"
             " revoke insert on t from app;", "revoke on t"),
            ("do 'begin if false then create table t (id bigint); end if; end';"
             " revoke insert on t from app;", "revoke on t"),
            ("do e'it\\'s create table t (id bigint);'; revoke insert on t from app;",
             "revoke on t"),
            ("do $$ begin if false then create table t (id bigint); end if; end; -- c $$;\n"
             " revoke insert on t from app;", "revoke on t"),
            ("/* /* */ create table t (id bigint); */ revoke insert on t from app;",
             "revoke on t"),
            ("select '--'; revoke insert on t from app;", "revoke on t"),
            ("select '/*'; revoke insert on t from app; select '*/';", "revoke on t"),
            ("do $a$ create table t (id bigint); $b$ $a$; revoke insert on t from app;",
             "revoke on t"),
            ("do 'unterminated create table t (id bigint); revoke insert on t from app;",
             "in a string (not analysed)"),
            ("alter table t add unique (c);", "add unique"),
            ("alter table t add primary key (c);", "add primary key"),
            ("alter table t add check (c > 0);", "add check"),
            ("alter table t add foreign key (c) references u;", "add foreign key"),
            ("alter table t add constraint k check (c > 0);", "add constraint k"),
            ("alter table t drop constraint k; alter table t add constraint k check (c > 0);",
             "add constraint k"),
            ("alter table only t add column d int, add constraint k unique (c);",
             "add constraint k"),
            # B docs-01 : un `;` dans une chaîne ne termine pas l'instruction
            ("alter table t add column d text default ';', add unique (c);", "add unique"),
            ("alter table t add column d text default e';', add constraint k unique (c);",
             "add constraint k"),
            ("alter table t add column d text default ';', add primary key (c);",
             "add primary key"),
            ("alter table t add column d text default ';', add foreign key (c) references u;",
             "add foreign key"),
            ("do $$ begin alter table t add column d text default ';', add unique (c); end $$;",
             "in a string (not analysed)"),
            ("revoke all on \"a;b\" from r;", "privilege removed (revoke"),
            # B cx1 : un nom cité garde sa casse, et son contenu n'est pas du code
            ('create table "T" (id bigint); revoke insert on "t" from app;',
             "privilege removed (revoke on \""),
            ('select 1 as "create table t "; revoke insert on t from app;', "revoke on t"),
            ('create table "T" (id bigint); alter table "t" add unique (id);', "add unique"),
            ('select 1 as "create table t "; alter table t add unique (c);', "add unique"),
            # B cx1 : ni un `;` de chaîne $$, ni le corps d'un DO ne cachent la contrainte
            ("alter table t add column d text default $$;$$, add unique (c);", "add unique"),
            ("alter table t add column d text default $s$;$s$, add constraint k unique (c);",
             "add constraint k"),
            ("create table n (id int); do $$ begin alter table n add column x int;"
             " alter table t add unique (c); end $$;", "in a string (not analysed)"),
            # B cx1 : les mots d'une chaîne ne coupent pas une instruction
            ("create table n (id int);"
             " alter table t add column d text default 'alter table n ', add unique (c);",
             "add unique"),
            ("create table n (id int);"
             " alter table t add column d text default $$alter table n $$, add unique (c);",
             "add unique"),
            # mesh-design #174693 : une chaîne n'est pas analysée, même pour une table neuve
            ("create table n (id int); do $$ begin alter table n add unique (id); end $$;",
             "in a string (not analysed)"),
            ("execute 'revoke insert on t from app';", "in a string (not analysed)"),
            # B cx1 / #174729 : hors forme simple, plus de preuve d'objet neuf
            ("do $$ begin alter table t add /* separation */ unique (c); end $$;",
             "in a string (not analysed)"),
            ("do $$ begin alter table t add -- separation\n unique (c); end $$;",
             "in a string (not analysed)"),
            ("create table n (id int); alter table n add /* x */ unique (id);", "add unique"),
            ("create function f() returns int language sql as $$select 1$$;"
             " revoke all on function f() from public;", "revoke on f"),
            ('create table "n" (id int); revoke insert on n from app;', "revoke on n"),
            ("create table n (id int, c text default 'drop'); revoke insert on n from app;",
             "revoke on n"),
        ):
            with self.subTest(sql=sql):
                reasons = migrations.non_additive(_migration(sql))
                self.assertTrue(any(reason in r for r in reasons), reasons)

    def test_gardes_posees_sur_une_table_existante(self):
        # #5142 : elles changent ce que fait l'écriture du code d'avant
        for sql, reason in (
            ("create trigger g after insert on t for each row execute function f();",
             "trigger on a table not proven new (t)"),
            ("create or replace trigger g before update of a, b on only t"
             " for each row execute function f();", "trigger on a table not proven new (t)"),
            ("create constraint trigger g after insert on t for each row execute function f();",
             "trigger on a table not proven new (t)"),
            ("create rule r as on insert to t do nothing;", "rule on a table not proven new (t)"),
            ("create or replace rule r as on insert to t do nothing;", "rule on a table"),
            ("create policy p on t using (true);", "policy on a table not proven new (t)"),
            ("alter table t enable row level security;", "row level security"),
            ("alter table t force row level security;", "row level security"),
            ("create table t (id int); alter table t enable row level security;",
             "row level security"),
            ("alter table t enable always trigger g;", "trigger enabled"),
            ("alter table t enable rule r;", "rule enabled"),
            ("create table if not exists t (id int); create trigger g after insert on t"
             " for each row execute function f();", "trigger on a table"),
            ("drop table if exists t; create table t (id int); create rule r as on insert"
             " to t do nothing;", "rule on a table"),
            ('create table "T" (id int); create trigger g after insert on "t"'
             " for each row execute function f();", "trigger on a table"),
            ("create table n (id int); create trigger g after insert on t"
             " for each row execute function f();", "trigger on a table not proven new (t)"),
            ("do $$ begin execute 'create trigger g after insert on t"
             " for each row execute function f()'; end $$;", "trigger in a string"),
            ("execute 'alter table t enable row level security';", "row level security in a string"),
            # le marqueur est réservé aux triggers
            ("-- ameesh: trigger compatible with previous code\n"
             "create rule r as on insert to t do nothing;", "rule on a table"),
            ("-- ameesh: trigger compatible with previous code\n"
             "create policy p on t using (true);", "policy on a table"),
            ("-- ameesh: trigger compatible with previous code\n"
             "alter table t enable row level security;", "row level security"),
        ):
            with self.subTest(sql=sql):
                reasons = migrations.non_additive(_migration(sql))
                self.assertTrue(any(reason in r for r in reasons), reasons)

    def test_0529_rejouee_hors_seuil_est_non_additive(self):
        hold = next(m for m in migrations.discover() if m.version == 529)
        self.assertEqual(migrations.non_additive(hold), [])
        replayed = dataclasses.replace(hold, version=migrations._GUARDS_CHECKED_AFTER + 1)
        self.assertTrue(any("trigger on a table not proven new (work_items)" in r
                            for r in migrations.non_additive(replayed)))

    def test_les_versions_livrees_gardent_leur_classement(self):
        # 0524 retire un droit : le reclasser marquerait une migration appliquée
        grant = next(m for m in migrations.discover() if m.version == 524)
        self.assertEqual(migrations.non_additive(grant), [])


if __name__ == "__main__":
    unittest.main()
