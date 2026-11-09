# SPDX-License-Identifier: AGPL-3.0-only
"""#6556 : un disque stable au seuil ne refait pas briefer à chaque retour.

AG1 /home/ameesh/development oscillait entre 20,0 et 20,1 GiB libres (85 %),
sans tendance : `host_disk_soon_full` quittait une passe puis revenait, et
chaque retour refaisait un brief neuf à mesh-design et au bureau (#205149,
#205160, #205212, 09/11 15:20-15:32Z). La garde de #3236/#5203/#5461 vaut
désormais aussi pour l'avertissement non critique ; le passage au critique
reste un brief neuf immédiat (#5563).
"""
from __future__ import annotations

import unittest

from ameesh import alert_briefs

from . import test_5203_pause_host_brief as base

SIX_H = alert_briefs.REMIND_AFTER_S


def disk_alert(critical=False, free_mib=20517):
    return dict(type="host_disk_soon_full", agent=None, since=None,
                value=1, threshold=1, host="ameesh-agents-1",
                critical=critical, orchestrators=["pilot"],
                detail="ameesh-agents-1 /home/ameesh/development: %d MiB free (85 %%)"
                % free_mib)


class DiskBriefOscillationTest(base.PauseBriefTest):
    def test_un_disque_qui_oscille_au_seuil_ne_fait_qu_un_brief(self):
        self.run_briefs([disk_alert()], now=10000)
        for step in range(1, 30):                       # 6 h moins un peu
            now = 10000 + step * 600
            self.run_briefs([] if step % 2 else [disk_alert(free_mib=20671)], now=now)
        self.assertEqual(self.recipients(), ["auditeur", "pilot"])

    def test_la_relance_de_six_heures_part(self):
        self.run_briefs([disk_alert()], now=10000)
        self.run_briefs([], now=10600)
        self.run_briefs([disk_alert()], now=11200)
        self.run_briefs([disk_alert()], now=10000 + SIX_H + 1)   # relance
        self.run_briefs([], now=10000 + SIX_H + 600)              # oscillation
        self.run_briefs([disk_alert()], now=10000 + SIX_H + 1200)
        self.assertEqual(self.recipients(), ["auditeur", "auditeur", "pilot", "pilot"])

    def test_le_passage_au_critique_part_tout_de_suite(self):
        self.run_briefs([disk_alert()], now=10000)
        self.run_briefs([], now=10600)
        self.run_briefs([disk_alert(critical=True)], now=10660)
        self.assertEqual(self.recipients(), ["auditeur", "auditeur", "pilot", "pilot"])

    def test_un_retour_apres_six_heures_d_absence_est_rebriefe(self):
        self.run_briefs([disk_alert()], now=10000)
        later = 10000 + SIX_H + 1
        self.run_briefs([], now=later)
        self.run_briefs([disk_alert()], now=later + 60)
        self.assertEqual(self.recipients(), ["auditeur", "auditeur", "pilot", "pilot"])


if __name__ == "__main__":
    unittest.main()
