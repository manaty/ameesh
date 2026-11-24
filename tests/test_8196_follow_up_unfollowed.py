# SPDX-License-Identifier: AGPL-3.0-only
"""#8196 (issue #6403, critères 4 et 5) : un suivi que plus aucun réveil ne suit.

Un réveil ne sert qu'une fois (#2466). `follow_up_unfollowed` se lève dans
l'heure pour un suivi ouvert sans réveil armé, dont le réveil a été délivré
sans note de l'assigné, ou dont le réveil échu n'est jamais délivré (agent de
conversation externe, #233204). L'assigné est briefé d'abord ; son humain
responsable une heure plus tard ; une alerte par épisode, levée au réarmement
ou à la clôture.

Le critère 5 est écrit en test d'intégration : l'avatar change d'hôte
(`ameesh agent move`) et reçoit toujours le réveil de son suivi, par
l'exécuteur de l'hôte d'arrivée.
"""
from __future__ import annotations

import dataclasses
import time
import unittest

from ameesh import alert_briefs, exploitation, registry, relocation, storage, work
from ameesh.notify import Notifier, NotifyConfig
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase
from .test_7108_avatar_b4 import ATELIER, AVATAR, BANC, ALICE, _AvatarCase

GRACE = exploitation.FOLLOW_UP_GRACE_S
HUMAN = exploitation.FOLLOW_UP_HUMAN_AFTER_S


def _unfollowed(db, listing, now):
    return [a for a in exploitation._follow_ups_unfollowed(db, listing, now)]


class FollowUpUnfollowedTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("avatar", "dev"):
            registry.upsert(self.db, name, harness="codex", host=self.cfg.host,
                            cwd=self.tmp, mode="execute")
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice'"
                        " WHERE name = 'avatar'")

    def listing(self):
        return storage.of(self.db).operations.listing()

    def follow_up(self, assignee="avatar"):
        return work.add(self.db, title="Check the answer", type="follow-up",
                        assignee=assignee, app="ameesh", actor=assignee,
                        cfg=self.cfg)["id"]

    def alerts(self, now):
        return _unfollowed(self.db, self.listing(), now)

    def deliver(self, lot, name="avatar"):
        """L'exécuteur délivre le réveil échu (claim puis consommation)."""
        work.schedule_wake(self.db, lot, time.time() - 1, text="check", actor=name)
        st = storage.of(self.db).lot_sessions
        taken = st.claim_wake(name, lot, time.time())
        self.assertIsNotNone(taken)
        self.assertTrue(st.consume_wake(name, lot, taken["wake_ts"]))
        return time.time()

    def test_created_without_wake_alerts_after_the_grace(self):
        lot = self.follow_up()
        now = time.time()
        self.assertEqual(self.alerts(now), [])
        [alert] = self.alerts(now + GRACE + 1)
        self.assertEqual((alert["type"], alert["lot"], alert["reason"], alert["agent"]),
                         ("follow_up_unfollowed", lot, "no_wake", "avatar"))
        self.assertFalse(alert["human_due"])
        self.assertEqual(alert["responsible"], "human:alice")
        self.assertIn("ameesh work wake %d" % lot, alert["detail"])

    def test_an_armed_wake_is_followed_and_development_lots_are_ignored(self):
        lot = self.follow_up()
        work.schedule_wake(self.db, lot, time.time() + 86400, text="tomorrow",
                           actor="avatar")
        work.add(self.db, title="Code", app="ameesh", assignee="dev", actor="dev",
                 footprint=[])
        self.assertEqual(self.alerts(time.time() + GRACE + 1), [])

    def test_delivered_without_note_then_human_after_the_hour(self):
        lot = self.follow_up()
        delivered = self.deliver(lot)
        self.assertEqual(self.alerts(delivered + 60), [], "le tour qui suit a son délai")
        [first] = self.alerts(delivered + GRACE + 1)
        self.assertEqual(first["reason"], "delivered_without_note")
        self.assertFalse(first["human_due"])
        [later] = self.alerts(delivered + GRACE + HUMAN + 1)
        self.assertTrue(later["human_due"])
        # une alerte par épisode : la bascule vers l'humain garde la clé
        self.assertEqual(exploitation.alert_key(first), exploitation.alert_key(later))

    def test_a_note_without_rearm_stays_the_same_episode(self):
        lot = self.follow_up()
        delivered = self.deliver(lot)
        [before] = self.alerts(delivered + GRACE + 1)
        work.note(self.db, lot, "asked again, no answer yet", actor="avatar")
        [after] = self.alerts(delivered + GRACE + 2)
        self.assertEqual(after["reason"], "no_wake")
        self.assertEqual(exploitation.alert_key(before), exploitation.alert_key(after))

    def test_rearm_or_close_lifts_the_alert(self):
        lot = self.follow_up()
        delivered = self.deliver(lot)
        self.assertTrue(self.alerts(delivered + GRACE + 1))
        work.schedule_wake(self.db, lot, time.time() + 86400, text="check again",
                           actor="avatar")
        self.assertEqual(self.alerts(delivered + GRACE + 1), [])
        other = self.follow_up()
        self.assertTrue(self.alerts(time.time() + GRACE + 1))
        work.close(self.db, other, done=True, actor="avatar")
        work.close(self.db, lot, done=True, actor="avatar")
        self.assertEqual(self.alerts(time.time() + GRACE + 1), [])

    def test_a_note_by_someone_else_does_not_count(self):
        lot = self.follow_up()
        delivered = self.deliver(lot)
        work.note(self.db, lot, "pilot looked", actor="dev")
        [alert] = self.alerts(delivered + GRACE + 1)
        self.assertEqual(alert["reason"], "delivered_without_note")

    def test_a_due_wake_never_delivered_alerts(self):
        """#233204 : l'assigné n'est mené par aucun exécuteur (chat-*)."""
        lot = self.follow_up()
        due = time.time() - 10
        work.schedule_wake(self.db, lot, due, text="check", actor="avatar")
        self.assertEqual(self.alerts(due + 60), [])
        [alert] = self.alerts(due + GRACE + 1)
        self.assertEqual(alert["reason"], "wake_undelivered")

    def test_working_assignee_is_waited_for_within_the_hour(self):
        lot = self.follow_up()
        delivered = self.deliver(lot)
        listing = [dict(row, status="running", lease_live=True)
                   if row["name"] == "avatar" else row for row in self.listing()]
        self.assertEqual(_unfollowed(self.db, listing, delivered + GRACE + 1), [])
        [alert] = _unfollowed(self.db, listing, delivered + GRACE + HUMAN + 1)
        self.assertEqual(alert["lot"], lot)

    def test_alerts_pass_includes_it(self):
        lot = self.follow_up()
        found = [a for a in exploitation.alerts(self.cfg, self.db, now=time.time() + GRACE + 1)
                 if a["type"] == "follow_up_unfollowed"]
        self.assertEqual([a["lot"] for a in found], [lot])


