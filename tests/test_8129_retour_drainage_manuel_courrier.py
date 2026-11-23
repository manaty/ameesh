# SPDX-License-Identifier: AGPL-3.0-only
"""#8129: during a dated manual drain, ordinary mail does not call the host back.

R1 #6002 (23/11): AG3 drained by hand at 04:26:11Z with a return at 05:54:11Z
was called back at 04:44:58Z because `return_targets` counted one unread,
non-urgent notice for an agent admitted on AG3 only. A manual drain carrying
its `return_at` is a promise to come back at that time: until then only an
`--urgent` unread, an actionable lot, a pending prompt or a restart request
wake the host. An automatic drain (#8029) still wakes on any mail.
"""
from __future__ import annotations

from datetime import datetime, timezone
import unittest

from tests.support import PgTestCase
from ameesh import host_release, storage, vm_adapter as vm


HOST = "ag3"
OTHER = "ag1"
NOW = datetime(2026, 11, 23, 4, 44, 58, tzinfo=timezone.utc).timestamp()
RETURN = datetime(2026, 11, 23, 5, 54, 11, tzinfo=timezone.utc).timestamp()
# no release window within the horizon, so only `return_targets` can wake
TARGET = dict(timezone="Europe/Paris", release_windows=["11:00", "23:00"])


def manual(**over):
    control = dict(host=HOST, mode="draining", automatic=False,
                   actor="human:smichea", return_ts=RETURN)
    control.update(over)
    return control


def automatic(**over):
    return manual(automatic=True, actor="vm-adapter", return_ts=None, **over)


def agent(**over):
    row = dict(name="mty-amh-impl-11", host=HOST, admitted_hosts=[HOST],
               releve="hote", mode="execute", status="idle",
               actionable_lots=0, unread=0, urgent_unread=0,
               has_pending_prompt=False, restart_requested_ts=None)
    row.update(over)
    return row


def targets(control, row):
    controls = {HOST: control, OTHER: {"host": OTHER, "mode": "available"}}
    return host_release.return_targets(control, [row], controls,
                                       known_hosts={HOST, OTHER}, holds={})


def wake(control, row):
    controls = {HOST: control, OTHER: {"host": OTHER, "mode": "available"}}
    return vm.wake_due(HOST, control, [row], [], controls, {}, now=NOW,
                       horizon=900, target=TARGET)


class ReturnTargetsTest(unittest.TestCase):
    def test_dated_manual_drain_ordinary_mail_waits(self):
        row = agent(unread=1)
        self.assertEqual(targets(manual(), row), [])
        self.assertFalse(wake(manual(), row))
        # witness: the same mail wakes the host once the drain is undated
        self.assertEqual(targets(manual(return_ts=None), row), [row["name"]])
        self.assertTrue(wake(manual(return_ts=None), row))

    def test_dated_manual_drain_urgent_mail_wakes(self):
        row = agent(unread=2, urgent_unread=1)
        self.assertEqual(targets(manual(), row), [row["name"]])
        self.assertTrue(wake(manual(), row))

    def test_automatic_drain_any_mail_wakes(self):
        row = agent(unread=1)
        self.assertEqual(targets(automatic(), row), [row["name"]])
        self.assertTrue(wake(automatic(), row))
        # witness: no work, no wake
        self.assertFalse(wake(automatic(), agent()))

    def test_dated_manual_drain_other_work_still_wakes(self):
        for over in (dict(actionable_lots=1), dict(has_pending_prompt=True),
                     dict(restart_requested_ts=NOW - 60)):
            with self.subTest(**over):
                row = agent(unread=1, **over)
                self.assertEqual(targets(manual(), row), [row["name"]])
                self.assertTrue(wake(manual(), row))

    def test_returning_mode_is_not_a_dated_drain(self):
        # `return_at` falls due: storage moves the host to `returning` and
        # clears `return_at`; any mail counts again.
        row = agent(unread=1)
        control = manual(mode="returning", return_ts=None, claims_open=False)
        self.assertEqual(targets(control, row), [row["name"]])


class ListingUrgentUnreadTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM agent_mailbox")
        self.db.execute("INSERT INTO agent_registry (name, status) VALUES ('t8129', 'idle')"
                        " ON CONFLICT DO NOTHING")

    def _mail(self, payload, delivered=False):
        self.db.execute(
            "INSERT INTO agent_mailbox (sender, recipient, body, payload, delivered_at)"
            " VALUES ('test', 't8129', 'x', %s::jsonb, "
            + ("now()" if delivered else "NULL") + ")", (payload,))

    def _row(self):
        return next(r for r in storage.of(self.db).operations.listing()
                    if r["name"] == "t8129")

    def test_urgent_unread_counts_only_undelivered_urgent_mail(self):
        self._mail("{}")
        self._mail('{"info": true}')
        row = self._row()
        self.assertEqual((row["unread"], row["urgent_unread"]), (1, 0))
        self._mail('{"urgent": true}')
        self._mail('{"urgent": true}', delivered=True)
        row = self._row()
        self.assertEqual((row["unread"], row["urgent_unread"]), (2, 1))


if __name__ == "__main__":
    unittest.main()
