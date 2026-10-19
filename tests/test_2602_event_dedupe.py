# SPDX-License-Identifier: AGPL-3.0-only
"""#2602: automatic mail follows actionable state, not observation jitter."""
from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import alert_briefs, mail, notify, storage


class SloDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = SimpleNamespace(canon=(), state_dir=self.tmp.name)
        self.store = mock.Mock()
        self.listing = [dict(name=n, team="ameesh", mode="execute")
                        for n in ("dev", "pilot", "auditeur", "bureau")]
        self.listing[-1]["mode"] = "externe"
        self.store.operations.listing.side_effect = lambda: self.listing
        self.item = dict(state="build", assignee="dev")
        self.store.work.get.side_effect = lambda lot: self.item
        self.store.operations.open_lots_activity.side_effect = lambda limit: [
            dict(id=2602, **self.item)]
        patch = mock.patch.object(storage, "of", return_value=self.store)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(mail, "send", return_value=1)
        self.send = patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(notify.fil, "record", return_value="thread-id")
        self.thread = patch.start()
        self.addCleanup(patch.stop)
        self.alert = dict(type="slo_breach", lot=2602, agent="dev", project="ameesh",
                          lot_state="build", step="build_quiet", level=1,
                          targets=["assignee"], orchestrators=["pilot"], since=1,
                          value=10000, threshold=7200, reason="build_quiet/level-1")
        self.notifier = self.make_notifier()

    def make_notifier(self, state=None):
        return notify.Notifier(self.cfg, notify.parse_config({"default": []}),
                               state=state, sender=object(), log=lambda text: None)

    def run_briefs(self, alerts=None, now=10000):
        self.notifier._brief_agents(object(), [self.alert] if alerts is None else alerts,
                                     None, now)

    def recipients(self):
        return [call.args[2] for call in self.send.call_args_list]

    def test_time_jitter_and_restart_do_not_resend_or_remind(self):
        self.run_briefs()
        self.alert.update(since=200, value=99999)
        path = os.path.join(self.tmp.name, "state.json")
        notify.save_state(path, self.notifier.state)
        self.notifier = self.make_notifier(notify.load_state(path))
        self.run_briefs(now=100000)
        self.assertEqual(self.recipients(), ["dev"])

    def test_absent_pass_does_not_forget_unchanged_state(self):
        self.run_briefs()
        self.run_briefs([])
        self.run_briefs()
        self.assertEqual(self.recipients(), ["dev"])

    def test_next_level_delivers_once_even_if_levels_flap(self):
        self.run_briefs()
        low = dict(self.alert)
        self.alert.update(level=2, targets=["pilot"])
        self.run_briefs()
        self.run_briefs([low])
        self.run_briefs()
        self.assertEqual(self.recipients(), ["dev", "pilot"])

    def test_state_change_rearms_delivery(self):
        self.run_briefs()
        self.item["state"] = "blocked"
        self.run_briefs([])
        self.item["state"] = "build"
        self.run_briefs()
        self.assertEqual(self.recipients(), ["dev", "dev"])

    def test_closed_lot_drops_ledger(self):
        self.run_briefs()
        self.item["state"] = "closed"
        self.run_briefs([])
        self.assertEqual(self.notifier.state["briefs"], {})

    def test_failed_recipient_retried_without_repeating_success(self):
        self.alert.update(level=3, targets=["pilot", "auditeur"])
        def send(db, sender, to, *args, **kwargs):
            if to == "pilot":
                raise RuntimeError("temporarily unavailable")
            return 1
        self.send.side_effect = send
        self.run_briefs()
        self.send.side_effect = None
        self.run_briefs()
        self.assertEqual(self.recipients(), ["auditeur", "pilot", "pilot"])

    def test_new_recipient_receives_same_state(self):
        self.alert.update(level=2, targets=["pilot"])
        self.run_briefs()
        self.listing.append(dict(name="pilot2", team="ameesh"))
        self.alert["orchestrators"] = ["pilot", "pilot2"]
        self.run_briefs()
        self.assertEqual(self.recipients(), ["pilot", "pilot2"])

    def test_external_bureau_copy_goes_to_thread_only_once(self):
        self.alert.update(level=3, targets=["bureau", "auditeur"])
        self.run_briefs()
        self.run_briefs([])
        self.run_briefs(now=100000)
        self.assertEqual(self.recipients(), ["auditeur"])
        self.thread.assert_called_once()
        self.assertEqual(self.thread.call_args.kwargs["lot"], 2602)
        self.assertEqual(self.thread.call_args.kwargs["recipients"], ["bureau"])

    def test_failed_thread_write_retries_only_the_copy(self):
        self.alert.update(level=3, targets=["bureau", "auditeur"])
        self.thread.return_value = None
        self.run_briefs()
        self.thread.return_value = "thread-id"
        self.run_briefs()
        self.assertEqual(self.recipients(), ["auditeur"])
        self.assertEqual(self.thread.call_count, 2)

    def test_capacity_gap_does_not_repeat_pilot_wake(self):
        alert = dict(type="idle_capacity", orchestrators=["pilot"], agents=["dev"],
                     lots=[2602], detail="idle", value=1, threshold=300)
        with mock.patch.object(alert_briefs, "briefs", return_value={"pilot": "idle"}):
            self.run_briefs([alert])
            self.run_briefs([], now=11000)
            self.run_briefs([alert], now=12000)
        self.assertEqual(self.recipients(), ["pilot"])

    def test_home_desk_still_receives_operational_escalation(self):
        self.alert.update(project="home", level=3, targets=["bureau", "auditeur"])
        self.run_briefs()
        self.assertEqual(self.recipients(), ["auditeur", "bureau"])
        self.thread.assert_not_called()

    def test_external_bureau_cannot_be_reintroduced_as_pilot(self):
        self.alert.update(level=3, targets=["pilot", "bureau"], orchestrators=["bureau"])
        out = alert_briefs.briefs(self.alert, self.listing)
        self.assertEqual(out, {})
