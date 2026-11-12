# SPDX-License-Identifier: AGPL-3.0-only
"""#7009 : une pause critique, un courrier par épisode, sans réveiller qui est
lui-même en pause budget sur le même compte.

11/11 03:32-04:23Z : 102 courriers « Critical agent paused on budget » entre
auditeur, mesh-design et orchestrateur, eux-mêmes en pause sur le compte de
l'agent signalé ; 23 tours réveillés pour rien. La clé d'épisode est celle de
#6824 ; ce lot écrit en `info` au destinataire en pause sur le même compte.
"""
from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import alert_briefs, mail, notify, storage

PAUSED = ("budget : critical agent: pause (account secondaire, five_hour 97 %) "
          "until 04:30")


class CriticalPauseQuietTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = SimpleNamespace(canon=(), state_dir=self.tmp.name)
        self.store = mock.Mock()
        self.listing = [
            dict(name="auditeur", team="ameesh", mode="execute"),
            dict(name="mesh-design", team="ameesh", mode="execute",
                 status="blocked", status_text=PAUSED),
            dict(name="pilot", team="ameesh", mode="execute", status="idle")]
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
        patch = mock.patch.object(alert_briefs, "briefs", return_value={
            "pilot": "critical pause", "mesh-design": "critical pause"})
        patch.start()
        self.addCleanup(patch.stop)
        self.notifier = notify.Notifier(self.cfg, notify.parse_config({"default": []}),
                                        sender=object(), log=lambda text: None)

    def alert(self, since, episode=("secondaire", "five_hour")):
        return dict(schema="ameesh-alert/1", type="critical_budget_pause",
                    agent="auditeur", since=since, value=600, threshold=0,
                    host="ameesh-agents-1", harness="claude",
                    responsible="human:smichea", resume_ts=20000,
                    episode=episode, urgent=True,
                    detail="critical agent auditeur paused on budget",
                    orchestrators=["pilot"])

    def test_deux_passes_sur_le_meme_etat_un_seul_courrier(self):
        self.notifier._brief_agents(object(), [self.alert(5000)], None, 10000)
        self.notifier._brief_agents(object(), [self.alert(5000)], None, 10060)
        names = sorted(call.args[2] for call in self.send.call_args_list)
        self.assertEqual(names, ["mesh-design", "pilot"])

    def test_destinataire_en_pause_sur_le_meme_compte_en_info(self):
        by_name = {row["name"]: row for row in self.listing}
        quiet = alert_briefs.quiet_payloads(
            self.alert(5000), ["pilot", "mesh-design", "thread:bureau"], by_name)
        self.assertEqual(quiet, {"mesh-design": {"info": True, "urgent": False}})

    def test_autre_compte_reveille(self):
        by_name = {row["name"]: row for row in self.listing}
        quiet = alert_briefs.quiet_payloads(
            self.alert(5000, ("primaire", "five_hour")), ["mesh-design"], by_name)
        self.assertEqual(quiet, {})

    def test_message_passif_pour_le_destinataire_en_pause(self):
        self.notifier._brief_agents(object(), [self.alert(5000)], None, 10000)
        calls = {call.args[2]: call for call in self.send.call_args_list}
        self.assertIn("mesh-design", calls)
        self.assertIn("pilot", calls)
        payload = self._payload(calls["mesh-design"])
        self.assertTrue(payload.get("info"))
        self.assertEqual(mail.passive_reason({"payload": payload}), "info")
        self.assertEqual(mail.passive_reason({"payload": self._payload(calls["pilot"])}), "")

    @staticmethod
    def _payload(call):
        return call.kwargs["payload"]


if __name__ == "__main__":
    unittest.main()
