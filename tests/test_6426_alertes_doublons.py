# SPDX-License-Identifier: AGPL-3.0-only
"""#6426 : un biais d'estimation ne briefe plus l'auditeur ni le pilote.

Analyse de nuit du 09/11 : `estimate_bias` partait à l'estimateur, aux
orchestrateurs de son équipe et à l'auditeur — trois courriers et trois réveils
pour une correction qui ne revient qu'à l'estimateur. Le pilote a écrit qu'il
n'avait aucun geste (mesh-design, 09/11 01:03) et l'auditeur lit les biais à sa
passe (`ameesh alerts --json`, docs/AUDITEUR.md §11). L'estimateur reste le
seul destinataire ; hors registre, le repli auditeur de #3502 demeure. Les
rappels de sommeil automatique ne partent déjà plus (#6256, test dédié).
"""
from __future__ import annotations

import unittest

from ameesh import alert_briefs


def _listing(*rows):
    return [dict(name=n, team=t) for n, t in rows]


BIAS = {"type": "estimate_bias", "agent": "dev1", "value": 2.9,
        "detail": "estimates of dev1: median actual/estimate ×2.9",
        "orchestrators": ["orch"], "reason": "under-1"}


class EstimateBiasRecipientsTest(unittest.TestCase):
    def setUp(self):
        self.listing = _listing(("orch", "a"), ("dev1", "a"), ("auditeur", ""))

    def test_un_estimateur_au_registre_recoit_seul_le_biais(self):
        briefs = alert_briefs.briefs(BIAS, self.listing,
                                     auditor_names=["auditeur"])
        self.assertEqual(sorted(briefs), ["dev1"])
        self.assertIn("Estimate bias", briefs["dev1"])

    def test_un_estimateur_hors_registre_garde_le_repli_auditeur(self):
        alert = dict(BIAS, agent="human:smichea")
        briefs = alert_briefs.briefs(alert, self.listing,
                                     auditor_names=["auditeur"])
        self.assertEqual(sorted(briefs), ["auditeur"])
        self.assertIn("Estimate bias", briefs["auditeur"])

    def test_un_estimateur_qui_est_aussi_auditeur_ne_double_pas(self):
        alert = dict(BIAS, agent="auditeur")
        briefs = alert_briefs.briefs(alert, self.listing,
                                     auditor_names=["auditeur"])
        self.assertEqual(sorted(briefs), ["auditeur"])

    def test_les_autres_alertes_gardent_l_auditeur(self):
        # Non-régression : seul `estimate_bias` est dédoublonné ici.
        idle = {"type": "idle_capacity", "agents": ["dev1"], "lots": [],
                "orchestrators": ["orch"], "threshold": 1800.0,
                "detail": "1 agent au repos"}
        briefs = alert_briefs.briefs(idle, self.listing,
                                     auditor_names=["auditeur"])
        self.assertEqual(sorted(briefs), ["auditeur", "orch"])


if __name__ == "__main__":
    unittest.main()
