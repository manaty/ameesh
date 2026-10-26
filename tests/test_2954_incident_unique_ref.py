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

#3607 : l'identité d'une exécution vit aussi dans `source`. Un lot ouvert à la
main la porte, son `issue_ref` étant le ticket miroir ; la synchro des tickets
peut aussi déplacer l'`issue_ref` du gagnant après son insertion. La relecture
sous le verrou couvre donc les deux champs exactement, et `by_source` retrouve
le lot quel que soit son état.
"""
from __future__ import annotations

from ameesh import db as db_mod, storage, work
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

    def count_source(self, ref=REF):
        rows = self.db.query(
            "SELECT count(*)::int AS n FROM work_items WHERE source = %s", (ref,))
        return int(rows[0]["n"])

    def test_second_creation_for_the_same_ref_is_refused(self):
        winner = self.add()
        with self.assertRaises(db_mod.DbError):
            self.add(title="Doublon")
        self.assertEqual(self.count(), 1)
        self.assertEqual(work.get(self.db, winner["id"])["state"], "intake")

    def test_a_manual_incident_under_the_source_holds_the_identity(self):
        # #3607 : un lot ouvert à la main porte l'identité de l'exécution dans
        # `source`, son `issue_ref` étant le ticket miroir ; l'ouverture
        # automatique de la même exécution doit être refusée.
        work.add(self.db, title="Incident ouvert à la main", type="bug",
                 priority=1, app="ameesh", source=REF,
                 issue_ref="manaty/ameesh#2787", actor="test")
        with self.assertRaises(db_mod.DbError):
            self.add(title="Doublon automatique")
        self.assertEqual(self.count_source(), 1)
        self.assertEqual(self.count(), 0)

    def test_the_mirror_ticket_move_does_not_reopen_the_identity(self):
        # #3607 : après la synchro, l'`issue_ref` du gagnant devient le ticket
        # miroir ; la relecture sous le verrou unique_ref le reconnaît par sa
        # `source` et refuse le second lot.
        winner = self.add()
        self.db.execute("UPDATE work_items SET issue_ref = %s WHERE id = %s",
                        ("manaty/ameesh#2787", int(winner["id"])))
        with self.assertRaises(db_mod.DbError):
            self.add(title="Doublon après synchro")
        self.assertEqual(self.count_source(), 1)

    def test_by_source_reads_any_state_and_only_the_exact_reference(self):
        manual = work.add(self.db, title="Incident ouvert à la main", type="bug",
                          app="ameesh", source=REF,
                          issue_ref="manaty/ameesh#2787", actor="test")
        ops = storage.of(self.db).work
        self.assertEqual([int(r["id"]) for r in ops.by_source(REF)],
                         [int(manual["id"])])
        # fermé, le lot garde l'identité de l'exécution
        work.close(self.db, manual["id"], abandoned=True, actor="test")
        self.assertEqual([int(r["id"]) for r in ops.by_source(REF)],
                         [int(manual["id"])])
        # la casse d'un dépôt GitHub ne fait pas une autre exécution
        self.assertEqual([int(r["id"]) for r in ops.by_source(REF.upper())],
                         [int(manual["id"])])
        # exactement : ni préfixe, ni suffixe, ni source vide (tous les lots
        # sans source)
        self.assertEqual(ops.by_source(REF.rsplit(":", 1)[0]), [])
        self.assertEqual(ops.by_source(REF + "0"), [])
        self.assertEqual(ops.by_source(""), [])

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
