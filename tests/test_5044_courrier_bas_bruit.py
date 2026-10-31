# SPDX-License-Identifier: AGPL-3.0-only
"""#5044 : le courrier automatique sans geste attendu ne réveille pas.

Le sommeil automatique part en `info` ; une alerte SLO copiée à plusieurs
coordinateurs n'en réveille qu'un, sans qu'aucun perde le texte ; un P1 et une
pause opérateur ordinaire réveillent comme avant. Les témoins de la CI sont
dans `test_l161c_failures_realtime`.
"""
from __future__ import annotations

import unittest

from ameesh import alert_briefs, mail


def _row(name):
    return {"name": name, "team": "ameesh", "lease_live": True, "status": "idle",
            "lease_owner": "runner-" + name}


BY_NAME = {name: _row(name) for name in ("dev1", "orch", "mesh-design", "auditeur")}
SLEEP = {"type": "operator_pause_long", "agent": "dev1", "sleep": True, "detail": "dev1"}
SLO = {"type": "slo_breach", "agent": "dev1", "lot": 42, "level": 2, "targets": ["pilot"]}


class QuietPayloadsTest(unittest.TestCase):
    def test_sommeil_automatique_passif_pour_tous(self):
        for kind in ("operator_pause_long", "operator_pause_host"):
            quiet = alert_briefs.quiet_payloads(dict(SLEEP, type=kind),
                                                ["orch", "auditeur"], BY_NAME)
            self.assertEqual(quiet, {"orch": {"info": True}, "auditeur": {"info": True}})

    def test_pause_operateur_ordinaire_reveille(self):
        alert = {"type": "operator_pause_long", "agent": "dev1", "reason": "maintenance"}
        self.assertEqual(alert_briefs.quiet_payloads(alert, ["orch", "auditeur"], BY_NAME), {})

    def test_slo_un_seul_reveille_les_autres_en_copie(self):
        names = ["orch", "mesh-design", "auditeur"]
        quiet = alert_briefs.quiet_payloads(SLO, names, BY_NAME)
        woken = set(names) - set(quiet)
        self.assertEqual(len(woken), 1)
        for extra in quiet.values():
            self.assertEqual(extra, {"cc": woken.copy().pop()})
            self.assertEqual(mail.passive_reason({"payload": extra}), "cc")
        # stable d'une passe à l'autre : la relance réveille le même
        self.assertEqual(alert_briefs.quiet_payloads(SLO, names, BY_NAME), quiet)

    def test_slo_le_porteur_du_lot_est_celui_qui_reveille(self):
        quiet = alert_briefs.quiet_payloads(SLO, ["dev1", "orch"], BY_NAME)
        self.assertEqual(quiet, {"orch": {"cc": "dev1"}})

    def test_slo_seul_destinataire_ou_p1_reveillent(self):
        self.assertEqual(alert_briefs.quiet_payloads(SLO, ["orch"], BY_NAME), {})
        p1 = dict(SLO, urgent=True)
        self.assertEqual(alert_briefs.quiet_payloads(p1, ["orch", "auditeur"], BY_NAME), {})

    def test_le_fil_du_bureau_n_est_pas_un_destinataire(self):
        self.assertEqual(alert_briefs.quiet_payloads(SLO, ["thread:bureau", "orch"], BY_NAME), {})


if __name__ == "__main__":
    unittest.main()
