# SPDX-License-Identifier: AGPL-3.0-only
"""#5860 : un créneau de paquet passé sans prendre le lot.

Témoin du lot (#5811) : GO, puis un paquet parti sans lui ; l'alerte
`verdict_merge` montait au niveau 3 depuis le premier créneau, avec « merge
(executor not designated) » pour seule cause. L'horloge repart du dernier
créneau passé, l'alerte est au niveau 1 et nomme le créneau et la raison du
composeur (codes de `paquet-candidats`).
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import exploitation, packet_candidates, work

from .test_4572_slo_candidate_cause import _SloBase

HEURE = 3600.0


class _Cadence:
    """Un créneau par heure pleine (UTC), comme le repli horaire."""

    @staticmethod
    def next_packet(at):
        return (int(float(at) // HEURE) + 1) * HEURE


class MissedSlotsTest(unittest.TestCase):
    def missed(self, slot, now):
        with mock.patch.object(exploitation, "_packet_calendar",
                               return_value=_Cadence()):
            return exploitation._missed_packet_slots(
                None, slot, "ameesh", now=now, observed=False)

    def test_avant_le_creneau_suivant_rien_ne_change(self):
        self.assertEqual(self.missed(10 * HEURE, 10 * HEURE + 3000), (10 * HEURE, None))

    def test_repart_du_dernier_creneau_passe(self):
        start, missed = self.missed(10 * HEURE, 12 * HEURE + 60)
        self.assertEqual(start, 12 * HEURE)
        self.assertEqual(missed, {"first_slot": 10 * HEURE, "slot": 12 * HEURE,
                                  "count": 2})

    def test_borne(self):
        _, missed = self.missed(0.0, 1000 * HEURE)
        self.assertEqual(missed["count"], exploitation.MAX_MISSED_SLOTS)


class CreneauManqueTest(_SloBase):
    READY = {"code": "", "detail": "", "owner": "", "gesture": "",
             "ready": True, "unknown": False}

    def setUp(self):
        super().setUp()
        # GO à :20 ; le premier créneau est l'heure pleine suivante
        self.first = (int(self.GO // HEURE) + 1) * HEURE
        self.patch = mock.patch.object(exploitation, "_packet_calendar",
                                       return_value=_Cadence())
        self.patch.start()
        self.addCleanup(self.patch.stop)
        # un paquet ouvert qui ne porte pas le lot
        work.add(self.db, title="porteur", body="unités : #999999 @ cafebabe",
                 branch="integration/paquet-5860", actor="test")

    def alert_with(self, cause, at):
        with mock.patch.object(packet_candidates, "candidate_refusal",
                               return_value=dict(cause)):
            return self.alert(now=at)

    def test_creneau_passe_niveau_1_et_raison_nommee(self):
        # 50 min après le second créneau : depuis le premier, 110 min (×3 de
        # la cible de repli de 30 min) ; depuis le créneau manqué, niveau 1.
        alerts = self.alert_with(self.READY, self.first + HEURE + 50 * 60)
        self.assertEqual(len(alerts), 1, alerts)
        alert = alerts[0]
        self.assertLessEqual(alert["level"], 1)
        self.assertEqual(alert["packet_missed"]["count"], 1)
        self.assertEqual(alert["packet_missed"]["slot"], self.first + HEURE)
        self.assertIn("passed without it", alert["detail"])
        self.assertIn("ready candidate not taken", alert["detail"])
        self.assertIn("passed without it", alert["waiting"]["label"])
        self.assertNotIn("executor not designated", alert["waiting"]["label"])
        self.assertIn("pilot", alert["targets"])
        text = "\n".join(self.briefs(alert).values())
        self.assertIn("passed without the", text)

    def test_signale_des_le_creneau_manque(self):
        # 2 min après le second créneau : sous la cible, mais le manque se dit.
        alerts = self.alert_with(self.READY, self.first + HEURE + 120)
        self.assertEqual([a["level"] for a in alerts], [1])

    def test_cause_auteur_nommee(self):
        cause = {"code": "freeze-not-head", "owner": "author",
                 "detail": "freeze 1dd33c0e17c3 is not the published head 949e195be64c",
                 "gesture": "freeze again on the published head, then have it reviewed",
                 "ready": False, "unknown": False}
        alerts = self.alert_with(cause, self.first + HEURE + 120)
        self.assertEqual(len(alerts), 1, alerts)
        self.assertEqual(alerts[0]["targets"], ["assignee"])
        self.assertIn("`freeze-not-head`", alerts[0]["detail"])
        self.assertIn("freeze-not-head", alerts[0]["waiting"]["label"])

    def test_premier_creneau_inchange(self):
        # Pendant l'attente du premier créneau, rien de nouveau : l'horloge
        # part du créneau, aucun manque n'est nommé.
        alerts = self.alert_with(self.READY, self.first + 40 * 60)
        self.assertEqual(len(alerts), 1, alerts)
        self.assertNotIn("packet_missed", alerts[0])


if __name__ == "__main__":
    unittest.main()
