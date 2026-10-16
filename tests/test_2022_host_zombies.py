# SPDX-License-Identifier: AGPL-3.0-only
"""#2022: zombies and processes in host readings (0455) — counted by every
runner, for the host and among its own descendants; `host_zombies` raised
at `max_zombies` (16/10: 5 900 to 11 000 zombies found by hand, #1749),
naming the runners that leave them. A warning: no pause."""
from __future__ import annotations

import json
import os
import re
import unittest
from contextlib import nullcontext
from unittest import mock

from ameesh import config, exploitation, notify, resources, storage
from ameesh.storage.postgres import hosts as pg_hosts

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def proc(pid, ppid, zombie=False):
    return {"pid": pid, "ppid": ppid, "pgid": pid, "name": "git", "start": None,
            "zombie": zombie}


class ProcessesTest(unittest.TestCase):
    def test_host_and_runner_counts(self):
        table = [proc(1, 0), proc(10, 1),                      # the runner, pid 10
                 proc(11, 10), proc(12, 10, True), proc(13, 11, True),
                 proc(20, 1), proc(21, 20, True)]              # another runner
        self.assertEqual(resources.processes(10, table), {
            "procs_total": 7, "procs_zombie": 3, "runner_procs": 3, "runner_zombies": 2})

    def test_unreadable_table_is_unmeasured(self):
        with mock.patch("ameesh.platform.process_table",
                        side_effect=OSError("no /proc")):
            self.assertEqual(resources.processes(), dict.fromkeys(resources.PROCESS_FIELDS))

    def test_this_process_by_default(self):
        found = resources.processes()
        if found["procs_total"] is None:
            self.skipTest("no process table on this OS")
        self.assertGreaterEqual(found["procs_total"], 1)
        self.assertGreaterEqual(found["procs_total"], found["runner_procs"])

    def test_collect_publishes_counts_and_runner(self):
        store = mock.MagicMock()
        store.hosts.turns_in_progress.return_value = 0
        store.hosts.lend_basis.return_value = None
        cfg = config.Config(host="pc")
        counts = {"procs_total": 9, "procs_zombie": 4, "runner_procs": 2, "runner_zombies": 1}
        with mock.patch.object(storage, "of", return_value=store), \
                mock.patch.object(resources, "processes", return_value=counts), \
                mock.patch.object(resources, "disk_paths", return_value=[]), \
                mock.patch.object(resources, "tmp_paths", return_value=["/tmp"]):
            reading = resources.collect(cfg, None)
            self.assertEqual(reading["runner_id"], cfg.runner)
            reading = resources.collect(config.Config(host="pc", runner_id="runner-a@pc"), None)
        self.assertEqual(reading["runner_id"], "runner-a@pc")
        self.assertEqual(reading["procs_zombie"], 4)
        self.assertEqual(store.hosts.record.call_args[0][0]["runner_zombies"], 1)


class ThresholdTest(unittest.TestCase):
    def test_default_and_declared(self):
        self.assertEqual(resources.thresholds(None)["max_zombies"], 200)
        self.assertEqual(resources.thresholds({"resources": {"max_zombies": 50}})
                         ["max_zombies"], 50)
        for bad in (0, -3, "many", True, 2.5):
            self.assertEqual(resources.thresholds({"resources": {"max_zombies": bad}})
                             ["max_zombies"], 200, bad)
        self.assertEqual(resources.fmt_value("max_zombies", 200), "200")

    def test_never_a_pause(self):
        limits = resources.thresholds(None)
        self.assertFalse(resources.pressure({"procs_zombie": 10 ** 5}, limits=limits)
                         ["blocked"])


