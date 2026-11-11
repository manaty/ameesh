# SPDX-License-Identifier: AGPL-3.0-only
"""#6824 : un seul brief par épisode continu de pause critique.

Le statut d'un agent critique clignote `blocked`/`idle` au fil des tours : un
tour réussi repose `idle` sans effacer le motif de pause, la garde budget
repose `blocked`. Chaque bascule réécrit `status_reason_since`, donc le `since`
et l'ancienne clé de brief ; et lorsque la passe d'alertes ne voit pas la pause
(statut `idle`), notify oubliait l'entrée. Le même épisode partait ainsi cinq
fois en trois minutes (#211020/22/25/29/31, auditeur, 11/11).

L'épisode est désormais le couple (compte, fenêtre), comme
`review_family_paused` (#6809) ; l'échéance `resume_ts` retient l'entrée pendant
une absence passagère, et une pause sur une autre fenêtre ou un autre compte
reste un nouvel épisode.
"""
from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import alert_briefs, mail, notify, storage


class CriticalBudgetPauseBriefOnceTest(unittest.TestCase):
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
                                  return_value={"pilot": "critical pause"})
        patch.start()
        self.addCleanup(patch.stop)
        self.notifier = notify.Notifier(self.cfg, notify.parse_config({"default": []}),
                                        sender=object(), log=lambda text: None)

    def run_briefs(self, alerts, now):
        self.notifier._brief_agents(object(), alerts, None, now)

    def recipients(self):
        return sorted(call.args[2] for call in self.send.call_args_list)

    def alert(self, since, resume_ts, *, episode=("secondaire", "five_hour"),
              detail=None):
        return dict(schema="ameesh-alert/1", type="critical_budget_pause",
                    agent="auditeur", since=since, value=600, threshold=0,
                    host="ameesh-agents-1", harness="claude",
                    responsible="human:smichea", resume_ts=resume_ts,
                    episode=episode,
                    detail=detail or ("critical agent auditeur paused on budget "
                                      "(host ameesh-agents-1): pause (account "
                                      "secondaire, five_hour 76 %) until 04:30"),
                    orchestrators=["pilot"])

    def test_une_pause_absente_d_une_passe_ne_refait_pas_le_brief(self):
        # blocage, passe où le statut est idle, puis blocage de la même pause
        self.run_briefs([self.alert(5000, 20000)], now=10000)
        self.run_briefs([], now=10030)
        self.run_briefs([self.alert(10031, 20000)], now=10060)
        self.assertEqual(self.recipients(), ["pilot"])

    def test_une_pause_apres_l_echeance_rebriefe(self):
        # la pause est levée (échéance passée) puis une nouvelle commence
        self.run_briefs([self.alert(5000, 10500)], now=10000)
        self.run_briefs([], now=10600)
        self.run_briefs([self.alert(10601, 12000)], now=10700)
        self.assertEqual(self.recipients(), ["pilot", "pilot"])

    def test_une_autre_fenetre_ouvre_un_nouvel_episode(self):
        first = self.alert(5000, 20000)
        other = self.alert(5000, 20000, episode=("primaire", "seven_day"))
        self.assertNotEqual(notify.brief_key_of(first), notify.brief_key_of(other))


if __name__ == "__main__":
    unittest.main()
