# SPDX-License-Identifier: AGPL-3.0-only
"""#6256 : le sommeil automatique n'envoie aucun courrier aux agents.

Feuille de route épuisée : 12 à 26 agents s'endorment ensemble et franchissent
ensemble les seuils de 30 min puis de 6 h. Chaque `operator_pause_long` et
chaque `operator_pause_host` partait à chaque destinataire, puis en relance à
6 h (264 courriers en 24 h, 08/11), pour un texte qui dit qu'aucun geste n'est
attendu. L'alerte reste lisible dans `ameesh alerts` ; une vraie pause
opérateur garde son brief.
"""
from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import alert_briefs, file_decoupage, mail, notify, storage


class SleepUnbriefedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = SimpleNamespace(canon=(), state_dir=self.tmp.name)
        self.store = mock.Mock()
        self.dormeurs = ["dev%02d" % i for i in range(12)]
        self.listing = [dict(name=n, team="ameesh", mode="execute")
                        for n in (*self.dormeurs, "pilot", "auditeur")]
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
                                  return_value={"pilot": "pause", "auditeur": "pause"})
        patch.start()
        self.addCleanup(patch.stop)
        self.notifier = notify.Notifier(self.cfg, notify.parse_config({"default": []}),
                                        sender=object(), log=lambda text: None)

    def run_briefs(self, alerts, now):
        self.notifier._brief_agents(object(), alerts, None, now)

    def recipients(self):
        return sorted(call.args[2] for call in self.send.call_args_list)

    def pauses(self, since, *, sleep=True):
        reason = (file_decoupage.SLEEP_REASON % "ameesh") if sleep \
            else "maintenance du poste"
        alerts = [dict(type="operator_pause_long", agent=name, since=since,
                       value=2400, threshold=1800, host="ameesh-agents-1",
                       reason=reason, sleep=sleep, until=since + 7200,
                       orchestrators=["pilot"], detail="%s paused" % name)
                  for name in self.dormeurs]
        alerts.append(dict(type="operator_pause_host", agent=None, since=None,
                           value=12, threshold=12, host="ameesh-agents-1",
                           agents=list(self.dormeurs), sleep=sleep,
                           orchestrators=["pilot"], detail="12/12 agents paused"))
        return alerts

    def test_douze_dormeurs_depuis_40_min_sans_courrier_ni_relance(self):
        self.run_briefs(self.pauses(1000), now=1000 + 40 * 60)
        self.run_briefs(self.pauses(1000), now=1000 + alert_briefs.REMIND_AFTER_S + 60)
        self.assertEqual(self.recipients(), [])

    def test_une_vraie_pause_operateur_reste_briefee(self):
        self.run_briefs(self.pauses(1000, sleep=False), now=1000 + 40 * 60)
        self.assertIn("pilot", self.recipients())

    def test_le_sommeil_reconnu_par_la_raison_seule(self):
        alert = self.pauses(1000)[0]
        alert.pop("sleep")
        self.assertTrue(alert_briefs.sleep_unbriefed(alert))
        self.assertFalse(alert_briefs.sleep_unbriefed(
            dict(alert, reason="maintenance du poste")))


if __name__ == "__main__":
    unittest.main()
