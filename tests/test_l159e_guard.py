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

    def test_lendable_capped_by_the_offer(self):
        # the measure (L159d, or the warm-up) never lends more than the offer
        offer = guard.parse_offer({"cpu_max": 0.5, "memory_max": "4GiB", "disk_max": "10GiB"})
        reading = dict(self.reading, mem_total_bytes=64 * GIB, mem_available_bytes=40 * GIB,
                       app_cpu=1.0, disk_free_bytes=100 * GIB)
        capped = {"cpu": 0.5, "memory_bytes": 4 * GIB, "disk_bytes": 10 * GIB}
        self.assertEqual(guard.lendable(offer, reading), capped)
        with mock.patch.object(guard.resources, "lendable", None, create=True):
            self.assertEqual(guard.lendable(offer, reading), capped)

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


    def _guard(self, clock, readings):
        return guard.Guard(self.cfg, signals=lambda now: dict(readings[0]),
                           clock=lambda: clock[0])

    def _ack(self, **fields):
        os.makedirs(os.path.dirname(self.cfg.ack_path), exist_ok=True)
        with open(self.cfg.ack_path, "w") as fh:
            json.dump(dict({"schema": "ameesh-host-ack/1", "state": "draining"}, **fields), fh)

    def test_ack_holds_for_the_drain_episode(self):
        # codex #211 r1: a capacity change during the drain makes a new seq;
        # the guest's acknowledgement of the drain still holds
        clock = [0.0]
        readings = [dict(self.reading, mem_available_bytes=1 * GIB)]
        g = self._guard(clock, readings)
        drain = g.step()
        self.assertEqual(drain["state"], "draining")
        clock[0] = 115.0
        self._ack(seq=drain["seq"], drained=True, in_turn=[])
        self.assertEqual(g.step()["state"], "draining")
        readings[0]["guest_mem_bytes"] = 1 * GIB      # memory_high_mb falls: new seq
        clock[0] = 118.0
        later = g.step()
        self.assertGreater(later["seq"], drain["seq"])
        self.assertEqual(later["caps"]["memory_high_mb"], 1024)
        clock[0] = 120.0
        self.assertEqual(g.step()["state"], "draining")
        # an acknowledgement from before the episode does not count
        self._ack(seq=drain["seq"] - 1, drained=True)
        clock[0] = 125.0
        self.assertEqual(g.step()["state"], "stopped")
        # nor one of the past episode once a new drain begins
        readings[0]["mem_available_bytes"] = 20 * GIB
        clock[0] = 1000.0
        self.assertEqual(g.step()["level"], 4)        # calm counting
        clock[0] = 1000.0 + self.cfg.offer.calm_seconds
        self.assertEqual(g.step()["level"], 0)
        self._ack(seq=later["seq"], drained=True)
        readings[0]["mem_available_bytes"] = 1 * GIB
        clock[0] += 5
        again = g.step()
        self.assertEqual(again["state"], "draining")
        clock[0] += self.cfg.offer.drain_seconds + guard.STOP_GRACE_S
        self.assertEqual(g.step()["state"], "stopped")

    def test_restart_keeps_level_and_deadline(self):
        # codex #211 r1: drain at t=0 (deadline 90), restart at t=115 under
        # the same pressure: the stop still comes at 120, not 205
        clock = [0.0]
        readings = [dict(self.reading, mem_available_bytes=1 * GIB)]
        first = self._guard(clock, readings).step()
        self.assertEqual(first["drain_deadline_ts"], 90.0)
        clock[0] = 115.0
        g = self._guard(clock, readings)
        held = g.step()
        self.assertEqual((held["state"], held["drain_deadline_ts"]), ("draining", 90.0))
        self.assertEqual(held["guard"]["decision"]["drain_seq"], first["seq"])
        clock[0] = 120.0
        self.assertEqual(self._guard(clock, readings).step()["state"], "stopped")
        # an acknowledgement given before the restart still holds after it
        os.remove(self.cfg.state_path)
        clock[0] = 0.0
        drain = self._guard(clock, readings).step()
        self._ack(seq=drain["seq"], drained=True)
        clock[0] = 200.0
        self.assertEqual(self._guard(clock, readings).step()["state"], "draining")
        # a restart during a freeze keeps its 60 s bound
        os.remove(self.cfg.state_path)
        readings[0] = dict(self.reading, psi_cpu_10=50.0)
        clock[0] = 0.0
        self.assertEqual(self._guard(clock, readings).step()["level"], 2)
        clock[0] = 50.0
        frozen = self._guard(clock, readings).step()
        self.assertEqual(frozen["freeze"], {"since_ts": 0.0, "until_ts": 60.0})
        clock[0] = 60.0
        self.assertEqual(self._guard(clock, readings).step()["state"], "draining")
        # nothing usable in the last state: level 0, seq kept
        for saved in ({"seq": 7}, {"seq": 7, "guard": {"decision": {"level": 9}}},
                      {"seq": 7, "guard": {"decision": {"level": "3", "reason": "x",
                                                        "since_ts": 0}}},
                      {"seq": 7, "guard": {"decision": {"level": 3, "reason": "x",
                                                        "since_ts": 1e12}}}):
            with open(self.cfg.state_path, "w") as fh:
                json.dump(saved, fh)
            restored = self._guard([100.0], [dict(self.reading)])
            self.assertEqual((restored.decision.level, restored.decision.since_ts,
                              restored.seq), (0, 100.0, 7), saved)

    def test_restart_takes_back_a_reset_calm_timer(self):
        # codex #211 r2: level 1 at t=0, calm from t=5, pressure again at
        # t=35 resets the timer; a restart at t=40 must not take back t=5
        clock = [0.0]
        # caps unchanged between the two: only the decision moves
        warn = dict(self.reading, psi_cpu_60=15.0)    # over half of 20 %
        calm = dict(self.reading)
        readings = [warn]
        g = self._guard(clock, readings)
        self.assertEqual(g.step()["level"], 1)
        for t, reading in ((5.0, calm), (30.0, calm), (35.0, warn)):
            clock[0], readings[0] = t, reading
            g.step()
        with open(self.cfg.state_path) as fh:
            self.assertIsNone(json.load(fh)["guard"]["decision"]["calm_since_ts"])
        clock[0], readings[0] = 40.0, calm
        g = self._guard(clock, readings)
        self.assertEqual(g.step()["level"], 1)
        clock[0] = 40.0 + self.cfg.offer.calm_seconds - 5
        self.assertEqual(g.step()["level"], 1)
        clock[0] = 40.0 + self.cfg.offer.calm_seconds
        self.assertEqual(g.step()["level"], 0)

    def test_ack_never_blocks_the_guard(self):
        # codex #211 r3: the guest replaces ack.json with a FIFO and keeps its
        # writer open; a symlink or a huge file is refused too
        import threading
        os.makedirs(os.path.dirname(self.cfg.ack_path))
        os.mkfifo(self.cfg.ack_path)
        writer = os.open(self.cfg.ack_path, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, writer)
        result = []
        reader = threading.Thread(target=lambda: result.append(
            guard.read_ack(self.cfg.ack_path)), daemon=True)
        reader.start()
        reader.join(2.0)
        self.assertFalse(reader.is_alive(), "read_ack blocked on a FIFO")
        self.assertEqual(result, [None])
        os.remove(self.cfg.ack_path)
        target = os.path.join(self.tmp, "elsewhere.json")
        with open(target, "w") as fh:
            json.dump({"schema": "ameesh-host-ack/1", "seq": 3, "drained": True}, fh)
        os.symlink(target, self.cfg.ack_path)
        self.assertIsNone(guard.read_ack(self.cfg.ack_path))
        os.remove(self.cfg.ack_path)
        with open(self.cfg.ack_path, "w") as fh:
            json.dump({"schema": "ameesh-host-ack/1", "seq": 3,
                       "in_turn": ["x" * 1000] * 100}, fh)
        self.assertIsNone(guard.read_ack(self.cfg.ack_path))
        # codex #211 r4: 64 KiB of nested arrays raise a RecursionError on the
        # Pythons whose decoder counts depth (forced here: 3.14 parses them)
        os.remove(self.cfg.ack_path)
        with open(self.cfg.ack_path, "w") as fh:
            fh.write("[" * 32000 + "]" * 32000)
        self.assertIsNone(guard.read_ack(self.cfg.ack_path))
        with mock.patch.object(guard.json, "loads", side_effect=RecursionError):
            self.assertIsNone(guard.read_ack(self.cfg.ack_path))
        self._ack(seq=3, drained=True)
        self.assertEqual(guard.read_ack(self.cfg.ack_path)["seq"], 3)

    def test_measure_keeps_what_the_guard_measured(self):
        # codex #211 r3: L159d leaves busy and app_disk_free_bytes null; the
        # guard's own readings are published
        if getattr(guard.resources, "measure", None) is None:
            self.skipTest("resources.measure comes with L159d")
        reading = dict(self.reading, busy=5, app_disk_free_bytes=GIB)
        state = guard.build_state(self.cfg, guard.Decision(1, "app_busy", 0.0), 1, 100.0,
                                  reading=reading)
        self.assertEqual((state["measure"]["busy"], state["measure"]["app_disk_free_bytes"]),
                         (5, GIB))
        self.assertEqual(state["measure"]["cpu_count"], 8)

    def test_ill_typed_ack_counts_as_none(self):
        # codex #211 r1: the guest writes the acknowledgement; `"in_turn": 1`
        # used to raise a TypeError that killed the guard
        for bad in ({"seq": 3, "in_turn": 1}, {"seq": 3, "in_turn": [1]},
                    {"seq": 3, "in_turn": "a"}, {"seq": True}, {"seq": 3.0},
                    {"seq": 3, "drained": "yes"}, {"seq": 3, "drained": 1},
                    {"seq": 3, "state": 4}):
            self._ack(**bad)
            self.assertIsNone(guard.read_ack(self.cfg.ack_path), bad)
        self._ack(seq=3, drained=True, in_turn=["a", "b"])
        self.assertEqual(guard.read_ack(self.cfg.ack_path)["in_turn"], ["a", "b"])
        clock = [0.0]
        readings = [dict(self.reading, mem_available_bytes=1 * GIB)]
        g = self._guard(clock, readings)
        drain = g.step()
        self._ack(seq=drain["seq"], drained=True, in_turn=1)
        clock[0] = 5.0
        self.assertEqual(g.step()["caps"]["max_concurrent"], 0)   # no TypeError
        clock[0] = 200.0
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


    def test_disk_of_the_guest_work_path(self):
        # codex #211 r4: the guard measured no disk (sample() without a path),
        # so the lendable disk and caps.disk_mb never appeared
        with tempfile.TemporaryDirectory() as tmp:
            cfg = guard.GuardConfig(host="vm-test", offer=guard.parse_offer({"cpu_max": 1}),
                                    work_path=tmp)
            reading = guard.read_signals(cfg, 100.0, psi=lambda avg: {},
                                         busy=lambda units: None)
            self.assertIsNotNone(reading["disk_free_bytes"])
            self.assertEqual(reading["disk_path"], tmp)


class CliTest(unittest.TestCase):
    def test_check_and_observe_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "guard.json")
            with open(config, "w") as fh:
                json.dump({"host": "vm-test", "max_agents": 2,
                           "state_path": os.path.join(tmp, "s", "state.json"),
                           "ack_path": os.path.join(tmp, "a", "ack.json"),
                           "work_path": tmp,
                           "lend": {"cpu_max": 2, "memory_max": "4GiB",
                                    "disk_max": "1MiB"}}, fh)
            with mock.patch("sys.stdout") as out:
                self.assertEqual(guard.main(["check", "--config", config, "--json"]), 0)
            printed = "".join(c.args[0] for c in out.write.call_args_list)
            self.assertEqual(json.loads(printed)["schema"], "ameesh-host-state/1")
            # codex #211 r4: the disk of work_path, measured through sample()
            self.assertEqual(json.loads(printed)["caps"].get("disk_mb"), 1)
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
