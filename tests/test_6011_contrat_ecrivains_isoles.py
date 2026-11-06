# SPDX-License-Identifier: AGPL-3.0-only
"""#6011 — le lot lu par `it.item` sous les rôles d'écriture isolés.

`deploy/sql/role-module-writers.sql` ne donne à `ameesh_it_qa` et
`ameesh_it_proof` qu'une poignée de lectures : `work_items`, `project_modules`,
`work_module_milestones`, les créneaux de migration et l'inventaire de
compatibilité. `it.item` lit le lot sous ce rôle ; une lecture élargie de
`work.get` (les jalons et le journal du lot, #5993) est refusée, et seule la
CI complète le voyait (#420, #2481).

Ce témoin exécute le geste réel — `it.item()` — sous un rôle aux droits
**lus dans le script de déploiement**, pas recopiés : une table lue sans
GRANT le fait rougir avant la CI de paquet. La carte mesurée ne relie pas
cette lecture dynamique (`it.item` → `work.get`) au fichier de stockage ; le
module est donc au contrat, `tests/parts/toujours.txt`, exécuté sur toute
branche de lot (L1211).
"""
from __future__ import annotations

import os
import re
import uuid

from ameesh import db as db_mod, modules, work
from ameesh.modules import it
from ameesh.modules.storage import ModuleStore

from .support import REPO, PgTestCase

WRITERS_SQL = os.path.join(REPO, "deploy", "sql", "role-module-writers.sql")

#: Les groupes d'écriture du contrat de `role-module-writers.sql`. Le test
#: leur donne un nom suffixé (le nom fixe porte l'ACL de 0427, h-32 v2) mais
#: les droits sont ceux du script.
WRITER_ROLES = ("ameesh_it_qa", "ameesh_it_proof")

#: `SELECT format('GRANT …') \gexec` : le texte de la GRANT, entre quotes.
_FORMAT_GRANT = re.compile(r"format\('(GRANT[^']*)'")
_SELECT_ON = re.compile(r"^GRANT\s+SELECT\s+ON\s+(.+?)\s+TO\s+(.+)$", re.S)
_TABLE = re.compile(r"^%I\.([a-z_][a-z0-9_]*)$")


def granted_reads(path: str = WRITERS_SQL) -> set[str]:
    r"""Les tables que le script accorde en SELECT aux écrivains isolés.

    Le script écrit ses GRANT par `format('GRANT …', :'schema') \gexec` : on
    lit la chaîne de format, on retient les GRANT SELECT dont la liste de
    rôles contient un des deux groupes, et on rend les tables `%I.<nom>`.
    Un format inattendu lève : une lecture non reconnue ne doit pas passer
    pour un contrat satisfait.
    """
    with open(path, encoding="utf-8") as source:
        text = source.read()
    tables: set[str] = set()
    for statement in _FORMAT_GRANT.findall(text):
        match = _SELECT_ON.match(statement)
        if not match:
            continue
        objects, roles = match.groups()
        if not set(WRITER_ROLES) & {role.strip() for role in roles.split(",")}:
            continue
        for obj in objects.split(","):
            table = _TABLE.match(obj.strip())
            if table is None:
                raise AssertionError(
                    "role-module-writers.sql : GRANT SELECT inattendu %r"
                    % obj.strip())
            tables.add(table.group(1))
    return tables


class ModuleWritersReadContractTest(PgTestCase):
    """#6011 : `it.item` sous les droits exacts des écrivains isolés."""

    def setUp(self):
        super().setUp()
        federation = {"extensions": {"ameesh": {"projects": {
            "acme": {"modules": ["it"]}}}}}
        with self.db.transaction() as tx:
            ModuleStore(tx).replace("", modules.declarations(federation))
        self.lot = work.add(
            self.db, title="livraison", type="release", app="acme")["id"]
        self.tables = granted_reads()

    def isolated_role(self, name: str) -> str:
        role = "t_%s_%s" % (name, uuid.uuid4().hex[:8])
        q = db_mod.quote_ident
        self.db.execute("CREATE ROLE %s NOLOGIN" % q(role))
        self.addCleanup(self.drop_role, role)
        with self.db.transaction() as tx:
            tx.execute("GRANT USAGE ON SCHEMA %s TO %s"
                       % (q(self.schema), q(role)))
            for table in sorted(self.tables):
                tx.execute("GRANT SELECT ON %s.%s TO %s"
                           % (q(self.schema), q(table), q(role)))
        return role

    def drop_role(self, role: str) -> None:
        q = db_mod.quote_ident
        if self.db.query("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)):
            self.db.execute("DROP OWNED BY %s" % q(role))
            self.db.execute("DROP ROLE %s" % q(role))

    def test_the_reader_grants_come_from_the_deployment_script(self):
        # Le script est la source de vérité : s'il change de forme, le
        # parsing le dit au lieu de valider un contrat vide.
        for table in ("work_items", "project_modules",
                      "work_migration_reservations"):
            self.assertIn(table, self.tables)

    def test_it_item_reads_the_lot_under_both_isolated_writers(self):
        q = db_mod.quote_ident
        for name in WRITER_ROLES:
            with self.subTest(writer=name):
                role = self.isolated_role(name)
                # La table élargie de #5993 reste fermée à ce rôle : la
                # lecture qui réussit n'est pas un privilège du propriétaire.
                self.assertFalse(self.db.query(
                    "SELECT has_table_privilege(%s, %s, 'SELECT') AS p",
                    (role, "%s.work_item_milestones" % q(self.schema)))[0]["p"])
                with self.db.transaction() as tx:
                    tx.execute("SET LOCAL ROLE %s" % q(role))
                    row = it.item(tx, self.lot)
                self.assertEqual(row["id"], self.lot)
                self.assertEqual(row["app"], "acme")
                # Témoin du refus réel : le SELECT direct est bien refusé,
                # dans sa propre transaction (une erreur l'abandonne).
                with self.assertRaisesRegex(db_mod.DbError, "permission denied"):
                    with self.db.transaction() as tx:
                        tx.execute("SET LOCAL ROLE %s" % q(role))
                        tx.query("SELECT 1 FROM work_item_milestones LIMIT 1")
