# SPDX-License-Identifier: AGPL-3.0-only
"""#5461 : après la relance de 6 h, une oscillation ne refait pas briefer.

La garde de #5203 (et de #2602/#3236) gardait l'entrée d'une alerte disparue
d'une passe tant que `now - sent_ts < REMIND_AFTER_S`, `sent_ts` étant l'heure
du brief initial. La relance de 6 h ne la déplaçait pas : la première
oscillation après la relance supprimait l'entrée, et le retour de l'alerte
repartait en brief neuf 16 à 22 min plus tard (02/11 : agents-3 à 04:07 puis
04:23, agents-1 et agents-2 à 05:18 puis 05:40).
"""
from __future__ import annotations

import unittest

from ameesh import alert_briefs

from . import test_5203_pause_host_brief as base

SIX_H = alert_briefs.REMIND_AFTER_S


class RelanceOscillationTest(base.PauseBriefTest):
    def test_l_oscillation_apres_la_relance_n_est_pas_rebriefee(self):
        self.run_briefs([self.host_alert(13)], now=10000)          # brief
        self.run_briefs([self.host_alert(13)], now=10000 + SIX_H + 1)  # relance
        self.run_briefs([], now=10000 + SIX_H + 600)               # sommeil reposé
        self.run_briefs([self.host_alert(11)], now=10000 + SIX_H + 1200)
        # le brief et sa relance, rien de plus
        self.assertEqual(self.recipients(), ["auditeur", "auditeur", "pilot", "pilot"])

    def test_une_pause_longue_relancee_puis_oscillante_n_est_pas_rebriefee(self):
        alert = dict(type="operator_pause_long", agent="dev", since=5000,
                     value=4000, threshold=3600, host="ameesh-agents-1",
                     orchestrators=["pilot"], detail="dev under operator pause")
        self.run_briefs([alert], now=10000)
        self.run_briefs([alert], now=10000 + SIX_H + 1)
        self.run_briefs([], now=10000 + SIX_H + 600)
        self.run_briefs([alert], now=10000 + SIX_H + 1200)
        self.assertEqual(self.recipients(), ["auditeur", "auditeur", "pilot", "pilot"])

    def test_six_heures_apres_la_relance_un_retour_est_de_nouveau_briefe(self):
        self.run_briefs([self.host_alert()], now=10000)
        self.run_briefs([self.host_alert()], now=10000 + SIX_H + 1)   # relance
        later = 10000 + 2 * SIX_H + 2
        self.run_briefs([], now=later)                  # absente 6 h après la relance
        self.run_briefs([self.host_alert()], now=later + 60)
        self.assertEqual(self.recipients(),
                         ["auditeur", "auditeur", "auditeur", "pilot", "pilot", "pilot"])


if __name__ == "__main__":
    unittest.main()
