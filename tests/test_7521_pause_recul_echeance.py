# SPDX-License-Identifier: AGPL-3.0-only
"""#7521 : un recul de l'échéance ne refait pas le courrier de pause budget.

15/11 : six agents d'AG3 en pause depuis 30 h recevaient un nouveau courrier
« Agent paused on budget until its window reset » à chaque fois que le
plafond de rythme repoussait la reprise (22:52Z → 04:33Z → 06:14Z, cinq
reculs) : 52 courriers et 14 tours de coordinateurs réveillés. L'échéance
`resume_ts` était dans la clé de l'épisode (#7175), donc chaque minute neuve
ouvrait un épisode et un brief.

L'épisode reste la pause continue (agent, hôte) : notify suit l'échéance qui
recule sans re-briefer. #7572 étend la règle au rattrapage du rythme (`pace`) :
une échéance dépassée pendant que la pause dure n'ouvre pas un nouvel épisode.
Une remise à zéro (`reset`) réellement dépassée, elle, re-briefe une fois
(#7175, B1 du gel #42425) : l'agent n'a pas repris.
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
              host="ameesh-agents-3", waits_reset=True, kind="pace"):
        out = dict(schema="ameesh-alert/1", type="budget_pause_long", agent=agent,
                   since=since, value=7200, threshold=3600, host=host,
                   harness="claude", responsible="human:smichea", detail="pause",
                   resume_ts=resume_ts, resume_kind=kind, orchestrators=["pilot"])
        if waits_reset:
            out["waits_reset"] = True
        return out

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

    def test_une_echeance_de_rythme_depassee_ne_rebriefe_pas(self):
        self.run(object(), [self.alert(40000)], None, 10000)
        # #7572 : l'échéance de rattrapage est atteinte et la pause continue
        # sur une autre estimation — c'est la même pause, déjà dite : pas de
        # second brief. Une remise à zéro, elle, re-briefe (B1, #7572).
        self.run(object(), [self.alert(50000)], None, 40030)
        self.run(object(), [self.alert(50000)], None, 40060)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.entry(self.alert(50000))["resume_ts"], 50000)

    def test_une_pause_qui_reprend_apres_une_absence_rebriefe(self):
        self.run(object(), [self.alert(40000)], None, 10000)
        # l'alerte disparaît : premier passage absent, dans la grâce du
        # clignotement de statut.
        self.run(object(), [], None, 20000)
        # l'absence dure au-delà de BUDGET_PAUSE_GAP_S : l'agent a repris,
        # l'épisode est clos.
        self.run(object(), [], None, 20000 + alert_briefs.BUDGET_PAUSE_GAP_S + 1)
        # une nouvelle pause est un nouvel épisode, même agent et même hôte.
        self.run(object(), [self.alert(60000, since=25000)], None, 30000)
        self.assertEqual(self.send.call_count, 2)

    def test_une_pause_sans_attente_de_reset_ne_repete_pas_non_plus(self):
        # #7572 : le cas du 16/11 — la raison porte le pourcentage et l'heure,
        # qui changent à chaque relevé ; sans clé stable, chaque passe
        # refaisait un courrier.
        first = self.alert(None, waits_reset=False)
        first["detail"] = "pause (account secondaire, seven_day 59 %) until 17:07"
        self.run(object(), [first], None, 10000)
        second = dict(first, since=11000, resume_ts=50000,
                      detail="pause (account secondaire, seven_day 61 %) until 18:30")
        self.run(object(), [second], None, 10060)
        # même pause, aucune relance avant les 6 h (une pause avec geste
        # attendu les garde, #1466/B2).
        self.run(object(), [second], None, 20000)
        self.assertEqual(self.send.call_count, 1)

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
