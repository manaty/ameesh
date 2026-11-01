# SPDX-License-Identifier: AGPL-3.0-only
"""#5301 : la fenêtre courte (#3779) n'est pas une pause budget qui dure.

Le 01/11 à 10:08Z, `budget_pause_long` signalait « ameesh-cx2 paused on
budget for 4942s … account primaire: next turn in 23s (start gap 30s between
turns, #3779; last start 10:06) » : l'agent était inactif (sommeil de feuille
de route épuisée), sans aucun tour à lancer ; le motif d'écart de départ
restait seul dans son statut. Plafonds et comptes étaient sains. Une pause de
jauge sans échéance lisible reste, elle, signalée (#1466).
"""
from __future__ import annotations

import unittest

from ameesh import exploitation, registry

from .support import PgTestCase

ECART = ("budget : account primaire: next turn in 23s (start gap 30s between "
         "turns, #3779; last start 10:06)")


class PauseFenetreCourteTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register("ouvrier", "codex", cwd=self.tmp)

    def _pause(self, texte, minutes):
        registry.set_status(self.db, "ouvrier", "blocked", status_text=texte,
                            error="budget")
        self.db.execute(
            "UPDATE agent_registry SET status_since = now() - make_interval(mins => %s)"
            " WHERE name = 'ouvrier'", (int(minutes),))

    def _alertes(self):
        return [a for a in exploitation.alerts(self.cfg, self.db)
                if a["type"] == "budget_pause_long"]

    def test_ecart_de_depart_d_un_agent_inactif_sans_alerte(self):
        self._pause(ECART, 82)
        self.assertEqual(self._alertes(), [])

    def test_cooldown_et_plafond_de_tours_sans_alerte(self):
        for texte in ("budget : account primaire: short-window cooldown until 10:40",
                      "budget : account primaire: 2/2 concurrent turns (provider "
                      "burst bucket shared by sessions, #3779)"):
            with self.subTest(texte=texte):
                self._pause(texte, 82)
                self.assertEqual(self._alertes(), [])

    def test_marque_critique_sur_la_fenetre_courte_sans_alerte(self):
        self._pause("budget : critical agent: " + ECART[len("budget : "):], 82)
        self.assertEqual(self._alertes(), [])

    def test_une_pause_de_jauge_meme_duree_reste_signalee(self):
        self._pause("budget : pause (account primaire, codex-10080min 99 %); …", 82)
        alertes = self._alertes()
        self.assertEqual(len(alertes), 1)
        self.assertEqual(alertes[0]["agent"], "ouvrier")


if __name__ == "__main__":
    unittest.main()
