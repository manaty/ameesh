# SPDX-License-Identifier: AGPL-3.0-only
"""#4112 : la réservation @restart (#4086) ne se perd plus sur un relevé périmé.

Un exécuteur relancé lit souvent un relevé « non prêt » d'avant son
redémarrage (pause du drain de `vm.sh`, publié par l'ancien processus il y a
moins d'une minute). Il attendait le relevé suivant, 60 s plus tard, et la
réservation de 90 s expirait : le rang 0 d'un autre hôte reprenait le rôle.
Désormais, un rôle réservé à cet hôte et lu « non prêt » fait republier le
relevé tout de suite (au plus toutes les `RESERVED_REPUBLISH_S`) ; la
readiness reste la garde.
"""
from __future__ import annotations

from unittest import mock

from ameesh import election, role_handover_pg as handover_pg
from ameesh import role_standings_pg as pg, runner

from .test_l165_ep2_election_score import _RoleCase


class ReservationAvantPretTest(_RoleCase):
    def setUp(self) -> None:
        super().setUp()
        self.role_row = self.role()
        self.ready_pair()
        # `atelier` est le mieux classé (rang 0) ; `banc` détenait le rôle.
        self.standing("pilote", "atelier", 99.0)

    def runner(self):
        obj = runner.Runner(self.cfg, self.db)
        self.addCleanup(obj.stop.set)
        return obj

    def pause(self, on: bool) -> None:
        self.db.execute(
            "UPDATE agent_registry SET operator_paused = %s,"
            " operator_pause_reason = %s WHERE name = 'pilote'",
            (on, "drain vm.sh" if on else None))

    def reserve(self, host: str = "banc") -> None:
        self.hold("pilote", host, epoch=7)
        self.db.execute("UPDATE agent_registry SET lease_owner = 'r' WHERE name = 'pilote'")
        self.assertTrue(handover_pg.reserve_restart(
            self.db, host, "pilote", election.RESTART_RESERVE_S, owner="r", epoch=7))
        self.db.execute("UPDATE agent_registry SET lease_owner = NULL,"
                        " lease_expires_at = NULL WHERE name = 'pilote'")

    def stale_standing(self) -> None:
        """Le relevé de l'ancien processus, sous la pause du drain."""
        self.pause(True)
        self.assertEqual(election.publish_self(self.cfg, self.db, "banc",
                                               force=True), 1)
        self.assertFalse(pg.latest(self.db, "pilote")["banc"]["ready"])

    def test_pause_levee_le_role_reste_a_l_hote_reserve(self):
        self.reserve()
        self.stale_standing()
        obj = self.runner()
        # Pause encore posée : non prêt, même relu à jour.
        allowed, reason, _ = obj.election_claim_allowed(self.role_row)
        self.assertFalse(allowed)
        self.assertIn("operator pause", reason)
        # `vm.sh` lève la pause ; le relevé de 60 s n'est pas encore dû.
        self.pause(False)
        obj._reserved_republish_next.clear()  # 5 s plus tard
        allowed, reason, mine = obj.election_claim_allowed(self.role_row)
        self.assertTrue(allowed, reason)
        self.assertTrue(mine["ready"])
        self.assertTrue(pg.latest(self.db, "pilote")["banc"]["ready"])
        # L'autre hôte, rang 0, attend toujours la réservation.
        allowed, reason, _ = election.claim_wait(
            self.db, self.role_row, "atelier", 0.0, now=1000.0)
        self.assertFalse(allowed)
        self.assertIn("restart reservation", reason)

    def rows(self) -> int:
        return self.db.query("SELECT count(*) AS n FROM role_standings"
                             " WHERE name = 'pilote' AND host = 'banc'")[0]["n"]

    def test_republication_bornee(self):
        self.reserve()
        self.stale_standing()
        obj = self.runner()
        obj.clock = mock.Mock(monotonic=lambda: 1000.0)  # même instant
        obj.election_claim_allowed(self.role_row)
        first = self.rows()
        for _ in range(5):
            obj.election_claim_allowed(self.role_row)
        self.assertEqual(self.rows(), first)

    def test_sans_reservation_aucune_republication(self):
        self.stale_standing()
        self.pause(False)
        obj = self.runner()
        allowed, reason, _ = obj.election_claim_allowed(self.role_row)
        self.assertFalse(allowed)
        self.assertTrue(reason.startswith("not ready"), reason)
        self.assertFalse(pg.latest(self.db, "pilote")["banc"]["ready"])

    def test_reservation_d_un_autre_hote_aucune_republication(self):
        self.reserve("atelier")
        self.stale_standing()
        self.pause(False)
        obj = self.runner()
        allowed, _, _ = obj.election_claim_allowed(self.role_row)
        self.assertFalse(allowed)
        self.assertFalse(pg.latest(self.db, "pilote")["banc"]["ready"])