class AlertTest(unittest.TestCase):
    def alerts(self, reading: dict, runners=()) -> list[dict]:
        store = mock.MagicMock()
        store.hosts.current.return_value = [dict({"host": "pc", "sampled_ts": 100.0},
                                                 **reading)]
        store.hosts.since.return_value = []
        store.hosts.runners.return_value = list(runners)
        store.turn_resources.orphans.return_value = []
        store.turn_resources.stale_running.return_value = []
        with mock.patch.object(storage, "of", return_value=store), \
                mock.patch("ameesh.canon.load_configured", return_value=[]), \
                mock.patch.object(exploitation, "_housekeeping_alerts", return_value=[]):
            return exploitation._host_alerts(None, None, now=200.0)

    def test_under_the_threshold_or_unmeasured(self):
        self.assertEqual(self.alerts({"procs_zombie": 199, "procs_total": 500}), [])
        self.assertEqual(self.alerts({"procs_zombie": None}), [])

    def test_names_the_runners(self):
        runners = [{"runner_id": "runner-dev7@pc", "runner_zombies": 1705, "runner_procs": 1720},
                   {"runner_id": "runner-dev2@pc", "runner_zombies": 1407, "runner_procs": 1500},
                   {"runner_id": "runner-ok@pc", "runner_zombies": 0, "runner_procs": 3}]
        found = self.alerts({"procs_zombie": 4580, "procs_total": 5276}, runners)
        self.assertEqual([a["type"] for a in found], ["host_zombies"])
        alert = found[0]
        self.assertIn("host pc: 4580 zombie processes of 5276 (threshold 200)", alert["detail"])
        self.assertIn("under runner-dev7@pc (1705 of 1720), runner-dev2@pc (1407 of 1500)",
                      alert["detail"])
        self.assertNotIn("runner-ok", alert["detail"])
        self.assertTrue(alert["critical"])
        warning = self.alerts({"procs_zombie": 300})[0]
        self.assertFalse(warning["urgent"])
        self.assertEqual(exploitation.alert_key(warning), exploitation.alert_key(alert))

    def test_runners_unreadable_keeps_the_alert(self):
        store = mock.MagicMock()
        store.hosts.runners.side_effect = RuntimeError("db down")
        with mock.patch.object(storage, "of", return_value=store):
            alert = exploitation._zombies_alert(
                None, {"host": "pc", "procs_zombie": 250}, resources.thresholds(None))
        self.assertEqual(alert["runners"], [])

    def test_declared_and_pushed(self):
        self.assertIn("host_zombies", notify.DEFAULT_TYPES)
        self.assertIn("host_zombies", notify.TYPE_LABELS)
        self.assertIn("host_zombies", exploitation.ALERT_TYPES)
        self.assertIn("host_zombies", exploitation.PRIORITY_TYPES)


class StorageTest(unittest.TestCase):
    def test_written_read_and_granted(self):
        db = mock.Mock()
        db.transaction.return_value = nullcontext(db)
        db.query.return_value = [{"host": "pc", "volumes": None}]
        pg_hosts.Hosts(db).record({"host": "pc", "procs_total": 9, "procs_zombie": 4,
                                   "runner_id": "r", "runner_procs": 2, "runner_zombies": 1})
        insert = db.query.call_args_list[-1][0]
        self.assertIn("runner_zombies", insert[0])
        self.assertEqual(list(insert[1][-5:]), [9, 4, "r", 2, 1])
        for column in ("procs_total", "procs_zombie", "runner_id", "runner_procs",
                       "runner_zombies"):
            self.assertIn(column, pg_hosts.READING_COLUMNS)
        pg_hosts.Hosts(db).runners("pc", 900)
        query = db.query.call_args_list[-1][0]
        self.assertIn("DISTINCT ON (runner_id)", query[0])
        self.assertEqual(query[1], ("pc", 900.0))
        with open(os.path.join(REPO, "deploy", "sql", "role-superviseur.sql"),
                  encoding="utf-8") as fh:
            sql = re.sub(r"--[^\n]*", "", fh.read())
        block = re.search(r"\('host_resources', ARRAY\[(.*?)\]\)", sql, re.S).group(1)
        self.assertLessEqual({"procs_total", "procs_zombie", "runner_id", "runner_procs",
                              "runner_zombies"}, set(re.findall(r"'(\w+)'", block)))

    def test_migration_is_additive(self):
        with open(os.path.join(REPO, "src", "ameesh", "migrations",
                               "0455_host_processes.sql"), encoding="utf-8") as fh:
            sql = re.sub(r"--[^\n]*", "", fh.read()).lower()
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        self.assertEqual(len(statements), 5)
        for statement in statements:
            self.assertTrue(statement.startswith(
                "alter table host_resources add column if not exists"), statement)


if __name__ == "__main__":
    unittest.main()