class RoutingTest(unittest.TestCase):
    ALERT = {"type": "follow_up_unfollowed", "agent": "avatar", "lot": 7,
             "title": "t", "detail": "follow-up #7 is no longer followed",
             "reason": "no_wake", "since": 1.0, "human_due": False}

    def test_brief_goes_to_the_assignee_only(self):
        listing = [{"name": "avatar", "lease_live": True},
                   {"name": "pilot", "lease_live": True}]
        out = alert_briefs.briefs(self.ALERT, listing, auditor_names=("pilot",),
                                  orchestrator_names=("pilot",))
        self.assertEqual(list(out), ["avatar"])
        self.assertIn("#7", out["avatar"])

    def test_external_assignee_reads_its_thread(self):
        listing = [{"name": "avatar", "mode": "externe", "lease_live": False}]
        out = alert_briefs.briefs(self.ALERT, listing)
        self.assertEqual(list(out), ["thread:avatar"])

    def test_human_only_once_due(self):
        notifier = Notifier.__new__(Notifier)
        notifier.ncfg = NotifyConfig(types=("follow_up_unfollowed",))
        self.assertFalse(notifier._deliverable(self.ALERT))
        self.assertTrue(notifier._deliverable(dict(self.ALERT, human_due=True)))


class WakeAfterHostChangeTest(_AvatarCase):
    """#6403 critère 5 : l'avatar déplacé d'hôte reçoit toujours le réveil de
    son suivi, par l'exécuteur de l'hôte d'arrivée ; l'exécuteur de départ ne
    tient plus de bail. La garantie #2466 : le réveil suit l'assigné du lot,
    pas un hôte."""

    def test_wake_follows_the_avatar_to_its_new_host(self):
        self.source()
        lot = work.add(self.db, title="Ask the supplier again", type="follow-up",
                       assignee=AVATAR, app="acme-web", actor=AVATAR)["id"]
        work.schedule_wake(self.db, lot, time.time() + 3600, text="supplier",
                           actor=AVATAR)
        self.ready(BANC, tenant=True)
        self.ready(ATELIER, tenant=False)
        relocation.move_manual(self.cfg, self.db, AVATAR, BANC, actor=ALICE)
        self.assertEqual(self.row(AVATAR)["host"], BANC)
        self.assertIsNone(registry.renew(self.db, AVATAR, "runner-source", 3, 3600),
                          "l'exécuteur de départ a perdu le bail")
        # l'échéance tombe après le déplacement
        self.db.execute("UPDATE work_items SET wake_at = now() - interval '1 second'"
                        " WHERE id = %s", (lot,))
        cfg = dataclasses.replace(self.cfg, host=BANC, runner_id="%s/alice" % BANC)
        runner = Runner(cfg, self.db, once=True)
        lease = registry.claim(self.db, AVATAR, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, AVATAR), lease)
        try:
            worker._lot_workdir_blocked = lambda lot_id: False
            worker._scheduled_wake_checked = float("-inf")
            spec = worker._scheduled_wake_spec()
            self.assertIsNotNone(spec, "l'hôte d'arrivée délivre le réveil du suivi")
            self.assertEqual(spec["lot"], str(lot))
            worker._turn_launched = True
            worker._consume_scheduled_wake(spec)
        finally:
            worker._forget_wake_claim()
            worker.watchdog_stop.set()
        notes = [e.get("note") or "" for e in work.events(self.db, lot)]
        self.assertTrue(any(n.startswith("wake: scheduled wake delivered") for n in notes))
        self.assertIsNone(work.get(self.db, lot).get("wake_ts"))
        # délivré sans note de l'avatar : l'alerte de #8196 prend le relais
        [alert] = exploitation._follow_ups_unfollowed(
            self.db, storage.of(self.db).operations.listing(), time.time() + GRACE + 1)
        self.assertEqual((alert["lot"], alert["reason"]), (lot, "delivered_without_note"))


if __name__ == "__main__":
    unittest.main()
