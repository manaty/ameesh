# SPDX-License-Identifier: AGPL-3.0-only
"""#2954 : un seul lot par exécution d'incident, même sous deux watchers.

Le défaut constaté (#2974/#2975, run 39554271532) : deux hôtes ont ouvert
l'incident de la même exécution dans la même fenêtre, chacun ayant lu
`issue_ref` avant l'insertion de l'autre. `work.add(..., unique_ref=True)`
prend un verrou consultatif de transaction sur la référence puis la relit,
quel que soit l'état des lots : le second créateur reçoit `DbError` et
`incident.open_incident` relit le gagnant.

Ces témoins vérifient la garantie au niveau du magasin (base réelle, psql et
psycopg) : refus de la seconde création, y compris quand le gagnant est
fermé ; plusieurs références distinctes restent possibles ; le défaut reste
inchangé pour les appels qui ne demandent pas l'unicité.
"""
from __future__ import annotations

from ameesh import db as db_mod, work
from .support import PgTestCase

REF = "ci-incident:manaty/ameesh:39554271532"


class IncidentUniqueRefTest(PgTestCase):

    def add(self, *, ref=REF, unique=True, title="Incident"):
        return work.add(self.db, title=title, type="bug", priority=1, app="ameesh",
                        source=ref, issue_ref=ref, actor="test",
                        unique_ref=unique)

    def count(self, ref=REF):
        rows = self.db.query(
            "SELECT count(*)::int AS n FROM work_items WHERE issue_ref = %s", (ref,))
        return int(rows[0]["n"])

    def test_second_creation_for_the_same_ref_is_refused(self):
        winner = self.add()
        with self.assertRaises(db_mod.DbError):
            self.add(title="Doublon")
        self.assertEqual(self.count(), 1)
        self.assertEqual(work.get(self.db, winner["id"])["state"], "intake")

    def test_a_closed_winner_still_holds_the_identity(self):
        # l'identité d'une exécution vaut quel que soit l'état du lot : un
        # incident fermé ne doit pas en rouvrir un second pour le même run
        winner = self.add()
        work.close(self.db, winner["id"], abandoned=True, actor="test")
        with self.assertRaises(db_mod.DbError):
            self.add(title="Doublon après clôture")
        self.assertEqual(self.count(), 1)

    def test_distinct_refs_are_independent(self):
        self.add(ref="ci-incident:manaty/ameesh:1")
        self.add(ref="ci-incident:manaty/ameesh:2")
        self.assertEqual(self.count("ci-incident:manaty/ameesh:1"), 1)
        self.assertEqual(self.count("ci-incident:manaty/ameesh:2"), 1)

    def test_default_creation_still_allows_a_shared_reference(self):
        # hors incident, plusieurs lots peuvent légitimement partager une
        # référence : l'unicité est opt-in, pas un changement global
        self.add(unique=False)
        self.add(unique=False, title="Second")
        self.assertEqual(self.count(), 2)

    def test_without_issue_ref_the_option_is_inert(self):
        first = work.add(self.db, title="Sans ref", type="bug", unique_ref=True,
                         actor="test")
        second = work.add(self.db, title="Sans ref 2", type="bug", unique_ref=True,
                          actor="test")
        self.assertNotEqual(first["id"], second["id"])
