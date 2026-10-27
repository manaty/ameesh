# SPDX-License-Identifier: AGPL-3.0-only
"""#4086 : un redémarrage d'exécuteur ne fait plus changer d'hôte un rôle élu.

L'exécuteur qui s'arrête en tenant un rôle élu le réserve à son hôte
(`role_handover`, challenger `@restart`) AVANT de rendre le bail : les autres
candidats attendent `RESTART_RESERVE_S`, l'exécuteur relancé sur le même hôte
réclame sans délai de rang (même hôte : la session est gardée). Un hôte qui ne
revient pas laisse la main à l'échéance. Et un relevé de ressources écrit
pendant la lecture n'est plus « daté dans le futur ».
"""
from __future__ import annotations

import time
import unittest

from ameesh import election, registry, role_handover_pg as handover_pg
from ameesh import role_standings_pg as pg, runner

from .test_l165_ep2_election_score import _RoleCase


class RestartReservationTest(_RoleCase):
    def setUp(self) -> None:
        super().setUp()
        self.role_row = self.role()
        self.ready_pair()
        # `atelier` est le mieux classé (rang 0) ; `banc` détient le rôle.
        self.standing("pilote", "atelier", 99.0)
        self.standing("pilote", "banc", 90.0)

    def runner(self, **kwargs):
        obj = runner.Runner(self.cfg, self.db, **kwargs)
        self.addCleanup(obj.stop.set)
        return obj

    def worker(self, obj):
        row = self.hold("pilote", "banc", epoch=7, expires_in=300)
        worker = runner.AgentWorker(obj, registry.get(self.db, "pilote"), row)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def reserved(self):
        return handover_pg.restart_reservation(self.db, "pilote")

    def test_redemarrage_du_detenteur_reserve_le_role_a_son_hote(self):
        obj = self.runner()
        worker = self.worker(obj)
        obj.draining.set()  # SIGTERM de `vm.sh redemarrer`
        self.assertTrue(worker.release_lease())
        mark = self.reserved()
        self.assertEqual(mark["host"], "banc")
        self.assertGreater(mark["left_s"], election.RESTART_RESERVE_S - 10)
        # Le mieux classé, sur un autre hôte, n'y touche pas.
        allowed, reason, _ = election.claim_wait(
            self.db, self.role_row, "atelier", 1000.0, now=1000.0)
        self.assertFalse(allowed)
        self.assertIn("restart reservation", reason)
        self.assertIn("banc", reason)
        # L'exécuteur relancé sur banc (rang 1) réclame sans attendre son rang.
        again = self.runner()
        allowed, reason, mine = again.election_claim_allowed(self.role_row)
        self.assertTrue(allowed, reason)
        self.assertEqual(mine["rank"], 1)
        # Sa propre réservation n'est pas une marque de bascule à attendre.
        self.assertEqual(again.handover_wait_s("pilote"), 0.0)

    def test_hote_qui_ne_revient_pas_rend_la_main_a_l_echeance(self):
        obj = self.runner()
        worker = self.worker(obj)
        obj.stop.set()
        worker.release_lease()
        self.assertIsNotNone(self.reserved())
        self.db.execute("UPDATE role_handover"
                        " SET released_until = clock_timestamp() - interval '1 second'")
        self.assertIsNone(self.reserved())
        allowed, reason, _ = election.claim_wait(
            self.db, self.role_row, "atelier", 1000.0, now=1000.0)
        self.assertTrue(allowed, reason)
        self.assertLess(election.RESTART_RESERVE_S, 300.0)  # §11 : moins de 5 min

    def test_aucune_reservation_en_marche_ni_pour_une_bascule(self):
        obj = self.runner()
        worker = self.worker(obj)
        worker.release_lease()  # fin de worker ordinaire : exécuteur en marche
        self.assertIsNone(self.reserved())
        obj = self.runner()
        worker = self.worker(obj)
        obj.note_handover_release("pilote", "atelier", 0.0)
        obj.draining.set()
        worker.release_lease()  # bascule planifiée : le challenger réclame
        self.assertIsNone(self.reserved())

    def test_bail_perdu_ou_passage_unique_ne_reserve_pas(self):
        obj = self.runner()
        worker = self.worker(obj)
        worker.lease_lost.set()
        obj.stop.set()
        worker.release_lease()
        self.assertIsNone(self.reserved())
        once = self.runner(once=True)
        once.stop.set()
        self.assertFalse(once.restart_pending())

    def test_la_reprise_consomme_la_reservation(self):
        handover_pg.reserve_restart(self.db, "banc", "pilote", 90.0)
        obj = self.runner()
        obj._clear_restart_reservation("pilote")
        self.assertIsNone(self.reserved())
        # Le chargeur des marques de bascule ne lit jamais la réservation.
        handover_pg.reserve_restart(self.db, "banc", "pilote", 90.0)
        self.assertEqual(handover_pg.load(self.db, "banc"), [])


class ReadingClockTest(_RoleCase):
    def test_releve_ecrit_pendant_la_lecture_reste_frais(self):
        self.role()
        self.ready_host("pilote", "banc")
        # Le `now` du lecteur est pris avant le relevé (une candidature entière).
        before = pg.now_ts(self.db)
        time.sleep(0.05)
        with self.db.transaction() as tx:
            time.sleep(0.05)
            tx.execute(
                "INSERT INTO host_resources (host, sampled_at, load1, cpu_count,"
                " mem_available_bytes, turns_in_progress)"
                " VALUES ('banc', clock_timestamp(), 0.1, 4, %s, 0)",
                (8 * 1024 ** 3,))
            reading = pg.latest_reading(tx, "banc")
            self.assertGreaterEqual(reading["now_ts"], reading["sampled_ts"])
            reason, _ = election._reading_reason(tx, "banc", now=before)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
