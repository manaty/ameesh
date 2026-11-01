# SPDX-License-Identifier: AGPL-3.0-only
"""#5331 : le sommeil automatique reposé ne refait pas briefer.

Le sommeil automatique (#3256 §2.3) est levé par un lot démarrable puis reposé
(redémarrage de staging, cycle de sommeil) : chaque épisode porte un `since`
neuf, donc une clé de brief neuve, et la garde de 6 h de #5203 ne le retenait
pas. La clé d'un brief de sommeil automatique ignore désormais le début
d'épisode (agent, hôte et projet de la raison) ; une vraie pause opérateur
garde son épisode et reste briefée.
"""
from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import alert_briefs, file_decoupage, mail, notify, storage


class SleepBriefKeyTest(unittest.TestCase):
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

    def pause_alert(self, since, *, sleep=True, agent="dev"):
        return dict(type="operator_pause_long", agent=agent, since=since,
                    value=4000, threshold=3600, host="ameesh-agents-1",
                    reason=(file_decoupage.SLEEP_REASON % "nexlink") if sleep
                    else "maintenance du poste",
                    sleep=sleep, until=since + 7200, orchestrators=["pilot"],
                    detail="%s in automatic sleep" % agent)

    def test_un_nouvel_episode_de_sommeil_ne_rebriefe_pas(self):
        self.run_briefs([self.pause_alert(5000)], now=10000)
        self.run_briefs([], now=10600)                       # sommeil levé
        self.run_briefs([self.pause_alert(11000)], now=11200)  # reposé, `since` neuf
        self.assertEqual(self.recipients(), ["auditeur", "pilot"])

    def test_une_vraie_pause_operateur_garde_son_episode(self):
        self.run_briefs([self.pause_alert(5000, sleep=False)], now=10000)
        self.run_briefs([self.pause_alert(11000, sleep=False)], now=11200)
        self.assertEqual(self.recipients(),
                         ["auditeur", "auditeur", "pilot", "pilot"])

    def test_le_sommeil_est_reconnu_par_la_raison_seule(self):
        first = self.pause_alert(5000)
        first.pop("sleep")
        second = dict(first, since=11000)
        self.assertEqual(notify.brief_key_of(first), notify.brief_key_of(second))

    def test_une_pause_operateur_garde_le_debut_d_episode_dans_sa_cle(self):
        first = self.pause_alert(5000, sleep=False)
        second = dict(first, since=11000)
        self.assertNotEqual(notify.brief_key_of(first), notify.brief_key_of(second))


if __name__ == "__main__":
    unittest.main()
