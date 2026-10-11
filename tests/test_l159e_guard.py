# SPDX-License-Identifier: AGPL-3.0-only
"""L159e — `ameesh guard`, tranche 1: offer, levels, state, observation loop."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import guard

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "dore", "capacity_guard", "levels.json")
GIB = 1024 ** 3


def _epoch(text: str) -> float:
    return dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc).timestamp()


class GoldenLevelsTest(unittest.TestCase):
    """The golden cases shared with the other guards (study §1.9)."""

    def test_cases(self):
        with open(GOLDEN, encoding="utf-8") as fh:
            golden = json.load(fh)
        offer = guard.parse_offer(golden["offer"])
        cfg = guard.GuardConfig(host="vm-test", offer=offer, max_agents=3)
        for case in golden["cases"]:
            decision = guard.Decision(since_ts=0.0)
            for step in case["steps"]:
                signals = dict(golden["calm"], **step.get("signals", {}))
                decision = guard.decide(offer, signals, decision, float(step["t"]),
                                        drained=step.get("drained", False))
                where = "%s, t=%s" % (case["name"], step["t"])
                self.assertEqual(decision.level, step["level"], where)
                if "reason" in step:
                    self.assertEqual(decision.reason, step["reason"], where)
                if "state" in step:
                    state = guard.build_state(cfg, decision, 1, float(step["t"]),
                                              reading=signals)
                    self.assertEqual(state["state"], step["state"], where)


class OfferTest(unittest.TestCase):
    def test_study_offer(self):
        offer = guard.parse_offer({
            "cpu_max": 3, "cpu_weight": 10, "memory_max": "14GiB",
            "agent_memory_max": "4.5GiB", "disk_max": "80GiB",
            "windows": ["mon-fri 18:00-14:00 UTC", "sat-sun"],
            "reclaim": {"memory_available_min": "6GiB", "cpu_pressure_max": "20%",
                        "memory_pressure_max": "2%", "busy_units": ["actions.runner.*"],
                        "busy_max": 4},
            "drain_seconds": 90, "calm_seconds": 600})
        self.assertEqual((offer.cpu_max, offer.memory_max, offer.agent_memory_max),
                         (3.0, 14 * GIB, int(4.5 * GIB)))
        self.assertEqual((offer.cpu_pressure_max, offer.memory_pressure_max), (20.0, 2.0))
        self.assertIsNone(offer.io_pressure_max)
        self.assertEqual((offer.drain_seconds, offer.calm_seconds), (90.0, 600.0))
        self.assertEqual(len(offer.windows), 2)

    def test_invalid_offers(self):
        for lend in (None, {}, {"cpu_max": -1}, {"cpu_max": 1, "memory_max": "lots"},
                     {"cpu_weight": 0}, {"reclaim": {"cpu_pressure_max": "120%"}},
                     {"reclaim": {"busy_units": "x"}}, {"reclaim": {"busy_max": -1}},
                     {"windows": ["someday"]}, {"windows": ["mon 25:00-26:00"]},
                     {"drain_seconds": 0}):
            with self.assertRaises(guard.GuardError, msg=lend):
                guard.parse_offer(lend)

    def test_windows(self):
        night = guard.parse_window("mon-fri 18:00-14:00 UTC")
        weekend = guard.parse_window("sat-sun")
        # Monday 2026-10-12
        self.assertIsNone(night.end_if_open(_epoch("2026-10-12T15:00:00")))
        self.assertEqual(night.end_if_open(_epoch("2026-10-12T19:00:00")),
                         _epoch("2026-10-13T14:00:00"))
        # past midnight: Tuesday morning belongs to Monday's window
        self.assertEqual(night.end_if_open(_epoch("2026-10-13T09:00:00")),
                         _epoch("2026-10-13T14:00:00"))
        # Friday's window runs into Saturday morning
        self.assertEqual(night.end_if_open(_epoch("2026-10-17T10:00:00")),
                         _epoch("2026-10-17T14:00:00"))
        self.assertIsNone(night.end_if_open(_epoch("2026-10-18T10:00:00")))
        # whole days chain: Saturday and Sunday end on Monday 00:00
        self.assertEqual(weekend.end_if_open(_epoch("2026-10-17T10:00:00")),
                         _epoch("2026-10-19T00:00:00"))
        self.assertIsNone(weekend.end_if_open(_epoch("2026-10-16T10:00:00")))
        offer = guard.parse_offer({"windows": ["mon-fri 18:00-14:00 UTC", "sat-sun"]})
        self.assertEqual(guard.raw_level(offer, {}, _epoch("2026-10-12T15:00:00")),
                         (3, "window_end"))
        self.assertEqual(guard.raw_level(offer, {}, _epoch("2026-10-12T19:00:00")),
                         (0, "idle"))
        self.assertEqual(guard.window_end(guard.parse_offer({"cpu_max": 1}), 0.0),
                         (True, None))


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l159e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = guard.GuardConfig(
            host="vm-test", max_agents=3,
            offer=guard.parse_offer({"cpu_max": 3, "memory_max": "14GiB",
                                     "agent_memory_max": "4GiB",
                                     "reclaim": {"memory_available_min": "6GiB",
                                                 "cpu_pressure_max": "20%"}}),
            state_path=os.path.join(self.tmp, "state", "state.json"),
            ack_path=os.path.join(self.tmp, "ack", "ack.json"))
        self.reading = {"cpu_count": 8, "mem_total_bytes": 32 * GIB,
                        "mem_available_bytes": 20 * GIB, "disk_free_bytes": 50 * GIB,
                        "guest_mem_bytes": 2 * GIB, "psi_cpu_10": 1.0, "psi_cpu_60": 1.0}

    def test_caps_per_level(self):
        with mock.patch.object(guard.resources, "lendable", None, create=True), \
                mock.patch.object(guard.resources, "measure", None, create=True):
            ready = guard.build_state(self.cfg, guard.Decision(0, "idle", 0.0), 7, 100.0,
                                      reading=self.reading)
            self.assertEqual(ready["schema"], "ameesh-host-state/1")
            self.assertEqual((ready["state"], ready["seq"], ready["expires_ts"]),
                             ("available", 7, 100.0 + guard.STATE_TTL_S))
            caps = ready["caps"]
            # warm-up: 1 CPU of 8, 25 % of 32 GiB
            self.assertEqual(caps["cpu_share"], 0.125)
            self.assertEqual(caps["memory_mb"], 8 * 1024)
            self.assertEqual((caps["max_concurrent"], caps["agent_memory_mb"]), (3, 4096))
            self.assertEqual(caps["enforced"], [])
            self.assertEqual(ready["guard"]["mode"], "observe")
            reduced = guard.build_state(self.cfg, guard.Decision(1, "app_cpu", 0.0), 8,
                                        100.0, reading=self.reading, in_turn=1)
            self.assertEqual(reduced["caps"]["max_concurrent"], 1)
            self.assertEqual(reduced["caps"]["cpu_share"], 0.0625)
            self.assertEqual(reduced["caps"]["memory_high_mb"], 2048)
            frozen = guard.build_state(self.cfg, guard.Decision(2, "app_cpu", 50.0), 9,
                                       100.0, reading=self.reading)
            self.assertEqual(frozen["freeze"], {"since_ts": 50.0, "until_ts": 110.0})
            drain = guard.build_state(self.cfg, guard.Decision(3, "app_memory", 50.0), 10,
                                      100.0, reading=self.reading, in_turn=2)
            self.assertEqual((drain["state"], drain["drain_deadline_ts"]),
                             ("draining", 140.0))
            self.assertEqual(drain["caps"]["max_concurrent"], 0)
            stop = guard.build_state(self.cfg, guard.Decision(4, "app_memory", 50.0), 11,
                                     100.0, reading=self.reading)
            self.assertEqual((stop["state"], stop["drain_deadline_ts"]), ("stopped", None))

    def test_loop_seq_expiry_and_ack(self):
        clock = [1000.0]
        readings = [dict(self.reading)]
        os.makedirs(os.path.dirname(self.cfg.state_path))
        with open(self.cfg.state_path, "w") as fh:
            json.dump({"schema": "ameesh-host-state/1", "seq": 41}, fh)
        g = guard.Guard(self.cfg, signals=lambda now: dict(readings[0]),
                        clock=lambda: clock[0])
        first = g.step()
        self.assertEqual(first["seq"], 42)          # keeps growing across restarts
        with open(self.cfg.state_path) as fh:
            self.assertEqual(json.load(fh)["seq"], 42)
        self.assertEqual(os.stat(self.cfg.state_path).st_mode & 0o777, 0o644)
        clock[0] += 5
        self.assertEqual(g.step()["seq"], 42)       # nothing changed
        clock[0] += guard.STATE_TTL_S / 3           # rewritten at a third of its life
        os.utime(self.cfg.state_path, ns=(0, 0))
        again = g.step()
        self.assertEqual(again["seq"], 42)
        self.assertNotEqual(os.stat(self.cfg.state_path).st_mtime_ns, 0)
        self.assertGreater(again["expires_ts"], first["expires_ts"])
        # the application takes memory back: drain, new seq
        readings[0]["mem_available_bytes"] = 1 * GIB
        clock[0] += 5
        drain = g.step()
        self.assertEqual((drain["state"], drain["seq"]), ("draining", 43))
        # the guest acknowledges the drain: no stop after the deadline
        os.makedirs(os.path.dirname(self.cfg.ack_path))
        with open(self.cfg.ack_path, "w") as fh:
            json.dump({"schema": "ameesh-host-ack/1", "seq": 43, "state": "draining",
                       "drained": True, "in_turn": []}, fh)
        clock[0] += 200
        self.assertEqual(g.step()["state"], "draining")
        # an acknowledgement of an older seq does not count
        with open(self.cfg.ack_path, "w") as fh:
            json.dump({"schema": "ameesh-host-ack/1", "seq": 12, "state": "draining",
                       "drained": True}, fh)
        clock[0] += 5
        self.assertEqual(g.step()["state"], "stopped")


class SignalsTest(unittest.TestCase):
    def test_psi_files_and_busy_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, some in (("cpu", "avg10=31.50 avg60=12.00 avg300=1.00 total=1"),
                               ("io", "avg10=0.00 avg60=0.50 avg300=0.00 total=1"),
                               ("memory", "avg10=0.10 avg60=0.20 avg300=0.00 total=1")):
                with open(os.path.join(tmp, name), "w") as fh:
                    fh.write("some %s\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n" % some)
            with mock.patch.dict(os.environ, {"AMEESH_PSI_DIR": tmp}), \
                    mock.patch.object(guard.resources, "psi", None, create=True):
                self.assertEqual(guard.read_psi("avg10"),
                                 {"psi_cpu_some": 31.5, "psi_mem_some": 0.1,
                                  "psi_io_some": 0.0})
                self.assertEqual(guard.read_psi("avg60")["psi_cpu_some"], 12.0)
            with mock.patch.dict(os.environ, {"AMEESH_PSI_DIR": os.path.join(tmp, "none")}), \
                    mock.patch.object(guard.resources, "psi", None, create=True):
                self.assertEqual(guard.read_psi("avg10")["psi_cpu_some"], None)

        class Proc:
            returncode, stdout = 0, "actions.runner.a.service loaded active running x\n" \
                                    "actions.runner.b.service loaded active running y\n"
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return Proc()
        self.assertEqual(guard.busy_count(("actions.runner.*",), run=run), 2)
        self.assertIn("actions.runner.*", calls[0])
        self.assertIsNone(guard.busy_count((), run=run))
        self.assertIsNone(guard.busy_count(("x",), run=lambda *a, **k: (_ for _ in ()).throw(
            OSError("no systemctl"))))


class CliTest(unittest.TestCase):
    def test_check_and_observe_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "guard.json")
            with open(config, "w") as fh:
                json.dump({"host": "vm-test", "max_agents": 2,
                           "state_path": os.path.join(tmp, "s", "state.json"),
                           "ack_path": os.path.join(tmp, "a", "ack.json"),
                           "lend": {"cpu_max": 2, "memory_max": "4GiB"}}, fh)
            with mock.patch("sys.stdout") as out:
                self.assertEqual(guard.main(["check", "--config", config, "--json"]), 0)
            printed = "".join(c.args[0] for c in out.write.call_args_list)
            self.assertEqual(json.loads(printed)["schema"], "ameesh-host-state/1")
            with mock.patch("sys.stderr"):
                self.assertEqual(guard.main(["run", "--config", config]), 2)
            self.assertEqual(guard.main(["run", "--observe", "--once", "--config", config]), 0)
            with open(os.path.join(tmp, "s", "state.json")) as fh:
                self.assertEqual(json.load(fh)["guard"]["kind"], "ameesh-guard")
            with open(config, "w") as fh:
                json.dump({"lend": {"cpu_max": "three"}}, fh)
            with mock.patch("sys.stderr"):
                self.assertEqual(guard.main(["check", "--config", config]), 1)


if __name__ == "__main__":
    unittest.main()
