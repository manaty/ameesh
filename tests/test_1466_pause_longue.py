# SPDX-License-Identifier: AGPL-3.0-only
"""#1466 : une pause budget qui dure est signalée.

Un agent resté `blocked` sur « budget : … » pendant plus d'une heure n'est
plus une pause ordinaire : c'est un plafond périmé, un compte retiré, ou une
configuration que l'exécuteur ne lit pas. L'alerte `budget_pause_long` le
rend visible (`ameesh alerts`), part en courrier aux orchestrateurs de son
équipe et aux auditeurs, et n'est pas poussée aux humains par défaut.
"""
from __future__ import annotations

import time
import types
import unittest

from ameesh import accounts, alert_briefs, exploitation, notify, registry

from .support import PgTestCase


class PauseLongueTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register("ouvrier", "deepseek", cwd=self.tmp)

    def _pause(self, texte, minutes):
        registry.set_status(self.db, "ouvrier", "blocked", status_text=texte,
                            error="budget")
        self.db.execute(
            "UPDATE agent_registry SET status_since = now() - make_interval(mins => %s)"
            " WHERE name = 'ouvrier'", (int(minutes),))

    def _alertes(self, **kw):
        return [a for a in exploitation.alerts(self.cfg, self.db, **kw)
                if a["type"] == "budget_pause_long"]

    def test_pause_courte_non_signalee(self):
        self._pause("budget : pause (account secondaire, five_hour 96 %) until 14:20; …", 5)
        self.assertEqual(self._alertes(), [])

    def test_pause_d_une_heure_signalee(self):
        self._pause("budget : pause (account secondaire, five_hour 96 %) until 14:20; …", 61)
        alertes = self._alertes()
        self.assertEqual(len(alertes), 1)
        alerte = alertes[0]
        self.assertEqual(alerte["agent"], "ouvrier")
        self.assertEqual(alerte["threshold"], exploitation.DEFAULT_LONG_BUDGET_PAUSE_S)
        self.assertGreaterEqual(alerte["value"], 3600)
        # le détail nomme les gestes : plafonds, comptes, configuration lue
        self.assertIn("ameesh budget", alerte["detail"])
        self.assertIn("ameesh accounts list", alerte["detail"])
        self.assertIn("ameesh doctor", alerte["detail"])
        # l'alerte critique garde son propre type, et la pause longue est
        # adressée aux orchestrateurs (alert_briefs)
        self.assertIn("orchestrators", alerte)

    def test_seuil_reglable_et_zero_desactive(self):
        self._pause("budget : pause (…) until 14:20", 61)
        self.assertEqual(self._alertes(long_budget_pause_s=7200), [])
        self.assertEqual(self._alertes(long_budget_pause_s=0), [])
        self.assertEqual(len(self._alertes(long_budget_pause_s=3600)), 1)

    def test_une_pause_critique_garde_son_alerte(self):
        self._pause("budget : critical agent: pause (account primaire, five_hour 96 %) "
                    "until 14:20", 61)
        types = [a["type"] for a in exploitation.alerts(self.cfg, self.db)]
        self.assertIn("critical_budget_pause", types)
        self.assertIn("budget_pause_long", types)

    def test_courrier_aux_orchestrateurs_et_auditeurs(self):
        alerte = {"type": "budget_pause_long", "agent": "ouvrier",
                  "orchestrators": ["orch"],
                  "detail": "paused on budget for 4200s: pause (account x) until 14:20"}
        listing = [{"name": "ouvrier", "team": "ameesh"},
                   {"name": "orch", "team": "ameesh"},
                   {"name": "auditeur", "team": "ameesh"}]
        out = alert_briefs.briefs(alerte, listing, auditor_names=["auditeur"],
                                  orchestrator_names=("orch",))
        self.assertIn("orch", out)
        self.assertIn("auditeur", out)
        self.assertNotIn("ouvrier", out)      # en pause : il ne lirait pas
        self.assertIn("ameesh doctor", out["orch"])

    def _dated(self, delta_s):
        """#4781 : le texte réel de `pause_label`, échéance à `delta_s`."""
        ev = types.SimpleNamespace(
            until=time.time() + delta_s, held=("seven_day", 0.23),
            profile=types.SimpleNamespace(name="primaire"))
        return "budget : " + accounts.pause_label({"primaire": ev}) + "claude paused"

    def test_regulation_a_echeance_proche_sans_alerte(self):
        self._pause(self._dated(3600), 61)
        self.assertEqual(self._alertes(), [])

    def test_echeance_a_peine_depassee_sans_alerte(self):
        # 30/10 : alerte à 02:40:47 pour « until 02:38 », agent repris
        self._pause(self._dated(-170), 61)
        self.assertEqual(self._alertes(), [])

    def test_echeance_depassee_signalee(self):
        self._pause(self._dated(-600), 61)
        self.assertEqual(len(self._alertes()), 1)

    def test_echeance_lointaine_signalee(self):
        self._pause(self._dated(4 * 86400), 61)
        self.assertEqual(len(self._alertes()), 1)

    def test_echeance_qui_recule_signalee_a_24_h(self):
        self._pause(self._dated(3600), 24 * 60 + 1)
        self.assertEqual(len(self._alertes()), 1)

    def test_pause_operateur_sans_alerte(self):
        # 30/10, ameesh-cx1 : sommeil de feuille de route, motif de budget resté
        # (#5301 : un motif de fenêtre courte ne s'alerte plus de lui-même ;
        # le témoin garde une pause de jauge sans échéance lisible)
        self._pause("budget : pause (account primaire, seven_day 99 %); …", 61)
        self.assertEqual(len(self._alertes()), 1)
        self.db.execute("UPDATE agent_registry SET operator_paused = true"
                        " WHERE name = 'ouvrier'")
        self.assertEqual(self._alertes(), [])

    def test_le_marqueur_ne_change_pas_la_nature_de_la_pause(self):
        texte = self._dated(3600)[len("budget : "):]
        self.assertNotIn("@", accounts.pause_nature(texte))
        self.assertEqual(accounts.pause_nature(texte).split("|")[:3],
                         ["pause", "primaire", "seven_day"])

    def test_type_non_pousse_aux_humains_par_defaut(self):
        self.assertIn("budget_pause_long", notify.TYPE_LABELS)
        self.assertNotIn("budget_pause_long", notify.DEFAULT_TYPES)


if __name__ == "__main__":
    unittest.main()
