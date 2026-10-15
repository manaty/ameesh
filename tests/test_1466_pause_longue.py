# SPDX-License-Identifier: AGPL-3.0-only
"""#1466 : une pause budget qui dure est signalée.

Un agent resté `blocked` sur « budget : … » pendant plus d'une heure n'est
plus une pause ordinaire : c'est un plafond périmé, un compte retiré, ou une
configuration que l'exécuteur ne lit pas. L'alerte `budget_pause_long` le
rend visible (`ameesh alerts`), part en courrier aux orchestrateurs de son
équipe et aux auditeurs, et n'est pas poussée aux humains par défaut.
"""
from __future__ import annotations

import unittest

from ameesh import alert_briefs, exploitation, notify, registry

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

    def test_type_non_pousse_aux_humains_par_defaut(self):
        self.assertIn("budget_pause_long", notify.TYPE_LABELS)
        self.assertNotIn("budget_pause_long", notify.DEFAULT_TYPES)


if __name__ == "__main__":
    unittest.main()
