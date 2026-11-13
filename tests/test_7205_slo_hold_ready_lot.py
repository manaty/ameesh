# SPDX-License-Identifier: AGPL-3.0-only
"""#7205 — l'accusé d'une alerte SLO sur un lot prêt n'est pas un `hold:`.

Le 13/11 (06:3x–06:57Z), le pilote a accusé les alertes « review GO → merge »
de cinq lots validés par des notes `hold: …`. `scripts/paquet-candidats.py`
lit tout hold actif d'un pilote comme une retenue (`pilot-hold`) : les cinq
lots sont sortis de h-06, environ une heure perdue sur un P1 (#7023). Le brief
d'un lot prêt dit maintenant d'accuser par une note simple, et la CLI avertit
quand un `hold:` est écrit sur un lot validé.
"""
from __future__ import annotations

import unittest

from ameesh import alert_briefs, planned_waits, work

from .support import PgTestCase


def _alert(**waiting):
    return {"type": "slo_breach", "lot": 7023, "title": "comptes d'organisation",
            "step": "verdict_merge", "step_label": "review GO → merge",
            "level": 1, "value": 1800.0, "threshold": 1800.0,
            "responsible": "mty-amh-impl-13",
            "waiting": {"what": "merge", **waiting}}


class ReadyLotBriefTest(unittest.TestCase):
    """Le brief d'un lot prêt nomme l'effet d'un `hold:` sur le paquet."""

    def test_lot_pret_niveau_1_et_3(self):
        for level in (1, 3):
            with self.subTest(level=level):
                alert = _alert()
                alert["level"] = level
                text = alert_briefs._slo_breach(alert)
                self.assertIn("next package", text)
                self.assertIn(alert_briefs.READY_HOLD_HINT, text)
                self.assertIn("pilot-hold", text)

    def test_creneau_manque_et_sortie_de_vehicule(self):
        for extra in ({"packet_missed": {"slot": 7 * 3600.0, "count": 1}},
                      {"vehicle_exit_ts": 7 * 3600.0}):
            with self.subTest(extra=extra):
                alert = _alert()
                alert.update(extra)
                self.assertIn(alert_briefs.READY_HOLD_HINT,
                              alert_briefs._slo_breach(alert))

    def test_cause_auteur_canon_ou_illisible_sans_la_mention(self):
        # Le composeur ne peut pas porter ces lots : la mention n'a pas d'objet.
        for waiting in ({"candidate": {"code": "freeze-not-head", "owner": "author",
                                       "gesture": "regeler"}},
                        {"canon": True},
                        {"candidate": {"unknown": True}}):
            with self.subTest(waiting=waiting):
                text = alert_briefs._slo_breach(_alert(**waiting))
                self.assertNotIn(alert_briefs.READY_HOLD_HINT, text)


class ReadyHoldWarningTest(PgTestCase):
    """La note est écrite ; la CLI dit qu'elle écarte le lot du paquet."""

    def setUp(self):
        super().setUp()
        self.register("dev-a", "claude")
        self.lot = work.add(self.db, title="lot relu", assignee="dev-a")

    def _state(self, state):
        self.db.execute("UPDATE work_items SET state = %s WHERE id = %s",
                        (state, self.lot["id"]))

    def _warning(self, text):
        return planned_waits.ready_hold_warning(self.db, self.lot["id"], text)

    def test_hold_sur_lot_valide_avertit(self):
        self._state("validated")
        warning = self._warning("hold: alerte SLO accusée, paquet suivant")
        self.assertIn("pilot-hold", warning)
        self.assertIn("next package", warning)
        self.assertIn("unhold: <reason>", warning)
        self.assertIn("#7205", warning)

    def test_note_simple_ou_levee_sans_avertissement(self):
        self._state("validated")
        for text in ("Alerte SLO accusée : paquet suivant.",
                     "unhold: paquet suivant", ""):
            with self.subTest(text=text):
                self.assertEqual(self._warning(text), "")

    def test_hold_hors_lot_valide_sans_avertissement(self):
        for state in ("intake", "build", "qa"):
            with self.subTest(state=state):
                self._state(state)
                self.assertEqual(self._warning("hold: attente"), "")


if __name__ == "__main__":
    unittest.main()
