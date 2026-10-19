# SPDX-License-Identifier: AGPL-3.0-only
"""#2771 — `orphan_lot` se tait sur un lot sous retenue de pilote.

Un lot dont un pilote a écrit `hold: <raison>` (#1664) attend un geste que son
détenteur ne peut pas faire (owner, bureau, paquet) : il n'est pas `sans_tour`.
Même lecture que `stale_lot` et le SLO (`planned_waits.pilot_holds`) : une
levée (`unhold:`) ou un changement d'état la finit, et `arrete` reste vrai
sous retenue. La retenue de l'assigné lui-même (#2764) tient 24 h, puis
l'alerte revient en la citant ; celle d'un tiers ordinaire ne compte pas.
"""
from __future__ import annotations

import unittest

from ameesh import exploitation, registry, work

from .support import PgTestCase


class OrphanLotHoldTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register("dort", "claude", cwd=self.tmp)
        self.lot = work.add(self.db, title="attend l'owner", assignee="dort")

    def _alerts(self):
        return [(a["lot"], a["reason"])
                for a in exploitation.alerts(self.cfg, self.db)
                if a["type"] == "orphan_lot"]

    def _vieillir(self, hours=2):
        for sql in (
            "UPDATE work_items SET created_at = now() - interval '%d hours',"
            " updated_at = now() - interval '%d hours'" % (hours, hours),
            "UPDATE work_item_events SET created_at = now() - interval '%d hours'" % hours,
            "UPDATE work_item_milestones SET at = now() - interval '%d hours'" % hours,
        ):
            self.db.execute(sql)

    def _note(self, text, actor, *, hours=2):
        """Une note du journal, datée (le chemin d'écriture authentifie le
        pilote, #1664 r2 ; ici seule la lecture est en jeu)."""
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor, created_at)"
            " SELECT id, state, %s, %s, now() - make_interval(hours => %s)"
            "   FROM work_items WHERE id = %s",
            (text, actor, hours, self.lot["id"]))

    def test_retenue_de_pilote_eteint_sans_tour(self):
        self._vieillir()
        self.assertEqual(self._alerts(), [(self.lot["id"], "sans_tour")])
        self._note("hold: gestes owner puis go", "human:proprio")
        self.assertEqual(self._alerts(), [])

    def test_retenue_d_un_tiers_ordinaire_ne_compte_pas(self):
        self.register("voisin", "claude", cwd=self.tmp)
        self._vieillir()
        self._note("hold: j'attends le paquet", "voisin")
        self.assertEqual(self._alerts(), [(self.lot["id"], "sans_tour")])

    def test_retenue_de_l_assigne_tient_jusqu_a_son_terme(self):
        """Cas #2764 : l'assigné écrit lui-même `hold:` (consigne du réveil)."""
        self._vieillir()
        self._note("hold: j'attends la fusion du paquet 99", "dort")
        self.assertEqual(self._alerts(), [])

    def test_retenue_de_l_assigne_echue_est_citee(self):
        self._vieillir(30)
        self._note("hold: j'attends la fusion du paquet 99", "dort", hours=25)
        alerts = [a for a in exploitation.alerts(self.cfg, self.db)
                  if a["type"] == "orphan_lot"]
        self.assertEqual([(a["lot"], a["reason"]) for a in alerts],
                         [(self.lot["id"], "sans_tour")])
        self.assertIn("hold of", alerts[0]["detail"])
        self.assertIn("past the 24h term", alerts[0]["detail"])

    def test_levee_rend_l_alerte(self):
        self._vieillir(3)
        self._note("hold: gestes owner", "human:proprio", hours=3)
        self.assertEqual(self._alerts(), [])
        self._note("unhold: gestes faits", "human:proprio", hours=2)
        self.assertEqual(self._alerts(), [(self.lot["id"], "sans_tour")])

    def test_arrete_reste_vrai_sous_retenue(self):
        self._vieillir()
        self._note("hold: gestes owner", "human:proprio")
        registry.set_status(self.db, "dort", "stopped", status_text="arrêté",
                            stop_reason="manuel")
        self.assertEqual(self._alerts(), [(self.lot["id"], "arrete")])


if __name__ == "__main__":
    unittest.main()
