# SPDX-License-Identifier: AGPL-3.0-only
"""#7521 : un recul de l'échéance ne refait pas le courrier de pause budget.

15/11 : six agents d'AG3 en pause depuis 30 h recevaient un nouveau courrier
« Agent paused on budget until its window reset » à chaque fois que le
plafond de rythme repoussait la reprise (22:52Z → 04:33Z → 06:14Z, cinq
reculs) : 52 courriers et 14 tours de coordinateurs réveillés. L'échéance
`resume_ts` était dans la clé de l'épisode (#7175), donc chaque minute neuve
ouvrait un épisode et un brief.

L'épisode reste la pause continue (agent, hôte) : notify suit l'échéance qui
recule sans re-briefer, et ne rouvre l'épisode que lorsque l'échéance
annoncée est réellement dépassée alors que la pause dure — la promesse du
courrier #7175.
"""
from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import alert_briefs, mail, notify, storage


class BudgetPauseReculTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = SimpleNamespace(canon=(), state_dir=self.tmp.name)
        self.store = mock.Mock()
        self.listing = [dict(name=n, team="ameesh", mode="execute")
                        for n in ("ouvrier", "pilot")]
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
                                  return_value={"pilot": "pause budget"})
        patch.start()
        self.addCleanup(patch.stop)
        self.notifier = notify.Notifier(self.cfg, notify.parse_config({"default": []}),
                                        sender=object(), log=lambda text: None)
        self.run = self.notifier._brief_agents

    def alert(self, resume_ts, *, since=1000, agent="ouvrier",
              host="ameesh-agents-3"):
        return dict(schema="ameesh-alert/1", type="budget_pause_long", agent=agent,
                    since=since, value=7200, threshold=3600, host=host,
                    harness="claude", responsible="human:smichea", detail="pause",
                    resume_ts=resume_ts, waits_reset=True,
                    orchestrators=["pilot"])

    def entry(self, alert):
        return self.notifier.state["briefs"][notify.brief_key_of(alert)]

    def test_un_recul_avant_l_echeance_ne_refait_pas_le_courrier(self):
        self.run(object(), [self.alert(40000)], None, 10000)
        self.run(object(), [], None, 10030)               # statut qui clignote
        # le plafond de rythme repousse la reprise de 40000 à 50000
        self.run(object(), [self.alert(50000)], None, 10060)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.entry(self.alert(50000))["resume_ts"], 50000)

    def test_la_retenue_suit_l_echeance_reculee(self):
        self.run(object(), [self.alert(40000)], None, 10000)
        self.run(object(), [], None, 30000)               # absente avant l'échéance
        self.run(object(), [self.alert(50000)], None, 30060)
        # l'ancienne échéance est passée, la nouvelle non : l'entrée reste
        self.run(object(), [], None, 45000)
        self.run(object(), [self.alert(50000)], None, 45030)
        self.assertEqual(self.send.call_count, 1)

    def test_une_echeance_depassee_pendant_la_pause_rebriefe_une_fois(self):
        self.run(object(), [self.alert(40000)], None, 10000)
        # l'échéance est atteinte et la pause repart sur une autre : ce n'est
        # plus « attendez le reset sans rien faire », un nouveau brief est dû
        self.run(object(), [self.alert(50000)], None, 40030)
        self.run(object(), [self.alert(50000)], None, 40060)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.entry(self.alert(50000))["resume_ts"], 50000)

    def test_la_cle_ignore_le_recul_mais_pas_l_agent_ni_l_hote(self):
        first = self.alert(40000)
        same = self.alert(50000)
        other_agent = self.alert(50000, agent="ouvrier-2")
        other_host = self.alert(50000, host="ameesh-agents-1")
        self.assertEqual(notify.brief_key_of(first), notify.brief_key_of(same))
        self.assertNotEqual(notify.brief_key_of(first),
                            notify.brief_key_of(other_agent))
        self.assertNotEqual(notify.brief_key_of(first),
                            notify.brief_key_of(other_host))


if __name__ == "__main__":
    unittest.main()
