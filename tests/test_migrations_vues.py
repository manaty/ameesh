# SPDX-License-Identifier: AGPL-3.0-only
"""#2229 : une vue `r.*` fige ses colonnes à la création.

0411 (ajout de `runner_seen_*` à `agent_registry`) est arrivée APRÈS 0428 (vue
`agent_mesh_overview` recréée avec `r.*`) : sur une base neuve la vue a les
colonnes, sur une base existante non, et le contrat du superviseur refuse
l'installation. Le témoin rejoue l'ordre réel d'une base existante ; la garde
refuse le motif dans les migrations.
"""
from __future__ import annotations

import dataclasses
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import uuid

from ameesh import db as db_mod
from ameesh import migrations
from tests.support import REPO, TEST_DSN, PgTestCase
from tests.role_acl import serialize_role_acl

ROLE_SQL = os.path.join(REPO, "deploy", "sql", "role-superviseur.sql")
RUNNER_SEEN = ["runner_seen_at", "runner_seen_host", "runner_seen_owner"]

#: vue `*` déjà livrée, admise tant que sa table source ne gagne aucune colonne
VUES_ETOILE_ADMISES = {"mesh_approvals_status": "mesh_approvals"}
_VUE = re.compile(r"\bcreate\s+(?:or\s+replace\s+)?view\s+([\w.\"]+)\s+as\b(.*?);",
                  re.IGNORECASE | re.DOTALL)
_ETOILE = re.compile(r"(?:\b\w+\.|\bselect\s+)\*", re.IGNORECASE)


