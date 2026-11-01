# SPDX-License-Identifier: AGPL-3.0-only
"""#5203 : une pause d'hôte qui disparaît d'une passe ne refait pas briefer.

Le sommeil automatique (#3256 §2.3), levé par un lot démarrable puis reposé,
remet à zéro la tenue de 15 min de tous les dormeurs : `operator_pause_host`
disparaît sur chaque hôte puis revient, et chaque retour envoyait un nouveau
« Automatic sleep still in place » (261 courriers en 24 h, analyse profonde
du 01/11). La garde de #2602/#3236 vaut désormais aussi pour ces pauses.
"""
from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import alert_briefs, mail, notify, storage


class PauseBriefTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = SimpleNamespace(canon=(), state_dir=self.tmp.name)
        self.store = mock.Mock()
        self.listing = [dict(name=n, team="ameesh", mode="execute")
                        for n in ("dev", "pilot", "auditeur")]
        self.store.operations.listing.side_effect = lambda: self.listing
        patch = mock.patch.object(storage, "of", return_value=self.store)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(mail, "send", return_value=1)
        self.send = patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(notify.fil, "record", return_value="thread-id")
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(alert_briefs, "briefs",
                                  return_value={"pilot": "sleep", "auditeur": "sleep"})
        patch.start()
        self.addCleanup(patch.stop)
        self.notifier = notify.Notifier(self.cfg, notify.parse_config({"default": []}),
                                        sender=object(), log=lambda text: None)

    def run_briefs(self, alerts, now):
        self.notifier._brief_agents(object(), alerts, None, now)

    def recipients(self):
        return sorted(call.args[2] for call in self.send.call_args_list)

    def host_alert(self, tenus=13):
        return dict(type="operator_pause_host", agent=None, since=None,
                    value=tenus, threshold=16, host="ameesh-agents-1",
                    agents=["dev"], sleep=True, orchestrators=["pilot"],
                    detail="%d/16 agents of host ameesh-agents-1 in automatic sleep" % tenus)

    def test_une_pause_d_hote_qui_revient_n_est_pas_rebriefee(self):
        self.run_briefs([self.host_alert(13)], now=10000)
        self.run_briefs([], now=11200)                  # sommeil reposé : tenue < 15 min
        self.run_briefs([self.host_alert(12)], now=12100)
        self.assertEqual(self.recipients(), ["auditeur", "pilot"])

    def test_une_pause_longue_qui_revient_n_est_pas_rebriefee(self):
        alert = dict(type="operator_pause_long", agent="dev", since=5000,
                     value=4000, threshold=3600, host="ameesh-agents-1",
                     orchestrators=["pilot"], detail="dev under operator pause")
        self.run_briefs([alert], now=10000)
        self.run_briefs([], now=11000)
        self.run_briefs([alert], now=12000)
        self.assertEqual(self.recipients(), ["auditeur", "pilot"])

    def test_apres_la_relance_un_retour_est_de_nouveau_briefe(self):
        self.run_briefs([self.host_alert()], now=10000)
        later = 10000 + alert_briefs.REMIND_AFTER_S + 1
        self.run_briefs([], now=later)                  # absente au-delà de 6 h
        self.run_briefs([self.host_alert()], now=later + 60)
        self.assertEqual(self.recipients(), ["auditeur", "auditeur", "pilot", "pilot"])


if __name__ == "__main__":
    unittest.main()
