# SPDX-License-Identifier: AGPL-3.0-only
"""#5641 — `PgTestCase.fill`, l'aide commune des remplissages volumineux.

Le remplissage prépare, il ne mesure pas : il ne partage ni le délai ni les
triggers par ligne du code mesuré. Ces témoins tiennent ses trois promesses,
sous les deux pilotes : les triggers nommés sont rétablis (après un succès
comme après un échec, par la transaction elle-même), le délai n'est levé que
pour le remplissage, et un nom qui n'est pas un identifiant est refusé.
"""
from __future__ import annotations

import time
import unittest

from ameesh import db as db_mod

from .support import PgTestCase

TRIGGER = ("work_item_events", "ameesh_events_lot")


class FixtureFillTest(PgTestCase):

    def _lot(self) -> int:
        return int(self.db.query(
            "INSERT INTO work_items (title, state) VALUES ('fill-5641', 'build')"
            " RETURNING id")[0]["id"])

    def _enabled(self) -> str:
        return self.db.query(
            "SELECT tgenabled FROM pg_trigger"
            " WHERE tgrelid = 'work_item_events'::regclass AND tgname = %s",
            (TRIGGER[1],))[0]["tgenabled"]

    def _events(self, lot: int) -> int:
        return int(self.db.query(
            "SELECT count(*) AS n FROM ameesh_events WHERE object = %s",
            ("lot:%d" % lot,))[0]["n"])

    def test_trigger_coupe_pendant_le_remplissage_puis_retabli(self):
        lot = self._lot()
        before = self._events(lot)
        self.fill("INSERT INTO work_item_events (work_item_id, state, note, actor)"
                  " SELECT %s, 'build', 'n' || g, 'agent' FROM generate_series(1, 50) g",
                  (lot,), triggers=(TRIGGER,))
        self.assertEqual(self._events(lot), before,
                         "le trigger d'événements est coupé pendant le remplissage")
        self.assertEqual(self._enabled(), "O")
        self.db.execute("INSERT INTO work_item_events (work_item_id, state, note, actor)"
                        " VALUES (%s, 'build', 'après', 'agent')", (lot,))
        self.assertEqual(self._events(lot), before + 1,
                         "rétabli : l'insertion suivante émet son événement")

    def test_un_remplissage_en_echec_retablit_les_triggers(self):
        lot = self._lot()
        with self.assertRaises(db_mod.DbError):
            # la deuxième ligne viole la clé étrangère : la transaction tombe
            self.fill("INSERT INTO work_item_events (work_item_id, state, note, actor)"
                      " VALUES (%s, 'build', 'a', 'agent'), (-1, 'build', 'b', 'agent')",
                      (lot,), triggers=(TRIGGER,))
        self.assertEqual(self._enabled(), "O",
                         "le ROLLBACK rétablit le trigger : rien ne reste coupé")
        self.assertEqual(int(self.db.query(
            "SELECT count(*) AS n FROM work_item_events WHERE work_item_id = %s",
            (lot,))[0]["n"]), 0)

    def test_nom_de_trigger_refuse_hors_identifiant(self):
        for pair in (("work_item_events", "x; DROP TABLE work_items"),
                     ("work_item_events",), ("Work_Items", "t")):
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                self.fill("SELECT 1", triggers=(pair,))
        self.assertEqual(self._enabled(), "O")

    def test_delai_leve_pour_le_seul_remplissage(self):
        if not isinstance(self.db, db_mod.PsycopgDriver):
            self.skipTest("psql : un processus par instruction, le délai de "
                          "session ne se règle pas entre deux appels")
        self.db.execute("SET statement_timeout = '200ms'")
        self.addCleanup(self.db.execute, "RESET statement_timeout")
        lot = self._lot()
        self.fill("INSERT INTO work_item_events (work_item_id, state, note, actor)"
                  " SELECT %s, 'build', 'lent', 'agent' FROM pg_sleep(0.5)", (lot,))
        self.assertEqual(self.db.query("SHOW statement_timeout")[0]["statement_timeout"],
                         "200ms", "SET LOCAL : le délai de la session revient")
        with self.assertRaises(db_mod.DbError):
            self.db.execute("SELECT pg_sleep(0.5)")

    def test_delai_client_du_pilote_psql_rendu(self):
        if "_client_timeout" not in vars(self.db):
            self.skipTest("délai client propre au pilote psql")
        saved = self.db._client_timeout
        seen = []
        original = self.db.transaction

        def transaction():
            seen.append(self.db._client_timeout)
            return original()

        self.db.transaction = transaction
        self.addCleanup(vars(self.db).pop, "transaction", None)
        lot = self._lot()
        self.fill("INSERT INTO work_item_events (work_item_id, state, note, actor)"
                  " VALUES (%s, 'build', 'c', 'agent')", (lot,))
        self.assertEqual(seen, [None], "délai client levé pendant le remplissage")
        self.assertEqual(self.db._client_timeout, saved)


if __name__ == "__main__":
    unittest.main()