def _sans_commentaires(sql: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", re.sub(r"--[^\n]*", " ", sql), flags=re.DOTALL)


class GardeVueEtoileTest(unittest.TestCase):
    def test_la_derniere_definition_d_une_vue_liste_ses_colonnes(self):
        """Une vue `*` ne voit pas les colonnes d'une migration arrivée après sa
        création, quel que soit son numéro : elle liste ses colonnes."""
        derniere: dict[str, tuple[str, str]] = {}
        textes = []
        for m in migrations.discover():
            texte = _sans_commentaires(m.sql)
            textes.append(texte)
            for nom, corps in _VUE.findall(texte):
                derniere[nom.lower()] = (m.label, corps)
        ecarts = [label for nom, (label, corps) in sorted(derniere.items())
                  if _ETOILE.search(corps) and nom not in VUES_ETOILE_ADMISES]
        self.assertEqual(ecarts, [], "vue définie avec `*` : lister ses colonnes")
        for vue, table in VUES_ETOILE_ADMISES.items():
            ajout = re.compile(r"\balter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?%s\s"
                               r"[^;]*\badd\s+column\b" % table, re.IGNORECASE)
            self.assertFalse(any(ajout.search(t) for t in textes),
                             "%s gagne une colonne : recréer %s sans `*`" % (table, vue))


class VueOverviewBaseExistanteTest(PgTestCase):
    """Le schéma de la classe est la base NEUVE de référence ; le témoin migre
    une seconde base dans l'ordre réel d'une base existante."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        serialize_role_acl(cls)

    def colonnes(self, db, rel: str) -> list[str]:
        return [r["attname"] for r in db.query(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass "
            "AND attnum > 0 AND NOT attisdropped ORDER BY attnum", (rel,))]

    def contrat(self, schema: str, role: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, PGOPTIONS="-c search_path=%s" % schema)
        return subprocess.run(
            ["psql", TEST_DSN, "-X", "-q", "-v", "ON_ERROR_STOP=1",
             "-v", "role=%s" % role, "-f", ROLE_SQL],
            capture_output=True, text=True, timeout=60, env=env)

    def test_0428_avant_0411_puis_0460(self):
        schema = "t_vue2229_%s" % uuid.uuid4().hex[:8]
        lecteur = "t_lec2229_%s" % uuid.uuid4().hex[:8]
        role = "t_sup2229_%s" % uuid.uuid4().hex[:8]
        dossier = tempfile.mkdtemp(prefix="ameesh-2229-")
        db = db_mod.connect(dataclasses.replace(self.cfg, schema=schema))
        try:
            toutes = migrations.discover()
            def passe(garde):
                for nom in os.listdir(dossier):
                    os.unlink(os.path.join(dossier, nom))
                for m in toutes:
                    if garde(m.version):
                        shutil.copy(m.path, dossier)
                migrations.migrate(db, dossier)
            # base existante : 0428 appliquée avant que 0411 ne soit fusionnée
            passe(lambda v: v <= 428 and v != 411)
            passe(lambda v: v < 460)
            self.assertTrue(set(RUNNER_SEEN) <= set(self.colonnes(db, "agent_registry")))
            self.assertFalse(set(RUNNER_SEEN) & set(self.colonnes(db, "agent_mesh_overview")),
                             "le témoin ne reproduit pas la base existante")
            refus = self.contrat(schema, role)
            self.assertNotEqual(refus.returncode, 0)
            self.assertIn("agent_mesh_overview.runner_seen_at", refus.stderr)
            # un droit de colonne et un droit de relation, posés avant 0460
            db.execute("CREATE ROLE %s NOLOGIN" % db_mod.quote_ident(lecteur))
            db.execute("GRANT SELECT (name, unread) ON agent_mesh_overview TO %s"
                       % db_mod.quote_ident(lecteur))
            db.execute("GRANT REFERENCES ON agent_mesh_overview TO %s"
                       % db_mod.quote_ident(lecteur))
            # La vue existante reste limitée aux colonnes accordées ci-dessus.
            # Une politique par défaut ne doit pas élargir ces droits à sa recréation.
            db.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA %s GRANT SELECT, UPDATE ON TABLES TO %s"
                       % (db_mod.quote_ident(schema), db_mod.quote_ident(lecteur)))
            passe(lambda v: True)
            # mêmes colonnes, même ordre, que sur une base neuve
            self.assertEqual(self.colonnes(db, "agent_mesh_overview"),
                             self.colonnes(self.db, "agent_mesh_overview"))
            self.assertTrue(set(RUNNER_SEEN) <= set(self.colonnes(db, "agent_mesh_overview")))
            droits = db.query(
                "SELECT has_column_privilege(%s, 'agent_mesh_overview', 'name', 'SELECT') AS nom, "
                "has_column_privilege(%s, 'agent_mesh_overview', 'turns', 'SELECT') AS hors, "
                "has_table_privilege(%s, 'agent_mesh_overview', 'REFERENCES') AS ref, "
                "has_column_privilege(%s, 'agent_mesh_overview', 'pending_prompt', 'SELECT') AS contenu, "
                "has_table_privilege(%s, 'agent_mesh_overview', 'UPDATE') AS ecrit",
                (lecteur, lecteur, lecteur, lecteur, lecteur))[0]
            self.assertEqual((droits["nom"], droits["hors"], droits["ref"]), (True, False, True))
            self.assertFalse(droits["contenu"])
            self.assertFalse(droits["ecrit"])
            # le contrat du superviseur est tenu, et lit la preuve de vie
            ok = self.contrat(schema, role)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertTrue(db.query(
                "SELECT has_column_privilege(%s, 'agent_mesh_overview', 'runner_seen_at', "
                "'SELECT') AS ok", (role,))[0]["ok"])
            # rejouable : rien à appliquer de plus
            self.assertEqual(migrations.migrate(db, dossier), [])
        finally:
            db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)
            for nom in (role, lecteur):
                if db.query("SELECT 1 FROM pg_roles WHERE rolname = %s", (nom,)):
                    db.execute("DROP OWNED BY %s" % db_mod.quote_ident(nom))
                    db.execute("DROP ROLE %s" % db_mod.quote_ident(nom))
            db.close()
            shutil.rmtree(dossier, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
