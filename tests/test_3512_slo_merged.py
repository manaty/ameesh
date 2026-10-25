# SPDX-License-Identifier: AGPL-3.0-only
"""#3512: delayed packet recording and stale snapshots must not send merge SLOs."""
from __future__ import annotations

import os
import subprocess
from unittest import mock

from ameesh import mail, notify, registry, slo_delivery, work

from .test_789_slo_alerts import _Base, _GIT_DEV1


class MergedSloTest(_Base):
    def setUp(self):
        super().setUp()
        self.qa(verdict_s=7200)
        self.notifier = notify.Notifier(
            self.cfg, notify.parse_config({"default": []}),
            sender=object(), log=lambda text: None)
        self.snapshot = self.slo()
        self.assertEqual([a["step"] for a in self.snapshot], ["verdict_merge"])

    def deliver(self, alerts=None):
        with mock.patch.object(mail, "send", return_value=1) as send, \
                mock.patch.object(notify.fil, "record", return_value="thread") as thread:
            self.notifier._brief_agents(
                self.db, self.snapshot if alerts is None else alerts, None, self.now)
        return send, thread

    def assert_silent(self):
        send, thread = self.deliver()
        send.assert_not_called()
        thread.assert_not_called()
        self.assertEqual(self.notifier.state["briefs"], {})

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], check=True,
                              capture_output=True, text=True, env=_GIT_DEV1).stdout.strip()

    def frozen_repo(self):
        self.repo = os.path.join(self.tmp, self._testMethodName)
        os.mkdir(self.repo)
        self.git("init", "-q", "-b", "main")
        self.commit_file("base", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.git("checkout", "-q", "-b", "lot/3512-test")
        self.commit_file("fix", "fix")
        self.head = self.git("rev-parse", "HEAD")
        registry.upsert(self.db, "dev1", harness="claude", host=self.cfg.host, cwd=self.repo)
        self.db.execute("UPDATE work_items SET branch = 'lot/3512-test',"
                        " branch_target = 'main', branch_head = %s WHERE id = %s",
                        (self.head, self.lot["id"]))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, note)"
            " VALUES (%s, 'frozen', now() - interval '3 hours', %s, %s)",
            (self.lot["id"], self.head, "ameesh-freeze-absent: main"))
        self.db.execute("UPDATE work_item_milestones SET sha = %s"
                        " WHERE work_item_id = %s AND kind = 'verdict'",
                        (self.head, self.lot["id"]))

    def commit_file(self, name, text):
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as stream:
            stream.write(text + "\n")
        self.git("add", name)
        self.git("commit", "-q", "-m", text)

    def test_open_lot_still_delivers(self):
        send, _ = self.deliver()
        self.assertTrue(send.called)

    def test_terminal_state_after_snapshot_sends_neither_mail_nor_thread(self):
        registry.upsert(self.db, "bureau", harness="claude", mode="externe")
        for state in ("merged", "promoted", "closed"):
            with self.subTest(state=state):
                self.db.execute("UPDATE work_items SET state = %s, close_reason = %s"
                                " WHERE id = %s",
                                (state, "done" if state == "closed" else None,
                                 self.lot["id"]))
                self.assert_silent()

    def test_merge_between_recipients_stops_remaining_deliveries(self):
        def merge(*args, **kwargs):
            self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                            (self.lot["id"],))
            return 1
        with mock.patch.object(mail, "send", side_effect=merge) as send:
            self.notifier._brief_agents(self.db, self.snapshot, None, self.now)
        self.assertEqual(send.call_count, 1)

    def test_merge_milestone_with_delayed_state_suppresses_snapshot(self):
        self.db.execute("INSERT INTO work_item_milestones (work_item_id, kind, sha)"
                        " VALUES (%s, 'merged', %s)", (self.lot["id"], "a" * 40))
        self.assertEqual(work.get(self.db, self.lot["id"])["state"], "qa")
        self.assert_silent()

    def test_annulled_merge_is_not_completion_evidence(self):
        item = work.get(self.db, self.lot["id"])
        self.assertFalse(slo_delivery.merge_observed(
            self.cfg, self.db, item, [{"kind": "merged", "annulled": True}]))

    def test_git_delivery_before_packet_recording_suppresses_generation_and_mail(self):
        self.frozen_repo()
        self.git("update-ref", "refs/remotes/origin/main", self.head)
        self.assertEqual(work.get(self.db, self.lot["id"])["state"], "qa")
        self.assertEqual(self.slo(), [])
        self.assert_silent()

    def test_cherry_picked_packet_without_membership_is_already_delivered(self):
        self.frozen_repo()
        self.git("checkout", "-q", "main")
        self.commit_file("other", "another lot")
        self.git("cherry-pick", self.head)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), self.head)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertEqual(self.slo(), [])
        self.assert_silent()

    def test_unpublished_merge_does_not_suppress_the_alert(self):
        self.frozen_repo()
        self.git("branch", "-f", "main", self.head)
        self.assertFalse(slo_delivery.merge_observed(
            self.cfg, self.db, work.get(self.db, self.lot["id"])))
        send, _ = self.deliver()
        self.assertTrue(send.called)

    def test_reverted_delivery_does_not_suppress_the_alert(self):
        self.frozen_repo()
        self.git("revert", "--no-edit", self.head)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertEqual(len(self.slo()), 1)
        send, _ = self.deliver()
        self.assertTrue(send.called)

    def test_new_head_after_delivered_freeze_does_not_suppress_the_alert(self):
        self.frozen_repo()
        self.git("update-ref", "refs/remotes/origin/main", self.head)
        self.commit_file("next", "next fix")
        self.db.execute("UPDATE work_items SET branch_head = %s WHERE id = %s",
                        (self.git("rev-parse", "HEAD"), self.lot["id"]))
        send, _ = self.deliver()
        self.assertTrue(send.called)

    def test_unreadable_git_evidence_does_not_suppress_the_alert(self):
        self.frozen_repo()
        with mock.patch.object(slo_delivery.plan_git, "probe", side_effect=OSError("unreadable")):
            send, _ = self.deliver()
        self.assertTrue(send.called)
