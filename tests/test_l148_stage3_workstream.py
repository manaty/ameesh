# SPDX-License-Identifier: AGPL-3.0-only
"""L148 (#147) stage 3, migration A : `chantier` et `workstream` restent en phase.

`0410_rename_chantier_workstream.sql` ajoute `agent_registry.workstream`, la
remplit depuis `chantier`, et pose un déclencheur qui recopie l'une sur
l'autre : le code d'avant (qui n'écrit que `chantier`) et celui d'après (qui
écrira `workstream`) tournent sur le même schéma, jusqu'à la migration D qui
retirera le déclencheur puis `chantier`.

Ce module touche la couche base et les migrations : `tests/parts/pilote-psql.txt`
le fait tourner aussi sous le pilote psql.
"""
from __future__ import annotations

import os
import unittest

from ameesh import migrations

from .support import PgTestCase

MIGRATION = "0410_rename_chantier_workstream.sql"


class WorkstreamSyncTest(PgTestCase):
    """Le déclencheur : une seule colonne écrite, les deux restent égales."""

    def insert(self, name, **columns):
        cols = ["name", *columns]
        self.db.execute(
            "INSERT INTO agent_registry (%s) VALUES (%s)"
            % (", ".join(cols), ", ".join(["%s"] * len(cols))),
            [name, *columns.values()])
        return self.pair(name)

    def pair(self, name):
        row = self.db.query(
            "SELECT chantier, workstream FROM agent_registry WHERE name = %s", (name,))[0]
        return row["chantier"], row["workstream"]

    def test_un_client_d_avant_ecrit_chantier(self):
        self.assertEqual(self.insert("dev1", chantier="ameesh"), ("ameesh", "ameesh"))

    def test_un_client_d_apres_ecrit_workstream(self):
        self.assertEqual(self.insert("dev1", workstream="ameesh"), ("ameesh", "ameesh"))

    def test_les_mises_a_jour_se_recopient_dans_les_deux_sens(self):
        self.insert("dev1", chantier="ameesh")
        self.db.execute("UPDATE agent_registry SET chantier = 'nexlink' WHERE name = 'dev1'")
        self.assertEqual(self.pair("dev1"), ("nexlink", "nexlink"))
        self.db.execute("UPDATE agent_registry SET workstream = 'ameesh' WHERE name = 'dev1'")
        self.assertEqual(self.pair("dev1"), ("ameesh", "ameesh"))

    def test_le_nouveau_nom_gagne_si_les_deux_sont_ecrits(self):
        self.insert("dev1", chantier="ameesh")
        self.db.execute("UPDATE agent_registry SET chantier = 'avant', workstream = 'apres'"
                        " WHERE name = 'dev1'")
        self.assertEqual(self.pair("dev1"), ("apres", "apres"))

    def test_le_vide_se_propage(self):
        self.insert("dev1", chantier="ameesh")
        self.db.execute("UPDATE agent_registry SET workstream = '' WHERE name = 'dev1'")
        self.assertEqual(self.pair("dev1"), ("", ""))
        self.assertEqual(self.insert("dev2"), ("", ""))


class WorkstreamBackfillTest(PgTestCase):
    """La migration remplit `workstream` pour les lignes du code d'avant."""

    def pair(self, name):
        row = self.db.query(
            "SELECT chantier, workstream FROM agent_registry WHERE name = %s", (name,))[0]
        return row["chantier"], row["workstream"]

    def test_les_lignes_existantes_sont_remplies(self):
        path = os.path.join(migrations.MIGRATIONS_DIR, MIGRATION)
        with open(path, encoding="utf-8") as fh:
            sql = fh.read()
        # l'état d'avant : une ligne écrite quand seul `chantier` existait
        self.addCleanup(self.db.script, sql)
        self.db.script("DROP TRIGGER IF EXISTS agent_registry_workstream_sync"
                       " ON agent_registry")
        self.db.execute("INSERT INTO agent_registry (name, chantier) VALUES ('avant', 'ameesh')")
        self.assertEqual(self.pair("avant"), ("ameesh", ""))
        self.db.script(sql)
        self.assertEqual(self.pair("avant"), ("ameesh", "ameesh"))


if __name__ == "__main__":
    unittest.main()
