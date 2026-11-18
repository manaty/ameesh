# SPDX-License-Identifier: AGPL-3.0-only
"""#7625 : un compte rendu identique ne repart pas au même destinataire.

La veille envoyait le même texte 49 fois par jour à mesh-design (unités
héritées), et les copies `budget_pause_long` répétaient le même état en
changeant seulement la durée et l'échéance. Un compte rendu est identifié par
son étiquette (`payload.report`) et par l'empreinte de son contenu
(`payload.report_fingerprint`) ; le même compte rendu au même destinataire ne
part pas avant `mail.REPORT_REMIND_S` (6 h). Un contenu changé (ou une
empreinte d'état changée, pour une pause budget) part tout de suite.

Le banc couvre les deux producteurs nommés par le lot : le courrier de veille
(`mail.send`, y compris l'identité d'émission `veille`) et le brief
`budget_pause_long` de `Notifier._brief_agents`.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from ameesh import alert_briefs, mail, notify, registry, storage

from .support import PgTestCase


class MailReportTest(PgTestCase):
    """La fenêtre des comptes rendus, au niveau du dépôt de courrier."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM agent_mailbox")

    def send(self, body: str, when: float, **kwargs):
        with mock.patch.object(mail.time, "time", return_value=when):
            return mail.send(self.db, "veille", "pilote", body,
                             payload={"info": True}, report="info", **kwargs)

    def messages(self, recipient: str = "pilote") -> list[dict]:
        return storage.of(self.db).mailbox.history(recipient, 20)

    def test_two_identical_reports_make_one_mail(self):
        base = time.time()
        first = self.send("nothing changed", base, created_us=int(base * 1e6))
        self.assertIsInstance(first, int)
        second = self.send("nothing changed", base + 60)
        self.assertIsNone(second)
        self.assertEqual(len(self.messages()), 1)

    def test_changed_content_is_sent(self):
        base = time.time()
        self.send("nothing changed", base, created_us=int(base * 1e6))
        changed = self.send("a lot waits for a decision", base + 60)
        self.assertIsInstance(changed, int)
        bodies = [row["body"] for row in self.messages()]
        self.assertEqual(bodies, ["a lot waits for a decision", "nothing changed"])

    def test_another_recipient_is_not_the_same_report(self):
        base = time.time()
        self.send("nothing changed", base, created_us=int(base * 1e6))
        other = self.send("nothing changed", base + 60)
        self.assertIsNone(other)
        with mock.patch.object(mail.time, "time", return_value=base + 60):
            sent = mail.send(self.db, "veille", "autre", "nothing changed",
                             payload={"info": True})
        self.assertIsInstance(sent, int)

    def test_the_report_is_repeated_after_six_hours(self):
        base = time.time()
        self.send("nothing changed", base, created_us=int(base * 1e6))
        suppressed = self.send("nothing changed", base + mail.REPORT_REMIND_S - 60)
        self.assertIsNone(suppressed)
        reminder = self.send("nothing changed", base + mail.REPORT_REMIND_S + 1)
        self.assertIsInstance(reminder, int)
        self.assertEqual(len(self.messages()), 2)

    def test_a_state_fingerprint_can_ignore_volatile_text(self):
        # Une pause budget garde la même identité (agent, hôte) et la même
        # empreinte d'état alors que sa durée et son échéance changent.
        base = time.time()
        first = mail.send(
            self.db, "ameesh", "pilote", "paused for 100 s until 12:00",
            report="budget_pause_long:ouvrier:ag1", report_fingerprint="waiting")
        self.assertIsInstance(first, int)
        second = mail.send(
            self.db, "ameesh", "pilote", "paused for 200 s until 13:00",
            report="budget_pause_long:ouvrier:ag1", report_fingerprint="waiting")
        self.assertIsNone(second)
        # L'échéance annoncée est dépassée : c'est un changement d'état.
        third = mail.send(
            self.db, "ameesh", "pilote", "paused for 300 s, check the caps",
            report="budget_pause_long:ouvrier:ag1", report_fingerprint="passed")
        self.assertIsInstance(third, int)
        self.assertEqual(len(self.messages()), 2)

    def test_an_idempotent_send_is_not_reviewed(self):
        first = mail.send(self.db, "ameesh", "pilote", "same",
                          payload={"info": True}, idempotency_key="once-7625")
        second = mail.send(self.db, "ameesh", "pilote", "same",
                           payload={"info": True}, idempotency_key="once-7625")
        self.assertEqual(first, second)
        self.assertEqual(len(self.messages()), 1)


