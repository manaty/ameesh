# SPDX-License-Identifier: AGPL-3.0-only
"""#3752 : le schéma cloné du gabarit vaut un schéma migré (tests/gabarit.py).

Témoins : catalogue identique entre `migrate()` et le clone, pour chaque
pilote disponible ; empreinte invalidée par toute migration ajoutée ou
modifiée ; isolation entre schémas clonés ; replis sur `migrate()`.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
import uuid
from unittest import mock

from ameesh import db as db_mod
from ameesh import migrations

from . import gabarit
from .support import PgTestCase

#: (catégorie, requête) ; `%s` reçoit le nom du schéma. Les noms de schéma
#: dans les résultats sont normalisés avant comparaison.
CATALOGUE = (
    ("schema", "SELECT coalesce(nspacl::text, '') AS acl, obj_description(oid, 'pg_namespace') AS comm"
               " FROM pg_namespace WHERE nspname = %s"),
    ("relations", "SELECT c.relname, c.relkind, c.relrowsecurity, c.relforcerowsecurity,"
                  " coalesce(c.relacl::text, '') AS acl, obj_description(c.oid, 'pg_class') AS comm"
                  " FROM pg_class c WHERE c.relnamespace = to_regnamespace(%s)"),
    ("colonnes", "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod) AS type,"
                 " a.attnotnull, pg_get_expr(d.adbin, d.adrelid) AS defaut, a.attidentity,"
                 " a.attgenerated, coalesce(a.attacl::text, '') AS acl,"
                 " col_description(c.oid, a.attnum) AS comm,"
                 " row_number() OVER (PARTITION BY c.oid ORDER BY a.attnum) AS rang"
                 " FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
                 " LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
                 " WHERE c.relnamespace = to_regnamespace(%s) AND a.attnum > 0"
                 " AND NOT a.attisdropped"),
    # sans parenthèses : le DDL rejoué ré-imbrique les AND d'un CHECK
    # (associativité), sans changer la contrainte
    ("contraintes", "SELECT conrelid::regclass::text AS rel, conname, contype,"
                    " translate(pg_get_constraintdef(oid), '()', '') AS def FROM pg_constraint"
                    " WHERE connamespace = to_regnamespace(%s)"),
    ("index", "SELECT indexname, pg_get_indexdef((quote_ident(schemaname) || '.' || quote_ident(indexname))"
              "::regclass) AS def FROM pg_indexes WHERE schemaname = %s"),
    ("triggers", "SELECT t.tgname, pg_get_triggerdef(t.oid) AS def, t.tgenabled FROM pg_trigger t"
                 " JOIN pg_class c ON c.oid = t.tgrelid"
                 " WHERE c.relnamespace = to_regnamespace(%s) AND NOT t.tgisinternal"),
    # config sans guillemets : le `search_path="t_x"` d'un ALTER FUNCTION … SET
    # %I vaut le `search_path=t_x` que rejoue pg_dump
    ("fonctions", "SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args, p.prokind,"
                  " CASE WHEN p.prokind IN ('f', 'p') THEN pg_get_functiondef(p.oid) END AS def,"
                  " coalesce(p.proacl::text, '') AS acl, p.prosecdef,"
                  " replace(coalesce(array_to_string(p.proconfig, ','), ''), '\"', '') AS config,"
                  " obj_description(p.oid, 'pg_proc') AS comm"
                  " FROM pg_proc p WHERE p.pronamespace = to_regnamespace(%s)"),
    ("politiques", "SELECT tablename, policyname, permissive, roles::text AS roles, cmd, qual,"
                   " with_check FROM pg_policies WHERE schemaname = %s"),
    ("types", "SELECT t.typname, t.typtype, coalesce(t.typacl::text, '') AS acl,"
              " coalesce((SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)"
              " FROM pg_enum e WHERE e.enumtypid = t.oid), '') AS labels"
              " FROM pg_type t WHERE t.typnamespace = to_regnamespace(%s)"),
    ("sequences", "SELECT sequencename, data_type::text AS type, start_value, increment_by,"
                  " max_value, cycle, last_value FROM pg_sequences WHERE schemaname = %s"),
    ("droits_par_defaut", "SELECT defaclrole::regrole::text AS role, defaclobjtype,"
                          " defaclacl::text AS acl FROM pg_default_acl"
                          " WHERE defaclnamespace = to_regnamespace(%s)"),
)
#: valeurs qu'une migration tire au moment où elle passe (hasard, horloge) :
#: seule leur présence est comparée, pas leur valeur
TIRAGES = {("ameesh_events_journal", "journal")}
HORLOGE = ("timestamp with time zone", "timestamp without time zone", "date")


def _normalise(value, schema: str):
    if isinstance(value, str):
        return value.replace(schema, "<schema>")
    if isinstance(value, (list, tuple)):
        return tuple(_normalise(v, schema) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _normalise(v, schema)) for k, v in value.items()))
    return value


def signature(db, schema: str) -> dict:
    """Le catalogue et les données du schéma, noms de schéma normalisés."""
    out = {}
    for categorie, sql in CATALOGUE:
        out[categorie] = sorted(map(repr, (_normalise(row, schema)
                                           for row in db.query(sql, (schema,)))))
    horloge = {}
    for row in db.query(
            "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod) AS type"
            " FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
            " WHERE c.relnamespace = to_regnamespace(%s) AND c.relkind IN ('r', 'p')"
            " AND a.attnum > 0 AND NOT a.attisdropped", (schema,)):
        if row["type"] in HORLOGE or (row["relname"], row["attname"]) in TIRAGES:
            horloge.setdefault(row["relname"], set()).add(row["attname"])
    donnees = {}
    for row in db.query("SELECT relname FROM pg_class WHERE relkind IN ('r', 'p')"
                        " AND relnamespace = to_regnamespace(%s)", (schema,)):
        table = row["relname"]
        lignes = []
        for ligne in db.query("SELECT to_jsonb(t)::text AS j FROM %s.%s t" % (
                db_mod.quote_ident(schema), db_mod.quote_ident(table))):
            valeurs = json.loads(ligne["j"])
            for colonne in horloge.get(table, ()):
                valeurs[colonne] = valeurs.get(colonne) is not None
            lignes.append(repr(_normalise(valeurs, schema)))
        donnees[table] = sorted(lignes)
    out["donnees"] = donnees
    return out


class GabaritTest(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.schemas: list[str] = []

    def tearDown(self) -> None:
        for schema in self.schemas:
            self.db.execute("DROP SCHEMA IF EXISTS %s CASCADE" % db_mod.quote_ident(schema))
        super().tearDown()

    def nouveau(self, prefixe: str, driver: str | None = None):
        schema = "t_gab_%s_%s" % (prefixe, uuid.uuid4().hex[:8])
        self.schemas.append(schema)
        db = db_mod.connect(dataclasses.replace(self.cfg, schema=schema), driver=driver)
        self.addCleanup(db.close)
        return schema, db

    def exiger_pg_dump(self) -> None:
        serveur = int(self.db.query(
            "SELECT current_setting('server_version_num') AS v")[0]["v"]) // 10000
        if gabarit.pg_dump_pour(serveur) is None:
            self.skipTest("aucun pg_dump >= %d : le gabarit se replie sur migrate()" % serveur)

    def test_le_schema_de_la_classe_est_a_jour(self):
        self.assertIn(gabarit.dernier, ("clone", "migrate", "migrate+gabarit"))
        self.assertEqual(migrations.pending(self.db), [])
        missing, modified, unknown = migrations.check(self.db)
        self.assertEqual((missing, modified, unknown), ([], [], []))

    def test_le_clone_vaut_migrate_pour_chaque_pilote(self):
        self.exiger_pg_dump()
        # le pilote de ce job toujours ; l'autre aussi quand il est installé
        # (le job psql n'a pas psycopg : son job à lui le couvre)
        pilotes = [self.db.name] + [d for d in ("psycopg", "psql") if d != self.db.name]
        for driver in pilotes:
            with self.subTest(pilote=driver):
                try:
                    source, a = self.nouveau("src", driver)
                except db_mod.Unavailable:
                    if driver == self.db.name:
                        raise
                    continue
                migrations.migrate(a, log=None)
                ddl = gabarit._dump(a, source)
                self.assertIsNotNone(ddl)
                cible, b = self.nouveau("clone", driver)
                gabarit.cloner(b, cible, source, ddl)
                attendu, obtenu = signature(a, source), signature(b, cible)
                self.assertGreater(len(attendu["relations"]), 100)
                for categorie in attendu:
                    self.assertEqual(obtenu[categorie], attendu[categorie], categorie)
                # la connexion garde son search_path après le rejeu
                self.assertEqual(b.query("SELECT current_schema() AS s")[0]["s"], cible)
                self.assertEqual(migrations.pending(b), [])

    def test_clones_isoles_et_a_jour(self):
        self.exiger_pg_dump()
        premier, a = self.nouveau("un")
        self.assertIn(gabarit.preparer(a, premier), ("clone", "migrate+gabarit", "migrate"))
        second, b = self.nouveau("deux")
        self.assertEqual(gabarit.preparer(b, second), "clone")
        self.assertEqual(migrations.pending(b), [])
        a.execute("INSERT INTO agent_registry (name, harness) VALUES ('fuite', 'claude')")
        self.assertEqual(b.query("SELECT count(*) AS n FROM agent_registry")[0]["n"], 0)
        self.assertEqual(a.query("SELECT count(*) AS n FROM agent_registry")[0]["n"], 1)

    def test_une_migration_ajoutee_ou_modifiee_invalide_le_gabarit(self):
        copie = tempfile.mkdtemp(prefix="ameesh-gabarit-")
        self.addCleanup(shutil.rmtree, copie, True)
        for migration in migrations.discover(migrations.MIGRATIONS_DIR):
            shutil.copy(migration.path, copie)
        base = gabarit.empreinte(self.db)
        self.assertEqual(gabarit.empreinte(self.db, copie), base)
        derniere = max(m.version for m in migrations.discover(copie))
        ajout = os.path.join(copie, "%04d_temoin_3752.sql" % (derniere + 1))
        with open(ajout, "w", encoding="utf-8") as fh:
            fh.write("create table temoin_3752 (id int);\n")
        ajoutee = gabarit.empreinte(self.db, copie)
        self.assertNotEqual(ajoutee, base)
        with open(ajout, "a", encoding="utf-8") as fh:
            fh.write("-- modifiée\n")
        self.assertNotIn(gabarit.empreinte(self.db, copie), (base, ajoutee))

    def test_sans_gabarit_ni_pg_dump_migrate_comme_avant(self):
        schema, db = self.nouveau("repli")
        with mock.patch.object(gabarit, "_lire", return_value=None), \
                mock.patch.object(gabarit, "pg_dump_pour", return_value=None):
            self.assertEqual(gabarit.preparer(db, schema), "migrate")
        self.assertEqual(migrations.pending(db), [])

    def test_gabarit_coupe_par_l_environnement(self):
        schema, db = self.nouveau("coupe")
        with mock.patch.dict(os.environ, {"AMEESH_TEST_GABARIT": "0"}), \
                mock.patch.object(gabarit, "_lire") as lire:
            self.assertEqual(gabarit.preparer(db, schema), "migrate")
        lire.assert_not_called()
        self.assertEqual(migrations.pending(db), [])

    def test_un_rejeu_en_echec_retire_le_gabarit_et_migre(self):
        schema, db = self.nouveau("echec")
        with mock.patch.object(gabarit, "_lire", return_value=("t_x", "SELECT 1/0;")), \
                mock.patch.object(gabarit, "_retirer") as retirer, \
                mock.patch.object(gabarit, "_ranger", return_value=False):
            self.assertEqual(gabarit.preparer(db, schema), "migrate")
        retirer.assert_called_once()
        self.assertEqual(migrations.pending(db), [])
