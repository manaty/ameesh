# SPDX-License-Identifier: AGPL-3.0-only
"""#7307 : un seul brief `review_family_paused` par épisode (compte, fenêtre).

La pause d'allure se lève à son échéance puis se repose 10 à 25 min plus tard
avec une échéance neuve (codex le 13/11 : 04:21, 04:34, 04:43, 04:46, 04:58 ;
claude le 14/11 : 00:07, 00:39, 01:03). notify oubliait l'entrée dès
l'échéance du premier brief, jamais mise à jour : chaque reprise rebriefait.
"""
from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import alert_briefs, mail, notify, storage

M = 60.0


class FamilyPauseShortRepauseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = SimpleNamespace(canon=(), state_dir=self.tmp.name)
        self.store = mock.Mock()
        self.listing = [dict(name=n, team="ameesh", mode="execute")
                        for n in ("auditeur", "pilot")]
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
                                  return_value={"pilot": "family paused"})
        patch.start()
        self.addCleanup(patch.stop)
        self.notifier = notify.Notifier(self.cfg, notify.parse_config({"default": []}),
                                        sender=object(), log=lambda text: None)

    def run_briefs(self, alerts, now):
        self.notifier._brief_agents(object(), alerts, None, now)

    def count(self):
        return len(self.send.call_args_list)

    def alert(self, resume_ts, *, episode=(("secondaire", "five_hour"),)):
        return dict(schema="ameesh-alert/1", type="review_family_paused",
                    agent=None, since=None, value=None, threshold=None,
                    family="codex", resume_ts=resume_ts, episode=list(episode),
                    pauses=[], held_lots=[], lots=[7266],
                    detail="review family codex paused on budget",
                    orchestrators=["pilot"])

    def test_trois_pauses_courtes_donnent_un_seul_brief(self):
        t = 100000.0
        self.run_briefs([self.alert(t + 2*M)], now=t)           # 04:21 → 04:23
        self.run_briefs([], now=t + 3*M)                        # reprise
        self.run_briefs([self.alert(t + 20*M)], now=t + 13*M)   # 04:34 → 04:41
        self.run_briefs([], now=t + 21*M)
        self.run_briefs([self.alert(t + 23*M)], now=t + 22*M)   # 04:43 → 04:44
        self.run_briefs([], now=t + 24*M)
        self.run_briefs([self.alert(t + 95*M)], now=t + 37*M)   # 04:58 → 05:56
        self.assertEqual(self.count(), 1)

    def test_l_echeance_suit_la_pause_qui_se_prolonge(self):
        t = 100000.0
        self.run_briefs([self.alert(t + 2*M)], now=t)
        self.run_briefs([self.alert(t + 90*M)], now=t + 1*M)    # recalculée
        self.run_briefs([], now=t + 60*M)                       # clignotement
        self.run_briefs([self.alert(t + 90*M)], now=t + 61*M)
        self.assertEqual(self.count(), 1)

    def test_une_absence_longue_ouvre_un_nouvel_episode(self):
        t = 100000.0
        self.run_briefs([self.alert(t + 2*M)], now=t)
        self.run_briefs([], now=t + 3*M)
        self.run_briefs([], now=t + 3*M + alert_briefs.FAMILY_PAUSE_GAP_S)
        self.run_briefs([self.alert(t + 120*M)], now=t + 50*M)
        self.assertEqual(self.count(), 2)


if __name__ == "__main__":
    unittest.main()
