# SPDX-License-Identifier: AGPL-3.0-only
"""L159e6 (#301): the capacity guard's last restart reaches `ameesh alerts`:
the runner copies `guard.restart` of the local state into its readings
(`host_resources.guard_restart`, 0361), `_host_alerts` raises
`guard_restart` for a day."""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from unittest import mock

from ameesh import exploitation, notify, resources, storage
from ameesh.storage.postgres import hosts as pg_hosts

UNIT_A, UNIT_B = "ameesh-guest-runner@a.service", "ameesh-guest-runner@b.service"
RESTART = {"ts": 1000.0, "started": [UNIT_A], "skipped": {UNIT_B: "stopped_by_owner"}}


class ReaderTest(unittest.TestCase):
    def read(self, content) -> dict | None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            if content is not None:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(content if isinstance(content, str) else json.dumps(content))
            return resources.guard_restart(path, now=1060.0)

    def test_the_last_restart_of_the_state(self):
        # with its age on the host's clock (codex #288 r1)
        self.assertEqual(self.read({"schema": "ameesh-host-state/1",
                                    "guard": {"restart": RESTART}}),
                         dict(RESTART, age_s=60.0))

    def test_none_without_a_readable_well_typed_record(self):
        # codex #288 r1: a `guard` that is not an object never raises (the
        # whole reading of the host would be lost)
        for content in (None, "{", [], {"guard": None}, {"guard": {}}, {"guard": "invalid"},
                        {"guard": [1]}, {"guard": {"restart": [1]}},
                        {"guard": {"restart": dict(RESTART, ts=True)}},
                        {"guard": {"restart": dict(RESTART, ts="1000")}},
                        {"guard": {"restart": dict(RESTART, ts=float("inf"))}},
                        {"guard": {"restart": dict(RESTART, started=UNIT_A)}},
                        {"guard": {"restart": dict(RESTART, skipped={UNIT_B: 1})}}):
            self.assertIsNone(self.read(content), content)

    def test_collect_publishes_it(self):
        store = mock.MagicMock()
        store.hosts.turns_in_progress.return_value = 0
        store.hosts.lend_basis.return_value = None
        cfg = mock.Mock(host="vm", worktree_roots=())
        with mock.patch.object(storage, "of", return_value=store), \
                mock.patch.object(resources, "sample", return_value={"host": "vm"}), \
                mock.patch.object(resources, "guard_restart", return_value=RESTART):
            resources.collect(cfg, None)
        self.assertEqual(store.hosts.record.call_args[0][0]["guard_restart"], RESTART)


class AlertTest(unittest.TestCase):
    def alerts(self, reading: dict, now: float) -> list[dict]:
        store = mock.MagicMock()
        store.hosts.current.return_value = [dict({"host": "vm", "sampled_ts": now}, **reading)]
        store.turn_resources.orphans.return_value = []
        store.turn_resources.stale_running.return_value = []
        with mock.patch.object(storage, "of", return_value=store), \
                mock.patch("ameesh.canon.load_configured", return_value=[]), \
                mock.patch.object(exploitation, "_housekeeping_alerts", return_value=[]):
            return [a for a in exploitation._host_alerts(None, None, now=now)
                    if a["type"] == "guard_restart"]

    def test_the_day_is_counted_on_the_host_clock(self):
        # codex #288 r1: a host 5 min ahead, or 2 days behind, of the
        # supervisor: a fresh restart (10 s old on the host when collected,
        # 5 s ago) is raised; one a day old on the host is not
        for skew in (300.0, -2 * 86400.0):
            fresh = dict(RESTART, ts=10000.0 + skew - 10, age_s=10.0)
            found = self.alerts({"guard_restart": fresh, "sampled_ts": 9995.0}, 10000.0)
            self.assertEqual(len(found), 1, skew)
            self.assertEqual(found[0]["since"], round(fresh["ts"], 3))
            old = dict(fresh, age_s=86400.0 - 4)
            self.assertEqual(self.alerts({"guard_restart": old, "sampled_ts": 9995.0},
                                         10000.0), [], skew)

    def test_raised_for_a_day_one_alert_per_restart(self):
        [alert] = self.alerts({"guard_restart": RESTART}, 1060.0)
        self.assertEqual((alert["host"], alert["since"], alert["value"]), ("vm", 1000.0, 1))
        self.assertEqual((alert["started"], alert["skipped"]), ([UNIT_A], {UNIT_B: "stopped_by_owner"}))
        self.assertIn("started again %s" % UNIT_A, alert["detail"])
        self.assertIn("%s (stopped_by_owner)" % UNIT_B, alert["detail"])
        [later] = self.alerts({"guard_restart": RESTART}, 1000.0 + 86399)
        self.assertEqual(exploitation.alert_key(alert), exploitation.alert_key(later))
        [other] = self.alerts({"guard_restart": dict(RESTART, ts=5000.0)}, 5001.0)
        self.assertNotEqual(exploitation.alert_key(alert), exploitation.alert_key(other))
        self.assertEqual(self.alerts({"guard_restart": RESTART}, 1000.0 + 86400), [])
        self.assertEqual(self.alerts({"guard_restart": None}, 1060.0), [])
        self.assertEqual(self.alerts({}, 1060.0), [])

    def test_declared_and_not_pushed_by_default(self):
        self.assertIn("guard_restart", exploitation.ALERT_TYPES)
        self.assertIn("guard_restart", notify.TYPE_LABELS)
        self.assertNotIn("guard_restart", notify.DEFAULT_TYPES)


class StorageTest(unittest.TestCase):
    def test_read_back_as_an_object_and_granted_to_the_supervisor(self):
        db = mock.Mock()
        db.query.return_value = [{"host": "vm", "guard_restart": json.dumps(RESTART)},
                                 {"host": "pc", "guard_restart": None}]
        rows = pg_hosts.Hosts(db).current(None)
        self.assertEqual([r["guard_restart"] for r in rows], [RESTART, None])
        self.assertIn("guard_restart::text AS guard_restart", pg_hosts.READING_COLUMNS)
        pg_hosts.Hosts(db).record({"host": "vm", "guard_restart": RESTART})
        insert = db.query.call_args_list[-1][0]
        self.assertIn("%s::jsonb", insert[0])
        self.assertIn(json.dumps(RESTART), insert[1])
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo, "deploy", "sql", "role-superviseur.sql"),
                  encoding="utf-8") as fh:
            sql = re.sub(r"--[^\n]*", "", fh.read())
        block = re.search(r"\('host_resources', ARRAY\[(.*?)\]\)", sql, re.S).group(1)
        self.assertIn("guard_restart", set(re.findall(r"'(\w+)'", block)))


if __name__ == "__main__":
    unittest.main()