class BudgetPauseBriefTest(PgTestCase):
    """#7625 : deux états identiques de `budget_pause_long`, un seul courrier."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM agent_mailbox")
        self.mail = mock.patch.object(notify.fil, "record", return_value="thread-id")
        self.mail.start()
        self.addCleanup(self.mail.stop)
        self.briefs = mock.patch.object(alert_briefs, "briefs",
                                        return_value={"pilote": "pause budget"})
        self.briefs.start()
        self.addCleanup(self.briefs.stop)
        self.notifier = notify.Notifier(
            self.cfg, notify.parse_config({"default": []}), log=lambda text: None)

    def alert(self, since: float, *, waits_reset: bool = True,
              resume_ts: float | None = None, reason: str = "all at threshold"):
        return dict(schema="ameesh-alert/1", type="budget_pause_long",
                    agent="ouvrier", since=since, value=7200, threshold=3600,
                    host="ameesh-agents-3", harness="claude", responsible="human:owner",
                    detail="pause", reason=reason, waits_reset=waits_reset,
                    resume_ts=resume_ts, orchestrators=["pilote"])

    def briefs_in_box(self) -> list[dict]:
        return [row for row in storage.of(self.db).mailbox.history("pilote", 20)
                if (row.get("payload") or {}).get("alert") == "budget_pause_long"]

    def close_episode(self, now: float) -> None:
        """Deux passes sans l'alerte, à plus de `BUDGET_PAUSE_GAP_S` : notify
        clôt l'épisode (#7572), la pause suivante est un nouvel épisode."""
        self.notifier._brief_agents(self.db, [], None, now)
        self.notifier._brief_agents(
            self.db, [], None, now + alert_briefs.BUDGET_PAUSE_GAP_S + 1)

    def test_identical_state_in_a_new_episode_is_not_resent_and_a_change_is(self):
        now = time.time()
        self.notifier._brief_agents(
            self.db, [self.alert(now - 7200, resume_ts=now + 3600,
                                 reason="all at threshold (100 s)")], None, now)
        self.assertEqual(len(self.briefs_in_box()), 1)
        # Nouvel épisode du même état : la durée et l'échéance du texte ont
        # changé, pas l'état. Le compte rendu identique ne repart pas.
        later = now + alert_briefs.BUDGET_PAUSE_GAP_S + 60
        self.close_episode(now + 1)
        self.notifier._brief_agents(
            self.db, [self.alert(later - 7200, resume_ts=later + 3600,
                                 reason="all at threshold (200 s)")], None, later)
        self.assertEqual(len(self.briefs_in_box()), 1)
        # L'état change : la pause n'attend plus une remise à zéro connue.
        changed = later + alert_briefs.BUDGET_PAUSE_GAP_S + 60
        self.close_episode(later + 1)
        self.notifier._brief_agents(
            self.db, [self.alert(changed - 7200, waits_reset=False,
                                 resume_ts=None,
                                 reason="profile diverges")], None, changed)
        self.assertEqual(len(self.briefs_in_box()), 2)

    def test_a_suppressed_brief_is_not_retried_every_pass(self):
        now = time.time()
        alert = self.alert(now - 7200, resume_ts=now + 3600, reason="state A")
        self.notifier._brief_agents(self.db, [alert], None, now)
        later = now + alert_briefs.BUDGET_PAUSE_GAP_S + 60
        self.close_episode(now + 1)
        same = self.alert(later - 7200, resume_ts=later + 3600, reason="state A bis")
        self.notifier._brief_agents(self.db, [same], None, later)
        # Le compte rendu identique est supprimé sans laisser de `pending` :
        # la passe suivante ne le retente pas, et l'épisode reste suivi.
        self.notifier._brief_agents(self.db, [same], None, later + 60)
        self.notifier._brief_agents(self.db, [same], None, later + 120)
        self.assertEqual(len(self.briefs_in_box()), 1)
        entry = self.notifier.state["briefs"][notify.key_of(same)]
        self.assertEqual(entry["to"], [])
        self.assertEqual(entry["pending"], [])


class VeilleCliTest(PgTestCase):
    """La minuterie héritée `--from veille` est freinée sans mentir."""

    def test_the_same_legacy_veille_mail_is_sent_once(self):
        for name in ("veille", "pilote"):
            registry.upsert(self.db, name, harness="claude")
        env = self.env(AGENT_MAIL_NAME="veille")
        text = "VEILLE (propriétaire absent) : rien de neuf"
        first = self.cli("send", "pilote", text, "--from", "veille", env=env)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("delivered to pilote", first.stdout)
        second = self.cli("send", "pilote", text, "--from", "veille", env=env)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("identical report to pilote", second.stdout)
        self.assertIn("not sent", second.stdout)
        self.assertEqual(len(storage.of(self.db).mailbox.history("pilote", 20)), 1)


if __name__ == "__main__":
    unittest.main()
