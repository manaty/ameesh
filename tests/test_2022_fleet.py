# SPDX-License-Identifier: AGPL-3.0-only
"""#2022: the fleet seen at a glance — `ameesh hosts --fleet` (one line per
host, stale readings, worst disk, zombies, the hosts to look at first) and
`ameesh hosts --publish` (one reading of a host that runs no agent: CI VMs,
the mesh server; 16/10: manaty-mesh-1 unmeasured for 3 days)."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import unittest
from unittest import mock

from ameesh import config as config_mod, mesh_cli, resources, storage

GIB = 1024 ** 3


def reading(host, free, *, age=60.0, zombies=0, now=10_000.0):
    return {"id": 1, "host": host, "sampled_ts": now - age,
            "volumes": [{"path": "/", "paths": ["/"], "disk_free_bytes": free,
                         "disk_total_bytes": 100 * GIB}],
            "procs_total": 300, "procs_zombie": zombies}


class FleetLineTest(unittest.TestCase):
    limits = resources.thresholds(None)

    def test_ok_host(self):
        line = resources.fleet_line(reading("ci-1", 60 * GIB), self.limits, now=10_000.0)
        self.assertTrue(line["ok"])
        self.assertEqual(line["disk"], {"path": "/", "free": 60 * GIB, "used": 0.4})
        self.assertEqual((line["zombies"], line["procs"]), (0, 300))

    def test_stale_disk_and_zombies_are_to_look_at(self):
        stale =resources.fleet_line(reading("mesh-1", 60 * GIB, age=7200),
                                     self.limits, now=10_000.0)
        self.assertTrue(stale["stale"])
        self.assertFalse(stale["ok"])
        full = resources.fleet_line(reading("agents-1", GIB), self.limits, now=10_000.0)
        self.assertIn("min_disk_free", full["breaches"])
        self.assertFalse(full["ok"])
        zombies = resources.fleet_line(reading("ci-3", 60 * GIB, zombies=250), self.limits,
                                       now=10_000.0)
        self.assertFalse(zombies["ok"])


class FleetCommandTest(unittest.TestCase):
    def test_send_rejects_modes_that_would_skip_delivery(self):
        parser = mesh_cli.build_parser()
        for flags in ([], ["--fleet", "--json"], ["--fleet", "--publish"],
                      ["--fleet", "--report"], ["--fleet", "--hold"],
                      ["--fleet", "--release"]):
            with self.subTest(flags=flags), \
                    mock.patch.object(mesh_cli, "_open") as opened, \
                    contextlib.redirect_stderr(io.StringIO()):
                args = parser.parse_args(["hosts", "--send", "bureau"] + flags)
                self.assertEqual(mesh_cli.cmd_hosts(config_mod.Config(), args), 2)
                opened.assert_not_called()

    def run_fleet(self, readings, *, json_out=False, send=None, code=0, err=None):
        store = mock.MagicMock()
        store.hosts.current.return_value = readings
        store.hosts.since.return_value = []
        args = argparse.Namespace(host=None, json=json_out, send=send)
        out = io.StringIO()
        with mock.patch.object(storage, "of", return_value=store), \
                mock.patch.object(mesh_cli.time, "time", return_value=10_000.0), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err or io.StringIO()):
            self.assertEqual(mesh_cli._host_fleet("db", [], args), code)
        return out.getvalue()

    def test_send_mails_the_summary_as_printed(self):
        with mock.patch.object(mesh_cli.mail, "send") as send:
            text = self.run_fleet([reading("ci-1", 60 * GIB)], send=["bureau"])
        send.assert_called_once_with("db", "ameesh", "bureau", text.rstrip("\n"),
                                     kind="notify")

    def test_send_failure_exits_non_zero_and_says_so(self):
        err = io.StringIO()
        with mock.patch.object(mesh_cli.mail, "send",
                               side_effect=[RuntimeError("db down"), 7]) as send:
            self.run_fleet([reading("ci-1", 60 * GIB)], send=["bureau", "owner"],
                           code=1, err=err)
        self.assertEqual(send.call_count, 2)
        self.assertIn("not sent to bureau (RuntimeError: db down)", err.getvalue())

    def test_hosts_to_look_at_first(self):
        text = self.run_fleet([reading("ci-1", 60 * GIB),
                               reading("agents-1", 9 * GIB, zombies=11_000)])
        lines = text.splitlines()
        self.assertEqual(lines[0], "fleet: 2 host(s), 1 to look at")
        self.assertTrue(lines[1].startswith("  agents-1 [!] measured 1m ago; "
                                            "disk / 91 % used, 9.0 GiB free"), lines[1])
        self.assertIn("11000 zombies of 300 processes", lines[1])
        self.assertTrue(lines[2].startswith("  ci-1 ok "), lines[2])

    def test_json(self):
        rows = [json.loads(l) for l in self.run_fleet(
            [reading("ci-1", 60 * GIB, age=7200)], json_out=True).splitlines()]
        self.assertEqual(rows[0]["host"], "ci-1")
        self.assertTrue(rows[0]["stale"])


class PublishTest(unittest.TestCase):
    def args(self, host=None):
        return argparse.Namespace(host=host, json=False, hold=False, release=False,
                                  publish=True, until=None, reason=None)

    def test_publishes_this_host_as_publish_runner(self):
        cfg = config_mod.Config(host="ameesh-ci-3")
        db = mock.Mock()
        seen = {}

        def collect(c, d):
            seen["runner_id"], seen["db"] = c.runner_id, d
            return {"disk_free_bytes": 50 * GIB, "procs_zombie": 99}

        out = io.StringIO()
        with mock.patch.object(mesh_cli, "_open", return_value=db), \
                mock.patch.object(resources, "collect", side_effect=collect), \
                contextlib.redirect_stdout(out):
            self.assertEqual(mesh_cli.cmd_hosts(cfg, self.args()), 0)
        self.assertEqual(seen, {"runner_id": "publish@ameesh-ci-3", "db": db})
        self.assertIn("ameesh-ci-3: reading published (disk 50.0 GiB free, 99 zombies)",
                      out.getvalue())
        db.close.assert_called_once()

    def test_refuses_another_host_or_no_name(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                mock.patch.object(mesh_cli, "_open") as opened:
            self.assertEqual(mesh_cli.cmd_hosts(config_mod.Config(host="a"),
                                                self.args("b")), 2)
            self.assertEqual(mesh_cli.cmd_hosts(config_mod.Config(host=""),
                                                self.args()), 2)
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
