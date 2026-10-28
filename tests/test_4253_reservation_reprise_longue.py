# SPDX-License-Identifier: AGPL-3.0-only
"""#4253 : la réservation @restart couvre une reprise de plusieurs minutes.

Le 28/10, l'exécuteur de mesh-design arrêté à 13:21:11Z n'a repris son bail
qu'à 13:24:11Z (179 s), au-delà des 90 s de #4086 : la réservation expirait
avant le retour de l'hôte et le rang 0 d'un autre hôte pouvait prendre le
rôle (session neuve). Sur les sept jours d'événements, la plus longue reprise
d'un hôte toujours allumé est de 195 s. La réservation est portée à 240 s :
elle couvre cette reprise, et 240 s + le délai du premier challenger restent
sous les cinq minutes de relève du §11.
"""
from __future__ import annotations

import unittest

from ameesh import election, role_handover_pg as handover_pg

from .test_l165_ep2_election_score import _RoleCase


class ReservationRepriseLongueTest(_RoleCase):
    def setUp(self) -> None:
        super().setUp()
        self.role_row = self.role()
        self.ready_pair()
        # `atelier` est le mieux classé (rang 0) ; `banc` détenait le rôle.
        self.standing("pilote", "atelier", 99.0)
        self.standing("pilote", "banc", 90.0)

    def reserve_banc(self) -> None:
        self.hold("pilote", "banc", epoch=7)
        self.db.execute("UPDATE agent_registry SET lease_owner = 'r'"
                        " WHERE name = 'pilote'")
        self.assertTrue(handover_pg.reserve_restart(
            self.db, "banc", "pilote", election.RESTART_RESERVE_S,
            owner="r", epoch=7))
        self.db.execute("UPDATE agent_registry SET lease_owner = NULL,"
                        " lease_expires_at = NULL WHERE name = 'pilote'")

    def test_reprise_de_195_s_mesuree_reste_reservee_a_l_hote(self):
        self.reserve_banc()
        # La plus longue reprise mesurée sur un hôte toujours allumé est de
        # 195 s (28/10 : 179 s) : la réservation courante doit encore tenir à
        # cet instant (elle expirerait avec l'ancien délai).
        self.db.execute(
            "UPDATE role_handover"
            " SET released_until = released_until - interval '195 seconds'"
            " WHERE name = 'pilote' AND challenger = %s",
            (handover_pg.RESTART,))
        self.assertIsNotNone(handover_pg.restart_reservation(self.db, "pilote"))
        # L'hôte réservé reprend sans délai de rang ; l'autre attend encore.
        allowed, reason, _ = election.claim_wait(
            self.db, self.role_row, "banc", 1000.0, now=1000.0)
        self.assertTrue(allowed, reason)
        allowed, reason, _ = election.claim_wait(
            self.db, self.role_row, "atelier", 1000.0, now=1000.0)
        self.assertFalse(allowed)
        self.assertIn("restart reservation", reason)

    def test_hote_qui_ne_revient_pas_rend_la_main_a_l_echeance(self):
        self.reserve_banc()
        self.db.execute(
            "UPDATE role_handover"
            " SET released_until = clock_timestamp() - interval '1 second'"
            " WHERE name = 'pilote' AND challenger = %s",
            (handover_pg.RESTART,))
        self.assertIsNone(handover_pg.restart_reservation(self.db, "pilote"))
        allowed, reason, _ = election.claim_wait(
            self.db, self.role_row, "atelier", 1000.0, now=1000.0)
        self.assertTrue(allowed, reason)

    def test_la_releve_reste_sous_cinq_minutes(self):
        # Deux hôtes candidats : le challenger est au rang 1 (20 s) ; trois
        # hôtes le mettraient au rang 2 (40 s). Les deux restent sous 300 s.
        self.assertLess(election.RESTART_RESERVE_S
                        + election.rank_delay_s(1), 300.0)
        self.assertLess(election.RESTART_RESERVE_S
                        + election.rank_delay_s(2), 300.0)


if __name__ == "__main__":
    unittest.main()
