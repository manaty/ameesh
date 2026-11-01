# SPDX-License-Identifier: AGPL-3.0-only
"""L159e — `ameesh guard`: offer, levels, state, observation loop (tranche 1);
caps and freeze enforced through systemd (tranche 2); drain, stop and manual
drain (tranche 3)."""
from __future__ import annotations

import dataclasses
import io
import datetime as dt
import json
import math
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import guard
from ameesh.platform import host

from .test_l159a_capacity_guard import errors as schema_errors
from .test_l159a_capacity_guard import schema

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "dore", "capacity_guard", "levels.json")
GIB = 1024 ** 3
#: the three caps every offer must carry (contract §3), large enough to
#: leave the computations alone
CAPS = {"cpu_max": 8, "memory_max": "1TiB", "disk_max": "10TiB"}


def lend(**fields) -> dict:
    return dict(CAPS, **fields)


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
        bad = [{"cpu_max": -1}, {"memory_max": "lots"},
               {"cpu_weight": 0}, {"reclaim": {"cpu_pressure_max": "120%"}},
               {"reclaim": {"busy_units": 3}}, {"reclaim": {"busy_max": -1}},
               {"windows": ["someday"]}, {"windows": ["mon 25:00-26:00"]},
               {"drain_seconds": 0},
               # codex #211 r5: non-finite times kept the guard draining forever
               {"drain_seconds": 1e309}, {"drain_seconds": float("nan")},
               {"calm_seconds": float("inf")}, {"calm_seconds": 10 ** 400},
               {"cpu_max": float("nan")}, {"drain_seconds": True},
               {"reclaim": {"io_pressure_max": "120%"}},
               {"reclaim": {"disk_paths": "/"}},
               # codex #459 r1: the reader refuses what the guard used to fix up
               {"reclaim": {"disk_free_min": 0}},
               {"reclaim": {"io_pressure_max": None}},
               # L159e4: whole seconds >= 0
               {"restart_after_seconds": -1}, {"restart_after_seconds": 1.5},
               {"restart_after_seconds": True}, {"restart_after_seconds": "600"}]
        # mesh-design #459 r4c: any offer problem closes the guard instead of
        # stopping it — level 3, nothing published, nothing lendable
        for offer in [{}, {"cpu_max": 1}] + [lend(**b) for b in bad]:
            with self.subTest(offer=offer):
                with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                    got = guard.parse_offer(offer)
                self.assertEqual(got.closed, "offer_invalid", offer)
                self.assertEqual(got.published, {}, offer)
                self.assertEqual(guard.raw_level(got, {}, 1000.0),
                                 (3, "offer_invalid"), offer)
                self.assertIn("lends nothing", err.getvalue(), offer)
        # an absent offer closes it too, with its own reason; the message names
        # the key of every problem
        for offer, reason in ((None, "no_offer"),
                              (lend(reclaim={"disk_paths": ["../etc"]}), "offer_invalid"),
                              (lend(reclaim={"disk_paths": ["/\x00"]}), "offer_invalid"),
                              (lend(reclaim={"disk_paths": ["/srv", "rel/x"]}),
                               "offer_invalid")):
            with self.subTest(offer=offer):
                with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                    got = guard.parse_offer(offer)
                text = err.getvalue()
                self.assertEqual(got.closed, reason)
                self.assertEqual(got.published, {})
                self.assertEqual(guard.raw_level(got, {}, 1000.0), (3, reason))
                self.assertIn("lends nothing", text)
                if offer is not None:
                    self.assertIn("disk_paths", text)
        # codex #459 r3: a path named twice (`/` and `/`) is collapsed, not
        # refused — the guard starts on an offer main accepted
        offer = guard.parse_offer(lend(reclaim={"disk_paths": ["/", "/"]}))
        self.assertEqual(offer.disk_paths, ("/",))
        self.assertIsNone(offer.closed)
        self.assertTrue(offer.published)

    def test_config_refuses_duplicate_json_keys(self):
        # codex #459 r1: a duplicate JSON key silently overwrote ('10GiB'
        # then 0: disk reclaim disabled); the config reader refuses
        # duplicates, as the canon's YAML reader does
        doc = ('{"host": "vm", "lend": {"cpu_max": 1, "memory_max": "1GiB",'
               ' "disk_max": "1GiB", "reclaim": {"disk_free_min": "10GiB",'
               ' "disk_free_min": 0}}}')
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "guard.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(doc)
            with self.assertRaises(guard.GuardError) as caught:
                guard.load_config(path)
            self.assertIn("duplicate key", str(caught.exception))
            self.assertIn("disk_free_min", str(caught.exception))
            # the same key once: the config loads, with the value read
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(doc.replace(', "disk_free_min": 0', ""))
            self.assertEqual(guard.load_config(path).offer.disk_free_min, 10 * GIB)

    def test_exactly_the_offers_of_the_contract(self):
        # codex #211 r6: the guard reads the offer with the contract's reader;
        # every golden offer the canon accepts, the guard accepts, and the
        # other way round
        with open(os.path.join(HERE, "dore", "capacity_guard", "offer.json")) as fh:
            cases = json.load(fh)["cases"]
        for case in cases:
            with self.subTest(case["why"]):
                if "offer" in case:
                    offer = guard.parse_offer(case["lend"])
                    self.assertEqual(offer.published, case["offer"])
                    self.assertIsNone(offer.closed)
                else:
                    # mesh-design #459 r4c: the canon still refuses the offer,
                    # the guard no longer stops on it — it starts closed
                    with mock.patch("sys.stderr", new_callable=io.StringIO):
                        offer = guard.parse_offer(case["lend"])
                    self.assertEqual(offer.closed, "offer_invalid")
                    self.assertEqual(offer.published, {})
        # overnight windows on a list of days, a calm of 0, a zone
        offer = guard.parse_offer(lend(windows=["mon,wed 18:00-14:00 UTC",
                                                "sun 18:00-20:00 Europe/Paris"],
                                       calm_seconds=0))
        self.assertEqual(offer.calm_seconds, 0.0)
        # Wednesday 2026-10-14 23:00 UTC: Wednesday's night runs to Thursday 14:00
        self.assertEqual(guard.window_end(offer, _epoch("2026-10-14T23:00:00")),
                         (True, _epoch("2026-10-15T14:00:00")))
        self.assertEqual(guard.window_end(offer, _epoch("2026-10-13T15:00:00"))[0], False)
        # Sunday 17:00 UTC is 19:00 in Paris (summer time): open until 18:00 UTC
        self.assertEqual(guard.window_end(offer, _epoch("2026-10-18T17:00:00")),
                         (True, _epoch("2026-10-18T18:00:00")))
        # the guard's own reclaim keys are published with the offer
        offer = guard.parse_offer(lend(reclaim={"io_pressure_max": "20%",
                                                "disk_free_min": "2GiB"}))
        self.assertEqual(offer.published["reclaim"],
                         {"io_pressure_max": 20.0, "disk_free_min": 2 * GIB})

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
        offer = guard.parse_offer(lend(windows=["mon-fri 18:00-14:00 UTC", "sat-sun"]))
        self.assertEqual(guard.raw_level(offer, {}, _epoch("2026-10-12T15:00:00")),
                         (3, "window_end"))
        self.assertEqual(guard.raw_level(offer, {}, _epoch("2026-10-12T19:00:00")),
                         (0, "idle"))
        self.assertEqual(guard.window_end(guard.parse_offer(lend()), 0.0),
                         (True, None))

    def test_windows_across_dst(self):
        # L159e5 (codex #211 r7): the repeated hour of 2026-10-25 in Paris;
        # the window is open once, at the first 02:00-02:45 (00:00-00:45Z)
        dst = guard.parse_window("sun 02:00-02:45 Europe/Paris")
        self.assertEqual(dst.end_if_open(_epoch("2026-10-25T00:15:00")),
                         _epoch("2026-10-25T00:45:00"))
        self.assertIsNone(dst.end_if_open(_epoch("2026-10-25T01:15:00")))
        # the skipped hour of 2026-03-29 (02:00-03:00 Paris = 01:00Z): a
        # range wholly in it never opens; an end in it is its start (codex
        # #276 r1: both landed an hour later)
        for t in range(int(_epoch("2026-03-28T23:00:00")), int(_epoch("2026-03-29T03:00:00")), 60):
            self.assertIsNone(dst.end_if_open(t), t)
        night = guard.parse_window("sat 23:30-02:30 Europe/Paris")
        self.assertEqual(night.end_if_open(_epoch("2026-03-28T23:00:00")),
                         _epoch("2026-03-29T01:00:00"))
        self.assertIsNone(night.end_if_open(_epoch("2026-03-29T01:00:00")))
        self.assertEqual(night.end_if_open(_epoch("2026-03-28T22:30:00")),
                         _epoch("2026-03-29T01:00:00"))
        self.assertIsNone(night.end_if_open(_epoch("2026-03-28T22:29:59")))
        # a start in the gap opens at its end; whole days end at midnight
        late = guard.parse_window("sun 02:30-04:00 Europe/Paris")
        self.assertIsNone(late.end_if_open(_epoch("2026-03-29T00:59:59")))
        self.assertEqual(late.end_if_open(_epoch("2026-03-29T01:00:00")),
                         _epoch("2026-03-29T02:00:00"))
        self.assertEqual(guard.parse_window("sun Europe/Paris").end_if_open(
            _epoch("2026-03-29T12:00:00")), _epoch("2026-03-29T22:00:00"))
        # every open instant around both changes of 2026 ends in the future
        windows = [guard.parse_window(w) for w in (
            "sun 02:00-02:45 Europe/Paris", "sat-sun 23:30-02:30 Europe/Paris",
            "sun Europe/Paris", "sat-sun America/New_York",
            "sun 01:00-03:00 America/New_York")]
        for day in ("2026-03-28", "2026-03-29", "2026-10-24", "2026-10-25",
                    "2026-11-01"):
            start = _epoch(day + "T00:00:00") - 6 * 3600
            for t in range(int(start), int(start) + 36 * 3600, 300):
                for w in windows:
                    end = w.end_if_open(t)
                    self.assertTrue(end is None or end > t, (w, t, end))


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l159e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = guard.GuardConfig(
            host="vm-test", max_agents=3,
            offer=guard.parse_offer({"cpu_max": 3, "memory_max": "14GiB", "disk_max": "10TiB",
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

    def test_states_match_the_schema(self):
        # codex #211 r6: the published offer in bytes, cpu_share > 0; every
        # level checked against docs/schemas/ameesh-host-state-1.schema.json
        doc = schema("ameesh-host-state-1.schema.json")
        with mock.patch.object(guard.resources, "lendable", None, create=True), \
                mock.patch.object(guard.resources, "measure", None, create=True):
            for level in range(5):
                with self.subTest(level=level):
                    state = guard.build_state(self.cfg, guard.Decision(level, "app_cpu", 50.0),
                                              7, 100.0, reading=self.reading, in_turn=1)
                    self.assertEqual(schema_errors(doc, state), [])
            self.assertEqual(set(guard.MEASURE_KEYS) | {"lendable"},
                             set(doc["properties"]["measure"]["required"]))
            self.assertEqual((state["offer"]["memory_max"], state["offer"]["disk_max"]),
                             (14 * GIB, 10 * 1024 * GIB))
            # 0.001 CPU of 64: the smallest share, not 0.0
            cfg = dataclasses.replace(self.cfg, offer=guard.parse_offer(lend(cpu_max=0.001)))
            reading = dict(self.reading, cpu_count=64)
            for level in (0, 1):
                state = guard.build_state(cfg, guard.Decision(level, "idle", 0.0), 1, 100.0,
                                          reading=reading)
                self.assertEqual(state["caps"]["cpu_share"], guard.CPU_SHARE_MIN)
                self.assertEqual(schema_errors(doc, state), [])
        self.assertEqual(guard.cpu_share(0.125), 0.125)
        self.assertEqual(guard.cpu_share(0.33333), 0.3334)
        self.assertEqual(guard.cpu_share(3.0), 1.0)

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

    def test_stop_holds_while_the_breach_does(self):
        # codex #211 r7: memory steadily 1 GiB under the 6 GiB minimum, drain
        # at t=0, stop at t=120; a pressure that never cleared is no calm, so
        # no fresh drain (and deadline) 600 s later: stopped until it clears
        clock = [0.0]
        readings = [dict(self.reading, mem_available_bytes=5 * GIB)]
        g = self._guard(clock, readings)
        self.assertEqual(g.step()["state"], "draining")
        clock[0] = 120.0
        self.assertEqual(g.step()["state"], "stopped")
        while clock[0] < 120.0 + 3 * self.cfg.offer.calm_seconds:
            clock[0] += 5
            state = g.step()
            self.assertEqual((state["level"], state["state"]), (4, "stopped"), clock[0])
        # the breach gone: calm counts from there
        readings[0]["mem_available_bytes"] = 20 * GIB
        clock[0] += 5
        cleared = clock[0]
        self.assertEqual(g.step()["level"], 4)
        clock[0] = cleared + self.cfg.offer.calm_seconds
        self.assertEqual(g.step()["level"], 0)

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
                                                        "since_ts": 1e12}}},
                      {"seq": 7, "guard": {"decision": {"level": 3, "reason": "x",
                                                        "since_ts": -10 ** 400}}},
                      {"seq": 7, "guard": {"decision": {"level": 3, "reason": "x",
                                                        "since_ts": float("-inf")}}},
                      {"seq": 7, "guard": {"decision": {"level": 1, "reason": "x",
                                                        "since_ts": 0,
                                                        "calm_since_ts": float("nan")}}}):
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

    def test_busy_tasks_count_jobs_not_idle_listeners(self):
        # #1256 R1 : quatre écouteurs persistants restent `active` sans job ;
        # avec `busy_tasks`, une unité ne compte que si une tâche de SON
        # cgroup correspond (le worker du job). Un cgroup illisible compte :
        # un job qu'on ne peut pas exclure ne fait pas gagner l'invité.
        with tempfile.TemporaryDirectory() as tmp:
            cg = os.path.join(tmp, "cg")
            proc = os.path.join(tmp, "proc")

            def unit_cgroup(name, pids):
                d = os.path.join(cg, "system.slice", name)
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "cgroup.procs"), "w") as fh:
                    fh.write("".join("%s\n" % p for p in pids))

            def task(pid, argv):
                d = os.path.join(proc, str(pid))
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "cmdline"), "wb") as fh:
                    fh.write(b"\0".join(a.encode() for a in argv) + b"\0")

            unit_cgroup("actions.runner.a.service", [10])          # écouteur idle
            task(10, ["/opt/actions-runner/bin/Runner.Listener", "run"])
            unit_cgroup("actions.runner.b.service", [20, 21])      # un job
            task(20, ["/opt/actions-runner/bin/Runner.Listener", "run"])
            task(21, ["/opt/actions-runner/bin/Runner.Worker", "spawnclient", "1", "2"])
            # c : pas de cgroup.procs -> illisible -> compte

            class Proc:
                def __init__(self, out):
                    self.returncode, self.stdout = 0, out

            def run(argv, **kw):
                if argv[:2] == ["systemctl", "list-units"]:
                    return Proc("actions.runner.a.service loaded active running x\n"
                                "actions.runner.b.service loaded active running y\n"
                                "actions.runner.c.service loaded active running z\n")
                if argv[:2] == ["systemctl", "show"]:
                    return Proc("/system.slice/%s\n" % argv[2])
                raise AssertionError(argv)

            # sans motif de tâche : les trois unités actives comptent (l'ancien
            # comportement, juste quand le job EST l'unité)
            self.assertEqual(guard.busy_count(("actions.runner.*",), run=run), 3)
            # avec : seul b (job) et c (illisible) comptent
            self.assertEqual(guard.busy_count(("actions.runner.*",), run=run,
                                              tasks=("Runner.Worker",),
                                              cgroup_root=cg, proc_root=proc), 2)

        # le témoin demandé par la R1 : quatre écouteurs en ligne et au repos
        # pèsent zéro job ; les mêmes quatre unités avec un worker chacune en
        # pèsent quatre (c'est le seuil `busy_max` de la fiche d'hôte qui
        # décide, pas le nombre d'écouteurs)
        with tempfile.TemporaryDirectory() as tmp:
            cg = os.path.join(tmp, "cg")
            proc = os.path.join(tmp, "proc")
            procs = [100 + n for n in range(1, 5)]

            def run4(argv, **kw):
                if argv[:2] == ["systemctl", "list-units"]:
                    return Proc("".join("actions.runner.n%d.service loaded active running x\n" % n
                                        for n in range(1, 5)))
                if argv[:2] == ["systemctl", "show"]:
                    return Proc("/system.slice/%s\n" % argv[2])
                raise AssertionError(argv)

            for n, pid in zip(range(1, 5), procs):
                d = os.path.join(cg, "system.slice", "actions.runner.n%d.service" % n)
                os.makedirs(d)
                with open(os.path.join(d, "cgroup.procs"), "w") as fh:
                    fh.write("%s\n" % pid)
                p = os.path.join(proc, str(pid))
                os.makedirs(p)
                with open(os.path.join(p, "cmdline"), "wb") as fh:
                    fh.write(b"/opt/actions-runner/bin/Runner.Listener\0run\0")
            self.assertEqual(guard.busy_count(("actions.runner.*",), run=run4,
                                              tasks=("Runner.Worker",),
                                              cgroup_root=cg, proc_root=proc), 0)
            for n, pid in zip(range(1, 5), [200 + n for n in range(1, 5)]):
                p = os.path.join(proc, str(pid))
                os.makedirs(p)
                with open(os.path.join(p, "cmdline"), "wb") as fh:
                    fh.write(b"/opt/actions-runner/bin/Runner.Worker\0spawnclient\0")
                d = os.path.join(cg, "system.slice", "actions.runner.n%d.service" % n)
                with open(os.path.join(d, "cgroup.procs"), "a") as fh:
                    fh.write("%s\n" % pid)
            self.assertEqual(guard.busy_count(("actions.runner.*",), run=run4,
                                              tasks=("Runner.Worker",),
                                              cgroup_root=cg, proc_root=proc), 4)


    def test_busy_tasks_unreadable_cmdline_counts_the_unit(self):
        # #1256 R1 delta B3 : un PID encore listé dont la ligne de commande
        # n'est pas lisible (permissions, erreur transitoire) n'est pas une
        # tâche exclue : l'unité compte, comme un cgroup illisible. Un PID
        # vraiment disparu entre les deux lectures, lui, ne cache rien.
        with tempfile.TemporaryDirectory() as tmp:
            cg = os.path.join(tmp, "cg")
            proc = os.path.join(tmp, "proc")
            d = os.path.join(cg, "system.slice", "actions.runner.x.service")
            os.makedirs(d)

            class Proc:
                def __init__(self, out):
                    self.returncode, self.stdout = 0, out

            def run(argv, **kw):
                if argv[:2] == ["systemctl", "list-units"]:
                    return Proc("actions.runner.x.service loaded active running x\n")
                if argv[:2] == ["systemctl", "show"]:
                    return Proc("/system.slice/actions.runner.x.service\n")
                raise AssertionError(argv)

            # le PID 30 est encore là (son /proc/30 existe) mais sa cmdline
            # est absente : inconnu -> l'unité compte, jamais zéro
            with open(os.path.join(d, "cgroup.procs"), "w") as fh:
                fh.write("30\n")
            os.makedirs(os.path.join(proc, "30"))
            self.assertEqual(guard.busy_count(("actions.runner.*",), run=run,
                                              tasks=("Runner.Worker",),
                                              cgroup_root=cg, proc_root=proc), 1)

            # le PID 31 a entièrement disparu entre les deux lectures : la
            # tâche est terminée, pas cachée -> zéro job
            with open(os.path.join(d, "cgroup.procs"), "w") as fh:
                fh.write("31\n")
            self.assertEqual(guard.busy_count(("actions.runner.*",), run=run,
                                              tasks=("Runner.Worker",),
                                              cgroup_root=cg, proc_root=proc), 0)

    def test_disk_of_the_guest_work_path(self):
        # codex #211 r4: the guard measured no disk (sample() without a path),
        # so the lendable disk and caps.disk_mb never appeared
        with tempfile.TemporaryDirectory() as tmp:
            cfg = guard.GuardConfig(host="vm-test", offer=guard.parse_offer(lend()),
                                    work_path=tmp)
            reading = guard.read_signals(cfg, 100.0, psi=lambda avg: {},
                                         busy=lambda units: None)
            self.assertIsNotNone(reading["disk_free_bytes"])
            self.assertEqual(reading["disk_path"], tmp)


class EnforcerTest(unittest.TestCase):
    """Tranche 2: the caps become slice and unit properties, level 2 freezes."""

    UNIT = "ameesh-guard-freeze-ameesh-guest.service"

    def setUp(self):
        self.cfg = guard.GuardConfig(host="vm-test", offer=guard.parse_offer(lend(cpu_max=1)))
        self.calls = []
        self.failing = set()     # verbs that fail without acting
        self.lying = set()       # verbs that act, then report a failure (timeout)
        self.units = "ameesh-guest-runner@a.service loaded active running a\n" \
                     "● ameesh-guest-runner@b.service loaded failed failed b\n"
        self.logged = []
        self.now = [1000.0]      # the guard's wall clock
        self.mono = [0.0]        # systemd's monotonic clock
        self.cost = {}           # seconds a command takes before it acts: verb -> s
        self.frozen = set()      # frozen slices
        self.services = {}       # freezing service -> [slice, thaw at, backstop at] (mono)
        self.freeze_fails = False  # the service's `systemctl freeze` fails
        self.enforcer = self.make(self.cfg)

    def make(self, cfg, **kw):
        """An enforcer on the fake systemd: the guard runs in system.slice,
        the boot clock is systemd's, its sleep lets time pass."""
        options = dict(run=self.fake, log=self.logged.append, clock=lambda: self.now[0],
                       cgroup="/system.slice/ameesh-guard.service",
                       boottime=lambda: self.mono[0], boot_id=lambda: "boot-1",
                       sleep=self.tick)
        options.update(kw)
        return guard.Enforcer(cfg, **options)

    def at(self, now):
        self.now[0] = now
        return self.enforcer

    def tick(self, seconds, wall=None):
        """Time passes: systemd's clock by `seconds`, the wall clock by `wall`
        (the same by default); a freezing service thaws at its end."""
        self.mono[0] += seconds
        self.now[0] += seconds if wall is None else wall
        for unit, (name, thaw_at, backstop) in list(self.services.items()):
            if self.mono[0] >= min(thaw_at, backstop):
                self.frozen.discard(name)
                del self.services[unit]

    def fake(self, argv, **kw):
        """systemctl and systemd-run, as far as caps and the freeze go."""
        self.calls.append(argv)
        verb = self.verb(argv)
        self.tick(self.cost.get(verb, 0))          # dispatch: the command acts late
        if verb in self.failing:
            return mock.Mock(returncode=1, stdout="", stderr="denied")
        out, code = "", 0
        if verb == "list-units":
            out = self.units
        elif verb == "show":
            out = "frozen\n" if argv[-1] in self.frozen else "running\n"
        elif verb == "systemd-run":
            unit = argv[1].split("=", 1)[1]
            props = dict(a.split("=", 1)[1].split("=", 1) for a in argv
                         if a.startswith("--property="))
            name, bound, seconds = argv[-3], argv[-2], int(argv[-1])
            self.assertEqual(argv[-6:-3], ["/bin/sh", "-c", guard.FREEZE_SCRIPT])
            self.assertEqual(props["ExecStopPost"], "systemctl thaw " + name)
            self.assertIn("--slice=" + guard.HELPER_SLICE, argv)
            if unit in self.services:
                code = 1                              # the unit already exists
            elif self.freeze_fails:
                pass                                  # freeze fails, the thaw loop exits 0
            else:
                self.frozen.add(name)                 # the script freezes, then sleeps
                # the script sleeps until the bound on the boot clock
                self.services[unit] = [name, float(bound) if bound else self.mono[0] + seconds,
                                       self.mono[0] + int(props["RuntimeMaxSec"])]
        elif verb == "stop" and argv[2].startswith(guard.FREEZE_UNIT):
            if argv[2] in self.services:              # ExecStopPost= thaws
                self.frozen.discard(self.services.pop(argv[2])[0])
            else:
                code = 5
        elif verb == "freeze":
            self.frozen.add(argv[2])
        elif verb == "thaw":
            self.frozen.discard(argv[2])
        if verb in self.lying:
            code = 1
        return mock.Mock(returncode=code, stdout=out, stderr="denied" if code else "")

    @staticmethod
    def verb(argv):
        return argv[1] if argv[0] == "systemctl" else argv[0]

    def freezes(self):
        return [c for c in self.calls if c[1] in ("freeze", "thaw")]

    def runs(self):
        return [c for c in self.calls if c[0] == "systemd-run"]

    def sets(self):
        return [c[3:] for c in self.calls if c[1] == "set-property"]

    def test_slice_properties(self):
        self.assertEqual(
            guard.slice_properties({"cpu_weight": 10, "io_weight": 10, "cpu_share": 0.125,
                                    "memory_high_mb": 2048, "memory_mb": 8192}, 8),
            {"CPUWeight": "10", "IOWeight": "10", "CPUQuota": "100%",
             "MemoryHigh": "2048M", "MemoryMax": "8192M"})
        # no CPU count: no quota (a share of nothing)
        self.assertNotIn("CPUQuota", guard.slice_properties({"cpu_share": 0.5}, None))
        self.assertEqual(guard.slice_properties({}, 8), {})

    def test_apply_changes_only_and_freeze(self):
        caps = {"cpu_weight": 10, "io_weight": 10, "cpu_share": 0.25, "memory_mb": 4096,
                "memory_high_mb": 4096, "agent_memory_mb": 1024, "max_concurrent": 2}
        done = self.enforcer.apply({"caps": dict(caps)}, 4)
        self.assertEqual(done, ["agent_memory_mb", "cpu_share", "cpu_weight", "io_weight",
                                "memory_high_mb", "memory_mb"])
        self.assertEqual(self.sets(), [
            ["ameesh-guest.slice", "CPUQuota=100%", "CPUWeight=10", "IOWeight=10",
             "MemoryHigh=4096M", "MemoryMax=4096M"],
            ["ameesh-guest-runner@a.service", "MemoryMax=1024M"],
            ["ameesh-guest-runner@b.service", "MemoryMax=1024M"]])
        # level 1: only the changed properties are sent
        self.calls.clear()
        reduced = dict(caps, cpu_share=0.125, memory_high_mb=1024)
        self.at(10.0).apply({"caps": reduced}, 4)
        self.assertEqual(self.sets(), [["ameesh-guest.slice", "CPUQuota=50%",
                                        "MemoryHigh=1024M"]])
        # level 2: frozen by its service, started once
        self.calls.clear()
        frozen = {"caps": reduced, "freeze": {"since_ts": 1.0, "until_ts": 61.0}}
        self.assertIn("freeze", self.at(10.0).apply(frozen, 4))
        self.at(10.0).apply(frozen, 4)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.frozen, {"ameesh-guest.slice"})
        # the level drops: the service stopped, the slice thawed
        self.calls.clear()
        self.assertNotIn("freeze", self.at(10.0).apply({"caps": reduced}, 4))
        self.assertEqual([c[1:] for c in self.calls if c[1] in ("stop", "thaw")],
                         [["stop", self.UNIT], ["thaw", "ameesh-guest.slice"]])
        self.assertEqual((self.frozen, self.services), (set(), {}))

    def test_failures_are_not_enforced(self):
        self.failing = {"set-property", "systemd-run"}
        caps = {"cpu_weight": 10, "agent_memory_mb": 1024}
        state = {"caps": caps, "freeze": {"since_ts": 1.0, "until_ts": 61.0}}
        self.assertEqual(self.at(10.0).apply(state, 4), [])
        self.assertTrue(any("denied" in line for line in self.logged))
        self.assertEqual(self.frozen, set())
        # retried at the next evaluation, and in force once it works
        self.failing = set()
        self.calls.clear()
        self.assertEqual(self.at(10.0).apply(state, 4),
                         ["agent_memory_mb", "cpu_weight", "freeze"])
        self.assertIn(["ameesh-guest.slice", "CPUWeight=10"], self.sets())
        # no agent unit loaded: the per-agent cap is not in force
        self.units = ""
        self.assertNotIn("agent_memory_mb", self.at(10.0).apply({"caps": caps}, 4))
        # systemctl missing: nothing in force, nothing raised
        broken = guard.Enforcer(self.cfg, log=self.logged.append, clock=lambda: 10.0,
                                run=lambda *a, **k: (_ for _ in ()).throw(OSError("none")))
        self.assertEqual(broken.apply(state, 4), [])

    def test_freeze_held_by_one_systemd_service(self):
        # codex #237 r4: the guard never freezes the slice itself; a service
        # freezes it, sleeps until the bound and thaws it, guard alive or not
        frozen = {"caps": {}, "freeze": {"since_ts": 1000.0, "until_ts": 1060.0}}
        self.assertIn("freeze", self.at(1010.0).apply(frozen, 4))
        run = self.runs()
        self.assertEqual(run, [[
            "systemd-run", "--unit=" + self.UNIT, "--slice=ameesh-guard-helper.slice",
            "--collect", "--quiet", "--property=RuntimeMaxSec=80",
            "--property=ExecStopPost=systemctl thaw ameesh-guest.slice",
            "/bin/sh", "-c", guard.FREEZE_SCRIPT, "ameesh-guest.slice", "50.000", "50"]])
        self.assertNotIn("freeze", [c[1] for c in self.calls if c[0] == "systemctl"])
        # the guard is killed: the slice thaws at the bound all the same
        self.tick(49)
        self.assertEqual(self.frozen, {"ameesh-guest.slice"})
        self.tick(1)
        self.assertEqual((self.frozen, self.services), (set(), {}))
        # past the bound, or under a second left: no freeze
        fresh = self.make(self.cfg)
        self.calls.clear()
        self.now[0] = 1059.5
        self.assertNotIn("freeze", fresh.apply(frozen, 4))
        self.assertEqual((self.runs(), self.frozen), ([], set()))

    def test_no_delay_leaves_a_freeze_past_its_bound(self):
        # codex #237 r4 (P1): bound 1060, evaluation at 1055; the cleanup and
        # the dispatch of systemd-run take 7 s each: the freeze starts late,
        # and still lasts 5 s at most, guard killed or not
        frozen = {"caps": {}, "freeze": {"since_ts": 1000.0, "until_ts": 1060.0}}
        self.cost = {"stop": 7, "reset-failed": 7, "systemd-run": 7}
        self.at(1040.0).apply(frozen, 4)
        # L159e5 (codex #237 r6): what is left is counted after the cleanup
        # (1054: 6 s), and the service sleeps until the bound on the boot
        # clock, not 6 s from its late start
        self.assertEqual(self.runs()[0][-2:], ["20.000", "6"])
        self.assertGreaterEqual(self.mono[0], 21.0)  # started past its bound
        self.tick(0)
        self.assertEqual((self.frozen, self.services), (set(), {}))
        # a bound already past once cleaned up: no freeze at all
        self.cost = {"stop": 7}
        self.calls.clear()
        self.at(0.0).apply({"caps": {}}, 4)
        self.at(1055.0).apply(frozen, 4)
        self.assertEqual((self.runs(), self.frozen), ([], set()))
        # (P1) the wall clock goes back: the freeze lasts what systemd's
        # clock says, and never more than the longest freeze (a new episode)
        self.cost = {}
        self.calls.clear()
        self.at(0.0).apply({"caps": {}}, 4)
        frozen = {"caps": {}, "freeze": {"since_ts": 0.0, "until_ts": 1060.0}}
        self.at(0.0).apply(frozen, 4)
        self.assertEqual(self.runs()[0][-1], "60")
        self.assertIn("--property=RuntimeMaxSec=90", self.runs()[0])
        self.tick(59, wall=-100)
        self.assertEqual(self.frozen, {"ameesh-guest.slice"})
        self.tick(1, wall=-100)
        self.assertEqual(self.frozen, set())

    def test_guards_of_two_slices_never_touch_each_other(self):
        # codex #237 r4 (P2): guard B, on another slice, starts, freezes and
        # thaws; guard A's freeze and its service are left alone
        frozen = {"caps": {}, "freeze": {"since_ts": 1000.0, "until_ts": 1060.0}}
        self.at(1000.0).apply(frozen, 4)
        other = dataclasses.replace(self.cfg, slice="guest-b.slice")
        b = self.make(other)
        self.calls.clear()
        b.thaw_at_start()
        b.apply(frozen, 4)
        b.apply({"caps": {}}, 4)
        self.assertFalse(any(self.UNIT in c or "ameesh-guest.slice" in c for c in self.calls))
        self.assertIn(self.UNIT, self.services)
        self.assertEqual(self.frozen, {"ameesh-guest.slice"})
        self.assertEqual(guard.freeze_unit("guest-b.slice"),
                         "ameesh-guard-freeze-guest-b.service")

    def test_slice_names(self):
        self.assertEqual(guard.freeze_unit("ameesh-guest.slice"), self.UNIT)
        for bad in ("a b.slice", "x.slice\n", "-x.slice", "x.service", "$x.slice",
                    "%i.slice", "a;b.slice", "x\\y.slice", "", None):
            with self.subTest(bad):
                self.assertIsNone(guard.freeze_unit(bad))
        broken = self.make(dataclasses.replace(self.cfg, slice="a b.slice"))
        self.assertNotIn("freeze", broken.apply(
            {"caps": {}, "freeze": {"since_ts": 1000.0, "until_ts": 1060.0}}, 4))
        self.assertEqual(self.runs(), [])

    def test_slices_that_hold_the_helper_or_the_guard_are_refused(self):
        # codex #237 r5 (P1): freezing system.slice (or any slice above the
        # helper or the guard) would freeze what has to thaw it
        self.assertEqual(guard.slice_ancestors("a-b-c.slice"),
                         ["-.slice", "a.slice", "a-b.slice", "a-b-c.slice"])
        own = "/apps.slice/apps-ops.slice/ameesh-guard.service"
        for name in ("-.slice", "system.slice", "user.slice", "ameesh.slice",
                     "ameesh-guard.slice", "ameesh-guard-helper.slice", "apps.slice",
                     "apps-ops.slice", "a b.slice"):
            with self.subTest(name):
                self.assertIsNotNone(guard.refused_slice(name, own))
        for name in ("ameesh-guest.slice", "apps-ops-x.slice", "ameesh-guard-helper-x.slice"):
            with self.subTest(name):
                self.assertIsNone(guard.refused_slice(name, own))
        # the guard in a user's manager: its user slice is refused too
        self.assertIsNotNone(guard.refused_slice(
            "user-1000.slice", "/user.slice/user-1000.slice/user@1000.service/x.service"))
        # a refused slice is never touched: no cap, no freeze, no thaw
        for name in ("system.slice", "ameesh-guard.slice"):
            refused = self.make(dataclasses.replace(self.cfg, slice=name))
            refused.thaw_at_start()
            self.assertEqual(refused.apply(
                {"caps": {"cpu_weight": 10},
                 "freeze": {"since_ts": 1000.0, "until_ts": 1060.0}}, 4), [])
            self.assertEqual(self.calls, [])
        self.assertTrue(any("refused" in line for line in self.logged))

    def test_freeze_counted_only_when_the_slice_reads_frozen(self):
        # codex #237 r5 (P2): the service starts, its freeze fails, the shell
        # exits 0: not enforced, and retried at the next evaluation
        frozen = {"caps": {}, "freeze": {"since_ts": 1000.0, "until_ts": 1060.0}}
        self.freeze_fails = True
        self.assertNotIn("freeze", self.at(1000.0).apply(frozen, 4))
        self.assertTrue(any(c[1] == "show" and "--property=FreezerState" in c
                            for c in self.calls))
        self.freeze_fails = False
        self.tick(5)
        self.assertIn("freeze", self.enforcer.apply(frozen, 4))
        self.assertEqual(len(self.runs()), 2)
        # thawed behind the guard's back: started again, for what is left
        self.frozen.clear()
        self.tick(5)
        left = int(60 - self.mono[0])     # the failed attempt polled 2.2 s
        self.assertIn("freeze", self.enforcer.apply(frozen, 4))
        self.assertEqual(self.runs()[-1][-1], str(left))
        self.assertLess(left, 50)

    def test_retries_and_restarts_keep_the_episode_bound(self):
        # codex #237 r5 (P2): a retry, or a restarted guard, gets what is
        # left of the episode on the boot clock, never a fresh 60 s
        frozen = {"caps": {}, "freeze": {"since_ts": 1000.0, "until_ts": 1060.0}}
        self.assertIn("freeze", self.at(1000.0).apply(frozen, 4))
        self.assertEqual(self.runs()[-1][-1], "60")
        self.tick(20)
        self.services.clear()           # the service dies, the slice thaws
        self.frozen.clear()
        self.assertIn("freeze", self.enforcer.apply(frozen, 4))
        self.assertEqual(self.runs()[-1][-1], "40")
        # a new guard, the wall clock 100 s back: the published episode holds
        self.tick(10, wall=-110)
        restarted = self.make(self.cfg)
        restarted.restore_episode(json.loads(json.dumps(self.enforcer.episode)))
        restarted.thaw_at_start()
        self.assertIn("freeze", restarted.apply(frozen, 4))
        self.assertEqual(self.runs()[-1][-1], "30")
        # another boot: the old episode means nothing, the wall clock decides
        other = self.make(self.cfg, boot_id=lambda: "boot-2")
        other.restore_episode(self.enforcer.episode)
        other.thaw_at_start()
        self.at(1050.0)
        self.assertIn("freeze", other.apply(frozen, 4))
        self.assertEqual(self.runs()[-1][-1], "10")
        # ill-typed records are ignored
        for bad in (None, 3, {"since_ts": "x", "boot_id": "b", "boot_deadline": 1},
                    {"since_ts": 1, "boot_id": 2, "boot_deadline": 1},
                    {"since_ts": 1, "boot_id": "b", "boot_deadline": float("inf")}):
            fresh = self.make(self.cfg)
            fresh.restore_episode(bad)
            self.assertIsNone(fresh.episode, bad)

    def test_guard_publishes_the_freeze_and_its_episode(self):
        # the schema's caps.enforced lists caps only: the freeze reports
        # itself (freeze.enforced); the episode is published for a restart
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dataclasses.replace(
                self.cfg, offer=guard.parse_offer(lend(reclaim={"cpu_pressure_max": "20%"})),
                state_path=os.path.join(tmp, "state.json"),
                ack_path=os.path.join(tmp, "ack.json"))
            reading = {"cpu_count": 4, "mem_total_bytes": 16 * GIB,
                       "mem_available_bytes": 12 * GIB, "disk_free_bytes": 50 * GIB,
                       "psi_cpu_10": 50.0}
            g = guard.Guard(cfg, observe=False, signals=lambda now: dict(reading),
                            clock=lambda: self.now[0], enforcer=self.make(cfg))
            state = g.step()
            self.assertEqual(state["level"], 2)
            self.assertNotIn("freeze", state["caps"]["enforced"])
            self.assertTrue(state["freeze"]["enforced"])
            self.assertEqual(state["guard"]["freeze"],
                             {"since_ts": 1000.0, "boot_id": "boot-1",
                              "boot_deadline": 60.0})
            self.assertEqual(schema_errors(schema("ameesh-host-state-1.schema.json"),
                                           state), [])
            # restarted 20 s later: the slice thawed at start, frozen again
            # for the 40 s the episode has left
            self.tick(20)
            g = guard.Guard(cfg, observe=False, signals=lambda now: dict(reading),
                            clock=lambda: self.now[0], enforcer=self.make(cfg))
            self.assertEqual(g.step()["freeze"]["enforced"], True)
            self.assertEqual(self.runs()[-1][-1], "40")

    def _guard_cfg(self, tmp):
        cfg = dataclasses.replace(
            self.cfg, offer=guard.parse_offer(lend(reclaim={"cpu_pressure_max": "20%"})),
            state_path=os.path.join(tmp, "state.json"), ack_path=os.path.join(tmp, "ack.json"))
        reading = {"cpu_count": 4, "mem_total_bytes": 16 * GIB,
                   "mem_available_bytes": 12 * GIB, "disk_free_bytes": 50 * GIB,
                   "psi_cpu_10": 50.0}
        return cfg, (lambda: guard.Guard(cfg, observe=False, signals=lambda now: dict(reading),
                                         clock=lambda: self.now[0], enforcer=self.make(cfg)))

    def test_episode_kept_when_the_wall_clock_goes_back(self):
        # L159e5 (codex #237 r6): freeze at wall 1000; 40 s later the wall
        # reads 940 and the guard restarts: the saved decision (since 1000,
        # "in the future") is refused, the new one starts at 940, yet the
        # episode of this boot holds: 20 s left, not a fresh 60
        with tempfile.TemporaryDirectory() as tmp:
            _cfg, make = self._guard_cfg(tmp)
            self.assertEqual(make().step()["level"], 2)
            self.tick(40, wall=-60)
            state = make().step()
            self.assertEqual((state["level"], state["freeze"]["since_ts"]), (2, 940.0))
            self.assertEqual(self.runs()[-1][-1], "20")
            self.assertEqual(self.runs()[-1][-2], "60.000")
            # left level 2: the episode is over, a later freeze is new
            g = make()
            g.step()
            g.signals = lambda now: {"cpu_count": 4, "mem_available_bytes": 12 * GIB}
            self.tick(5)
            self.assertNotIn("freeze", g.step()["guard"])

    def test_stale_episode_of_a_guard_killed_out_of_level_2(self):
        # the last state says level 3 but still carries an episode of this
        # boot, its bound past: over; a new freeze gets its own 60 s
        with tempfile.TemporaryDirectory() as tmp:
            cfg, make = self._guard_cfg(tmp)
            with open(cfg.state_path, "w") as fh:
                json.dump({"schema": "ameesh-host-state/1", "seq": 3, "guard": {
                    "decision": {"level": 0, "reason": "idle", "since_ts": 900.0},
                    "freeze": {"since_ts": 800.0, "boot_id": "boot-1",
                               "boot_deadline": -20.0}}}, fh)
            self.assertEqual(make().step()["level"], 2)
            self.assertEqual(self.runs()[-1][-1], "60")

    def test_episode_written_before_the_freeze_is_dispatched(self):
        # L159e5 (codex #237 r6): the guard dies between starting the freeze
        # and publishing the state; restarted 40 s later, it takes back the
        # episode written before the dispatch: 20 s left, not a fresh 60
        for published in (True, False):
            with tempfile.TemporaryDirectory() as tmp:
                cfg, make = self._guard_cfg(tmp)
                if published:            # a state of level 0 published before
                    with open(cfg.state_path, "w") as fh:
                        json.dump({"schema": "ameesh-host-state/1", "seq": 3,
                                   "guard": {"decision": {"level": 0, "reason": "idle",
                                                          "since_ts": 900.0}}}, fh)
                g = make()
                with mock.patch.object(guard, "write_state", wraps=guard.write_state) as w, \
                        mock.patch.object(guard, "_key", side_effect=SystemExit):
                    with self.assertRaises(SystemExit):     # killed before publishing
                        g.step()
                    self.assertEqual(w.call_count, 1)
                self.assertTrue(self.frozen)
                with open(cfg.state_path) as fh:
                    saved = json.load(fh)
                self.assertEqual((saved["guard"]["freeze"]["boot_id"],
                                  saved["guard"]["decision"]["level"]), ("boot-1", 2))
                self.assertEqual(saved.get("seq"), 3 if published else None)
                self.tick(40)
                state = make().step()
                self.assertEqual(state["level"], 2, published)
                self.assertEqual(self.runs()[-1][-1], "20", published)
                self.setUp()

    def test_no_freeze_dispatched_when_the_episode_is_not_written(self):
        # codex #276 r1: a write that fails dispatches nothing (a restart
        # would bound the episode anew); the next evaluation tries again
        with tempfile.TemporaryDirectory() as tmp:
            _cfg, make = self._guard_cfg(tmp)
            g = make()
            real = guard.write_state
            failing = [OSError("full")]     # the episode's write only
            def write(path, record):
                if failing:
                    raise failing.pop()
                real(path, record)
            with mock.patch.object(guard, "write_state", side_effect=write):
                state = g.step()
            self.assertEqual(state["level"], 2)
            self.assertFalse(state["freeze"]["enforced"])
            self.assertEqual((self.runs(), self.frozen), ([], set()))
            self.assertNotIn("freeze", state["guard"])
            self.assertIn("freeze refused: episode not written", self.logged)
            self.tick(5)
            self.assertTrue(g.step()["freeze"]["enforced"])
            self.assertEqual(self.runs()[-1][-1], "55")

    def freeze_script(self, uptime_s: str | None, suspend_at: int = 0,
                      whole_sleep: bool = False) -> list[str]:
        """FREEZE_SCRIPT run by sh on fake systemctl and sleep, bound 60.000:
        each `sleep` moves the boot clock by its seconds, the `suspend_at`-th
        one spans a 300 s suspend; `uptime_s` None: no clock to read;
        `whole_sleep`: `sleep` refuses a fraction at once (BusyBox without
        FEATURE_FLOAT_SLEEP). The calls, in order."""
        with tempfile.TemporaryDirectory() as tmp:
            uptime, log = os.path.join(tmp, "uptime"), os.path.join(tmp, "log")
            if uptime_s is not None:
                with open(uptime, "w") as fh:
                    fh.write("%s 5.00\n" % uptime_s)
            for name, body in (
                    ("systemctl", 'echo "systemctl $*" >> "%s"' % log),
                    ("sleep", 'echo "sleep $*" >> "%s"; %sn=$(grep -c sleep "%s"); '
                              'awk -v s="$1" -v n="$n" -v z=%d '
                              '\'{print $1 + s + (n == z ? 300 : 0), $2}\' '
                              '"%s" > "%s.new" && mv "%s.new" "%s"'
                              % (log, 'case "$1" in *.*) exit 1;; esac; '
                                 if whole_sleep else "", log, suspend_at, uptime, uptime,
                                 uptime, uptime))):
                path = os.path.join(tmp, name)
                with open(path, "w") as fh:
                    fh.write("#!/bin/sh\n" + body + "\n")
                os.chmod(path, 0o755)
            script = guard.FREEZE_SCRIPT.replace(host.UPTIME, uptime)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ.get("PATH", ""))
            subprocess.run(["/bin/sh", "-c", script, "ameesh-guest.slice", "60.000", "50"],
                           env=env, check=True, timeout=20)
            with open(log) as fh:
                return fh.read().splitlines()

    def test_freeze_script_holds_the_bound_across_a_suspend(self):
        # codex #276 r1: the clock is read again after the suspend, the
        # slice thaws at once (a single `sleep 50` would hold it 50 s after
        # resume)
        self.assertEqual(self.freeze_script("10.00", suspend_at=3), [
            "systemctl freeze ameesh-guest.slice", "sleep 1", "sleep 1", "sleep 1",
            "systemctl thaw ameesh-guest.slice"])

    def test_freeze_script_thaws_at_the_bound(self):
        # codex #276 r2: the last wait is what is left, not a whole second
        # (58.90 + 1 + 1 thawed at 60.90)
        self.assertEqual(self.freeze_script("58.90"), [
            "systemctl freeze ameesh-guest.slice", "sleep 1", "sleep 0.1",
            "systemctl thaw ameesh-guest.slice"])
        # codex #308 r1: a `sleep` that refuses the fraction (BusyBox) ends
        # the wait at once, never a busy loop up to the bound
        self.assertEqual(self.freeze_script("58.90", whole_sleep=True), [
            "systemctl freeze ameesh-guest.slice", "sleep 1", "sleep 0.1",
            "systemctl thaw ameesh-guest.slice"])
        # an unreadable clock ends the wait
        self.assertEqual(self.freeze_script(None), [
            "systemctl freeze ameesh-guest.slice", "systemctl thaw ameesh-guest.slice"])

    def test_a_persistent_write_failure_never_ends_the_guard(self):
        # codex #276 r2: a full disk refuses the episode (no freeze), then the
        # publication: reported, the guard keeps evaluating; the state is
        # written as soon as the disk takes it again
        import errno
        with tempfile.TemporaryDirectory() as tmp:
            cfg, make = self._guard_cfg(tmp)
            g = make()
            full = OSError(errno.ENOSPC, "No space left on device")
            with mock.patch.object(guard, "write_state", side_effect=full), \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                for _ in range(3):
                    self.assertEqual(g.step()["level"], 2)
                    self.tick(5)
            self.assertEqual((self.runs(), os.path.exists(cfg.state_path)), ([], False))
            self.assertIn("No space left on device", err.getvalue())
            state = g.step()
            self.assertTrue(state["freeze"]["enforced"])
            with open(cfg.state_path) as fh:
                self.assertEqual(json.load(fh)["seq"], state["seq"])

    def test_restart_takes_over_the_service(self):
        # an earlier guard's service still holds the slice: the new guard
        # stops it (it thaws) at start; a service left by a failed attempt is
        # stopped and forgotten before a new one starts
        self.frozen.add("ameesh-guest.slice")
        self.services[self.UNIT] = ["ameesh-guest.slice", 40.0, 70.0]
        self.enforcer.thaw_at_start()
        self.assertEqual((self.frozen, self.services), (set(), {}))
        self.lying = {"systemd-run"}
        frozen = {"caps": {}, "freeze": {"since_ts": 1000.0, "until_ts": 1060.0}}
        self.assertNotIn("freeze", self.at(1000.0).apply(frozen, 4))
        self.lying = set()
        self.calls.clear()
        self.tick(5)
        self.assertIn("freeze", self.enforcer.apply(frozen, 4))
        self.assertEqual([c[1:] for c in self.calls if c[0] == "systemctl"][:2],
                         [["stop", self.UNIT], ["reset-failed", self.UNIT]])
        self.assertEqual(self.services[self.UNIT][1], self.mono[0] + 55)

    def test_freeze_attempted_is_thawed(self):
        # codex #237 r1: systemd-run reports a failure (timeout, SIGTERM)
        # after the service froze the slice: the drop of the level and stop() thaw
        self.lying = {"systemd-run"}
        frozen = {"caps": {}, "freeze": {"since_ts": 1000.0, "until_ts": 1060.0}}
        self.assertNotIn("freeze", self.at(1000.0).apply(frozen, 4))
        self.assertEqual(self.frozen, {"ameesh-guest.slice"})
        self.lying = set()
        self.at(1000.0).apply({"caps": {}}, 4)
        self.assertEqual((self.frozen, self.services), (set(), {}))
        self.lying = {"systemd-run"}
        self.at(1000.0).apply(frozen, 4)
        self.lying = set()
        self.enforcer.freeze(False)                          # Guard.stop
        self.assertEqual((self.frozen, self.services), (set(), {}))

    def test_thaw_at_start_retried_until_it_works(self):
        # codex #237 r1: a failed thaw at start is retried at each evaluation
        self.failing = {"thaw"}
        self.enforcer.thaw_at_start()
        self.enforcer.apply({"caps": {}}, 4)
        self.assertEqual(len(self.freezes()), 2)
        self.failing = set()
        self.enforcer.apply({"caps": {}}, 4)
        self.enforcer.apply({"caps": {}}, 4)
        self.assertEqual(len(self.freezes()), 3)             # once it worked, no more

    def test_every_agent_capped_despite_a_failure(self):
        # codex #237 r1: a failing first unit no longer skips the others
        def fake(argv, **kw):
            self.calls.append(argv)
            bad = argv[1] == "set-property" and argv[3] == "ameesh-guest-runner@a.service"
            out = self.units if argv[1] == "list-units" else ""
            return mock.Mock(returncode=1 if bad else 0, stdout=out, stderr="")
        enforcer = guard.Enforcer(self.cfg, run=fake, log=self.logged.append)
        for _ in range(2):
            self.assertNotIn("agent_memory_mb",
                             enforcer.apply({"caps": {"agent_memory_mb": 1024}}, 4))
        self.assertIn(["ameesh-guest-runner@b.service", "MemoryMax=1024M"], self.sets())

    def test_guard_thaws_at_start_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dataclasses.replace(
                self.cfg, offer=guard.parse_offer(lend(cpu_max=2)),
                state_path=os.path.join(tmp, "state.json"),
                ack_path=os.path.join(tmp, "ack.json"))
            reading = {"cpu_count": 4, "mem_total_bytes": 16 * GIB,
                       "mem_available_bytes": 12 * GIB, "disk_free_bytes": 50 * GIB}
            with mock.patch.object(guard.resources, "lendable", None, create=True), \
                    mock.patch.object(guard.resources, "measure", None, create=True):
                g = guard.Guard(cfg, observe=False, signals=lambda now: dict(reading),
                                clock=lambda: 1000.0,
                                enforcer=self.make(cfg))
                self.assertEqual(self.calls[:2], [["systemctl", "stop", self.UNIT],
                                                  ["systemctl", "thaw", "ameesh-guest.slice"]])
                state = g.step()
                self.assertEqual(state["guard"]["mode"], "enforce")
                self.assertIn("cpu_share", state["caps"]["enforced"])
                with open(cfg.state_path) as fh:
                    self.assertEqual(json.load(fh)["caps"]["enforced"],
                                     state["caps"]["enforced"])
                g.enforcer.frozen = True
                self.calls.clear()
                g.stop()
                self.assertEqual(self.freezes(), [["systemctl", "thaw", "ameesh-guest.slice"]])
                # observing: no enforcer, no systemctl
                self.calls.clear()
                observer = guard.Guard(cfg, signals=lambda now: dict(reading),
                                       clock=lambda: 1000.0)
                self.assertEqual(observer.step()["caps"]["enforced"], [])
                observer.stop()
                self.assertEqual(self.calls, [])


class DrainStopTest(unittest.TestCase):
    """Tranche 3: jobs stopped at the drain's deadline, guest stopped at level 4,
    manual drain."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l159e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = guard.GuardConfig(
            host="vm-test", max_agents=2,
            offer=guard.parse_offer({**CAPS, "cpu_max": 2, "drain_seconds": 100,
                                     "reclaim": {"memory_available_min": "4GiB"}}),
            state_path=os.path.join(self.tmp, "state.json"),
            ack_path=os.path.join(self.tmp, "ack.json"),
            drain_request_path=os.path.join(self.tmp, "drain.json"))
        self.calls = []
        self.reading = {"cpu_count": 4, "mem_total_bytes": 16 * GIB,
                        "mem_available_bytes": 12 * GIB, "disk_free_bytes": 50 * GIB}
        self.clock = [1000.0]
        patcher = mock.patch.multiple(guard.resources, lendable=None, measure=None,
                                      create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake(self, argv, **kw):
        self.calls.append(argv)
        return mock.Mock(returncode=0, stdout="", stderr="")

    def at(self, enforcer, now):
        self.clock[0] = now
        return enforcer

    def guard(self, observe=False):
        return guard.Guard(self.cfg, observe=observe, signals=lambda now: dict(self.reading),
                           clock=lambda: self.clock[0],
                           enforcer=guard.Enforcer(self.cfg, run=self.fake, log=lambda t: None,
                                   clock=lambda: self.clock[0]))

    def stops(self):
        return [c[1:] for c in self.calls if c[1] in ("stop", "kill")
                and not c[2].startswith(guard.FREEZE_UNIT)]

    def test_deadline_stops_jobs_then_level_4_stops_guest(self):
        g = self.guard()
        self.assertEqual(g.step()["level"], 0)
        self.reading["mem_available_bytes"] = 1 * GIB
        drain = g.step()
        self.assertEqual((drain["level"], drain["drain_deadline_ts"]), (3, 1100.0))
        self.assertEqual(self.stops(), [])
        self.clock[0] = 1100.0                   # the deadline: jobs stopped, once
        g.step()
        self.clock[0] = 1105.0
        g.step()
        self.assertEqual(self.stops(), [["stop", "ameesh-job-*.scope"]])
        self.clock[0] = 1130.0                   # 30 s later, no acknowledgement
        self.assertEqual(g.step()["state"], "stopped")
        self.clock[0] = 1135.0
        g.step()
        self.assertEqual(self.stops(), [
            ["stop", "ameesh-job-*.scope"],
            ["kill", "--signal=SIGKILL", "ameesh-guest-runner@*.service", "ameesh-job-*.scope"],
            ["stop", "ameesh-guest.slice"]])

    def test_level_4_stops_the_guest_users_slice_too(self):
        # R2 N2 (#1416) : le gestionnaire de l'utilisateur invité vit dans
        # user-<uid>.slice (linger, `systemd-run --user`), hors de la slice
        # invitée : un processus qu'un job y a laissé survit à l'arrêt de la
        # slice seule. Le niveau 4 arrête donc aussi cette slice, et le
        # gestionnaire arrêté est enregistré pour repartir avec l'invité.
        def run(argv, **kw):
            self.calls.append(argv)
            if argv[1] == "list-units":
                return mock.Mock(returncode=0, stdout=(
                    "ameesh-guest-runner@1.service loaded active running guest\n"),
                    stderr="")
            if argv[1] == "show" and argv[2] == "--property=UID":
                return mock.Mock(returncode=0, stdout="998\n", stderr="")
            if argv[1] == "is-active":
                return mock.Mock(returncode=0, stdout="active\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        enforcer = guard.Enforcer(self.cfg, run=run, log=lambda t: None,
                                  clock=lambda: self.clock[0],
                                  monotonic=lambda: self.clock[0])
        self.assertTrue(self.at(enforcer, 2000.0).stop_guest())
        stops = [c[2:] for c in self.calls if len(c) > 1 and c[1] == "stop"]
        self.assertIn(["ameesh-guest.slice"], stops)
        self.assertIn(["user-998.slice"], stops)
        self.assertEqual(sorted(enforcer.stopped["units"]),
                         ["ameesh-guest-runner@1.service", "user@998.service"])

    def test_a_stopped_user_manager_restarts_with_the_guest(self):
        # R2 N2 : le gestionnaire relancé garde rootless Podman utilisable
        # pour le job suivant ; un gestionnaire qui ne tournait pas n'est pas
        # démarré pour autant.
        def run(argv, **kw):
            self.calls.append(argv)
            if argv[1] == "list-units":
                return mock.Mock(returncode=0, stdout=(
                    "ameesh-guest-runner@1.service loaded active running guest\n"),
                    stderr="")
            if argv[1] == "show" and argv[2] == "--property=UID":
                return mock.Mock(returncode=0, stdout="998\n", stderr="")
            if argv[1] == "show":
                return mock.Mock(returncode=0, stdout=(
                    "ActiveState=inactive\nInactiveEnterTimestampMonotonic=1000000\n"),
                    stderr="")
            if argv[1] == "is-active":
                return mock.Mock(returncode=0, stdout="active\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        enforcer = guard.Enforcer(self.cfg, run=run, log=lambda t: None,
                                  clock=lambda: self.clock[0],
                                  monotonic=lambda: self.clock[0])
        self.assertTrue(self.at(enforcer, 2000.0).stop_guest())
        self.calls.clear()
        enforcer.restart_stopped(2000.0)
        starts = [c[3] for c in self.calls if len(c) > 3 and c[1] == "start"]
        self.assertIn("user@998.service", starts)
        self.assertIn("ameesh-guest-runner@1.service", starts)

    def test_an_inactive_user_manager_is_not_stopped_nor_restarted(self):
        # Seul ce que l'arrêt fait tomber revient : un gestionnaire déjà
        # inactif n'est ni arrêté ni relancé (pas d'état créé par le garde).
        def run(argv, **kw):
            self.calls.append(argv)
            if argv[1] == "list-units":
                return mock.Mock(returncode=0, stdout=(
                    "ameesh-guest-runner@1.service loaded active running guest\n"),
                    stderr="")
            if argv[1] == "show" and argv[2] == "--property=UID":
                return mock.Mock(returncode=0, stdout="998\n", stderr="")
            if argv[1] == "is-active":
                return mock.Mock(returncode=3, stdout="inactive\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        enforcer = guard.Enforcer(self.cfg, run=run, log=lambda t: None,
                                  clock=lambda: self.clock[0],
                                  monotonic=lambda: self.clock[0])
        self.assertTrue(self.at(enforcer, 2000.0).stop_guest())
        self.assertIn(["user-998.slice"],
                      [c[2:] for c in self.calls if len(c) > 1 and c[1] == "stop"])
        self.assertEqual(enforcer.stopped["units"],
                         {"ameesh-guest-runner@1.service": 2000.0})

    def test_level_4_stops_sessions_even_when_the_user_manager_is_inactive(self):
        def run(argv, **kw):
            self.calls.append(argv)
            if argv[1] == "list-units":
                return mock.Mock(returncode=0, stdout=(
                    "ameesh-guest-runner@1.service loaded active running guest\n"))
            if argv[1] == "show" and argv[2] == "--property=UID":
                return mock.Mock(returncode=0, stdout="998\n")
            if argv[1] == "is-active":
                return mock.Mock(returncode=3 if argv[-1] == "user@998.service" else 0)
            return mock.Mock(returncode=0, stdout="", stderr="")

        enforcer = guard.Enforcer(self.cfg, run=run, log=lambda t: None,
                                  clock=lambda: self.clock[0], monotonic=lambda: self.clock[0])
        self.assertTrue(self.at(enforcer, 2000.0).stop_guest())
        self.assertIn(["systemctl", "stop", "user-998.slice"], self.calls)
        self.assertNotIn("user@998.service", enforcer.stopped["units"])

    def test_ci_only_host_uses_the_required_covering_agent_pattern(self):
        # ci-runners.sh refuses prepare/enable/check unless agent_units covers
        # ameesh-guest-ci@1.service (guard_covers); this is the admitted config.
        self.cfg = dataclasses.replace(self.cfg, agent_units="ameesh-guest-*.service")
        def run(argv, **kw):
            self.calls.append(argv)
            if argv[1] == "list-units":
                self.assertEqual(argv[-1], "ameesh-guest-*.service")
                return mock.Mock(returncode=0, stdout=(
                    "ameesh-guest-ci@1.service loaded active running guest\n"))
            if argv[1] == "show" and argv[2] == "--property=UID":
                return mock.Mock(returncode=0, stdout="998\n")
            return mock.Mock(returncode=0, stdout="", stderr="")

        enforcer = guard.Enforcer(self.cfg, run=run, log=lambda t: None,
                                  clock=lambda: self.clock[0], monotonic=lambda: self.clock[0])
        self.assertTrue(enforcer.stop_guest())
        self.assertIn(["systemctl", "stop", "user-998.slice"], self.calls)
        self.assertIn("ameesh-guest-ci@1.service", enforcer.stopped["units"])

    def test_a_failed_user_manager_stop_is_retried_after_the_units_are_gone(self):
        # R2 N2 : si l'arrêt de user-UID.slice échoue, le nouvel essai ne
        # conclut pas au succès parce que les unités ont disparu : le
        # gestionnaire dû est gardé et arrêté au tour suivant.
        state = {"lists": 0, "stop": 0}

        def run(argv, **kw):
            self.calls.append(argv)
            if argv[1] == "list-units":
                state["lists"] += 1
                stdout = ("ameesh-guest-runner@1.service loaded active running guest\n"
                          if state["lists"] == 1 else "")
                return mock.Mock(returncode=0, stdout=stdout, stderr="")
            if argv[1] == "show" and argv[2] == "--property=UID":
                return mock.Mock(returncode=0, stdout="998\n", stderr="")
            if argv[1] == "is-active":
                return mock.Mock(returncode=0, stdout="active\n", stderr="")
            if argv[1] == "stop" and argv[2] == "user-998.slice":
                state["stop"] += 1
                return mock.Mock(returncode=0 if state["stop"] > 1 else 1,
                                 stdout="", stderr="busy")
            return mock.Mock(returncode=0, stdout="", stderr="")

        enforcer = guard.Enforcer(self.cfg, run=run, log=lambda t: None,
                                  clock=lambda: self.clock[0],
                                  monotonic=lambda: self.clock[0])
        self.at(enforcer, 2000.0)
        self.assertFalse(enforcer.stop_guest())
        self.assertNotIn("user@998.service", enforcer.stopped["units"])
        self.at(enforcer, 2010.0)
        self.assertTrue(enforcer.stop_guest())
        stops = [c[2:] for c in self.calls if len(c) > 1 and c[1] == "stop"]
        self.assertEqual(stops.count(["user-998.slice"]), 2)
        self.assertEqual(enforcer.stopped["units"],
                         {"ameesh-guest-runner@1.service": 2000.0,
                          "user@998.service": 2010.0})

    def test_acknowledged_drain_never_stops_the_guest(self):
        g = self.guard()
        g.step()
        self.reading["mem_available_bytes"] = 1 * GIB
        drain = g.step()
        with open(self.cfg.ack_path, "w") as fh:
            json.dump({"schema": "ameesh-host-ack/1", "seq": drain["seq"],
                       "state": "draining", "drained": True, "in_turn": []}, fh)
        self.clock[0] = 1200.0
        self.assertEqual(g.step()["state"], "draining")
        self.assertEqual(self.stops(), [["stop", "ameesh-job-*.scope"]])

    def test_failed_stop_is_retried_and_observe_stops_nothing(self):
        failing = [True]

        def run(argv, **kw):
            self.calls.append(argv)
            bad = failing[0] and argv[1] == "stop" and argv[2] == "ameesh-guest.slice"
            return mock.Mock(returncode=1 if bad else 0, stdout="", stderr="")
        enforcer = guard.Enforcer(self.cfg, run=run, log=lambda t: None,
                                   clock=lambda: self.clock[0])
        state = {"level": 4, "caps": {}, "drain_deadline_ts": None}
        self.at(enforcer, 2000.0).apply(state, 4)
        failing[0] = False
        self.at(enforcer, 2005.0).apply(state, 4)
        self.at(enforcer, 2010.0).apply(state, 4)
        self.assertEqual([c[2] for c in self.calls if c[1] == "stop"],
                         ["ameesh-job-*.scope", "ameesh-guest.slice", "ameesh-guest.slice"])
        # back to level 0, then a new stop: done again
        self.at(enforcer, 3000.0).apply({"level": 0, "caps": {}}, 4)
        self.calls.clear()
        self.at(enforcer, 4000.0).apply(state, 4)
        self.assertEqual(len([c for c in self.calls if c[1] == "stop"]), 2)
        # observing: the levels are decided, nothing is stopped
        self.calls.clear()
        self.clock[0] = 1000.0
        g = self.guard(observe=True)
        self.reading["mem_available_bytes"] = 1 * GIB
        g.step()
        self.clock[0] = 2000.0
        self.assertEqual(g.step()["state"], "stopped")
        self.assertEqual(self.calls, [])

    def test_failed_kill_is_retried(self):
        # codex #238 r1: SIGKILL fails, the slice stop works: the kill is
        # retried at the next evaluation
        failing = [True]

        def run(argv, **kw):
            self.calls.append(argv)
            bad = failing[0] and argv[1] == "kill"
            return mock.Mock(returncode=1 if bad else 0, stdout="", stderr="")
        enforcer = guard.Enforcer(self.cfg, run=run, log=lambda t: None,
                                   clock=lambda: self.clock[0])
        state = {"level": 4, "caps": {}, "drain_deadline_ts": None}
        self.at(enforcer, 2000.0).apply(state, 4)
        failing[0] = False
        self.at(enforcer, 2005.0).apply(state, 4)
        self.at(enforcer, 2010.0).apply(state, 4)
        self.assertEqual(len([c for c in self.calls if c[1] == "kill"]), 2)

    def test_ended_manual_drain_never_escalates(self):
        # codex #238 r1: drain --for 60 on a healthy host: level 3 at t=0,
        # expired at 60; no job stop at 100, no kill at 130
        for end in ("expires", "cancel"):
            self.calls.clear()
            self.clock[0] = 1000.0
            if os.path.exists(self.cfg.state_path):
                os.remove(self.cfg.state_path)
            guard.request_drain(self.cfg.drain_request_path, 1000.0,
                                60 if end == "expires" else None)
            g = self.guard()
            self.assertEqual(g.step()["level"], 3)
            self.clock[0] = 1060.0
            if end == "cancel":
                guard.cancel_drain(self.cfg.drain_request_path)
            self.assertEqual(g.step()["level"], 0, end)
            for t in (1100.0, 1130.0, 1135.0):
                self.clock[0] = t
                self.assertEqual(g.step()["state"], "available", end)
            self.assertEqual(self.stops(), [], end)
        # the request gone but the application short of memory: the drain goes on
        guard.request_drain(self.cfg.drain_request_path, 2000.0, 10)
        self.clock[0] = 2000.0
        g = self.guard()
        self.assertEqual(g.step()["level"], 3)
        self.reading["mem_available_bytes"] = 1 * GIB
        self.clock[0] = 2020.0
        self.assertEqual(g.step()["level"], 3)
        self.clock[0] = 2130.0
        self.assertEqual(g.step()["state"], "stopped")

    def test_request_expiry_and_deadline_on_one_clock(self):
        # codex #238 r4: request until 1099, drain deadline 1100; the
        # evaluation starts at 1098 and measuring takes 3 s: at 1101 the
        # request is over on a healthy host, the drain ends, no job stopped
        guard.request_drain(self.cfg.drain_request_path, 1000.0, 99)
        slow = [0.0]

        def signals(now):
            self.clock[0] += slow[0]
            return dict(self.reading)
        g = guard.Guard(self.cfg, observe=False, signals=signals,
                        clock=lambda: self.clock[0],
                        enforcer=guard.Enforcer(self.cfg, run=self.fake, log=lambda t: None,
                                                clock=lambda: self.clock[0]))
        drain = g.step()
        self.assertEqual((drain["level"], drain["drain_deadline_ts"]), (3, 1100.0))
        self.clock[0], slow[0] = 1098.0, 3.0
        self.assertEqual(g.step()["state"], "available")
        self.assertEqual(self.stops(), [])
        # still requested when the measure ends, and the deadline (1201)
        # passed meanwhile: the jobs stop
        guard.request_drain(self.cfg.drain_request_path, 1101.0, 200)
        self.clock[0], slow[0] = 1101.0, 0.0
        self.assertEqual(g.step()["drain_deadline_ts"], 1201.0)
        self.clock[0], slow[0] = 1198.0, 3.0
        g.step()
        self.assertEqual(self.stops(), [["stop", "ameesh-job-*.scope"]])
        # the enforcer judges the deadline at the decision's instant, not
        # on its own later clock
        self.calls.clear()
        enforcer = guard.Enforcer(self.cfg, run=self.fake, log=lambda t: None,
                                  clock=lambda: 1101.0)
        state = {"level": 3, "caps": {}, "drain_deadline_ts": 1100.0}
        enforcer.apply(state, 4, 1099.9)
        self.assertEqual(self.stops(), [])
        enforcer.apply(state, 4, 1100.0)
        self.assertEqual(self.stops(), [["stop", "ameesh-job-*.scope"]])

    def test_cancelled_manual_drain_ends_at_level_4_too(self):
        # codex #238 r2: indefinite manual drain, level 4 at t=120, SIGKILL
        # fails; cancelled at 125: back to available, the kill not retried
        def run(argv, **kw):
            self.calls.append(argv)
            return mock.Mock(returncode=1 if argv[1] == "kill" else 0, stdout="", stderr="")
        guard.request_drain(self.cfg.drain_request_path, 1000.0, None)
        g = guard.Guard(self.cfg, observe=False, signals=lambda now: dict(self.reading),
                        clock=lambda: self.clock[0],
                        enforcer=guard.Enforcer(self.cfg, run=run, log=lambda t: None,
                                   clock=lambda: self.clock[0]))
        self.assertEqual(g.step()["level"], 3)
        self.clock[0] = 1130.0
        self.assertEqual(g.step()["level"], 4)
        guard.cancel_drain(self.cfg.drain_request_path)
        self.clock[0] = 1135.0
        self.assertEqual(g.step()["state"], "available")
        kills = len([c for c in self.calls if c[1] == "kill"])
        for t in (1140.0, 1145.0):
            self.clock[0] = t
            self.assertEqual(g.step()["level"], 0)
        self.assertEqual(len([c for c in self.calls if c[1] == "kill"]), kills)

    def test_manual_drain(self):
        path = self.cfg.drain_request_path
        self.assertFalse(guard.drain_requested(path, 0.0))
        guard.request_drain(path, 1000.0, 60)
        self.assertTrue(guard.drain_requested(path, 1059.0))
        self.assertFalse(guard.drain_requested(path, 1060.0))
        guard.request_drain(path, 1000.0, None)        # until cancelled
        self.assertTrue(guard.drain_requested(path, 10 ** 10))
        g = self.guard()
        state = g.step()
        self.assertEqual((state["state"], state["reason"]), ("draining", "manual"))
        self.assertTrue(guard.cancel_drain(path))
        self.assertFalse(guard.cancel_drain(path))
        with open(path, "w") as fh:
            json.dump({"schema": "other", "until_ts": None}, fh)
        self.assertFalse(guard.drain_requested(path, 0.0))
        # codex #238 r5: a duration that is not a finite number > 0, or whose
        # end is not a date, writes nothing; an infinite until_ts is no drain
        os.unlink(path)
        for seconds in (0, -1, math.inf, -math.inf, math.nan, 1e300, 10 ** 400,
                        True, "60"):
            with self.assertRaises(ValueError, msg=repr(seconds)):
                guard.request_drain(path, 1000.0, seconds)
            self.assertFalse(os.path.exists(path), repr(seconds))
        for until in (math.inf, "2e9"):
            with open(path, "w") as fh:
                json.dump({"schema": guard.SCHEMA_DRAIN_REQUEST, "until_ts": until}, fh)
            self.assertFalse(guard.drain_requested(path, 0.0), repr(until))
        os.unlink(path)
        with mock.patch.object(guard, "load_config", lambda path: self.cfg), \
                mock.patch("sys.stderr"):
            self.assertEqual(guard.main(["drain", "--for", "inf"]), 2)
        self.assertFalse(os.path.exists(path))


class RestartTest(unittest.TestCase):
    """L159e4: the agent units a stop stopped start again, once, after
    `restart_after_seconds` at level 1 or below."""

    UNITS = ("ameesh-guest-runner@1.service", "ameesh-guest-runner@2.service",
             "ameesh-guest-runner@3.service")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l159e4-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = guard.GuardConfig(
            host="vm-test", max_agents=3,
            offer=guard.parse_offer({**CAPS, "drain_seconds": 100, "calm_seconds": 600,
                                     "restart_after_seconds": 300,
                                     "reclaim": {"memory_available_min": "4GiB",
                                                 "cpu_pressure_max": "20%"}}),
            state_path=os.path.join(self.tmp, "state.json"),
            ack_path=os.path.join(self.tmp, "ack.json"),
            drain_request_path=os.path.join(self.tmp, "drain.json"))
        self.reading = {"cpu_count": 4, "mem_total_bytes": 16 * GIB,
                        "mem_available_bytes": 1 * GIB, "disk_free_bytes": 50 * GIB}
        self.clock, self.mono, self.boot = [0.0], [5000.0], ["boot-a"]
        # the fake systemd: ActiveState and InactiveEnterTimestampMonotonic (µs)
        self.units = {u: ["active", 0] for u in self.UNITS[:2]}
        self.units[self.UNITS[2]] = ["inactive", 1000 * 10 ** 6]   # the owner's, before
        self.starts, self.logs = [], []
        patcher = mock.patch.multiple(guard.resources, lendable=None, measure=None,
                                      create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def systemd(self, argv, **kw):
        out = ""
        if argv[1] == "list-units":
            running = "--state=active,activating,reloading" in argv
            out = "".join("%s loaded %s\n" % (u, st) for u, (st, _) in self.units.items()
                          if not running or st == "active")
        elif argv[1] == "kill":
            for unit in self.units.values():
                if unit[0] == "active":
                    unit[:] = ["inactive", int(self.mono[0] * 10 ** 6)]
        elif argv[1] == "show" and argv[-1] not in self.units:
            out = "FreezerState=frozen\n"          # the slice, frozen at level 2
        elif argv[1] == "show":
            st, inactive = self.units[argv[-1]]
            out = "ActiveState=%s\nInactiveEnterTimestampMonotonic=%d\n" % (st, inactive)
        elif argv[1] == "start":
            self.starts.append(argv[-1])
            self.units[argv[-1]][0] = "active"
        return mock.Mock(returncode=0, stdout=out, stderr="")

    def guard(self):
        enforcer = guard.Enforcer(self.cfg, run=self.systemd, log=self.logs.append,
                                  clock=lambda: self.clock[0], boot_id=lambda: self.boot[0],
                                  boottime=lambda: self.mono[0], monotonic=lambda: self.mono[0],
                                  sleep=lambda s: None)
        return guard.Guard(self.cfg, observe=False, signals=lambda now: dict(self.reading),
                           clock=lambda: self.clock[0], enforcer=enforcer)

    def at(self, g, t):
        self.mono[0] += t - self.clock[0]
        self.clock[0] = t
        return g.step()

    def stop_then_clear(self, g):
        self.at(g, 0.0)
        state = self.at(g, 130.0)
        self.assertEqual(state["state"], "stopped")
        self.assertEqual(sorted(state["guard"]["stopped"]["units"]), list(self.UNITS[:2]))
        self.reading["mem_available_bytes"] = 12 * GIB
        self.at(g, 200.0)                                # calm counting from 200
        self.assertEqual(self.at(g, 800.0)["level"], 0)  # level 0: low from 800
        return g

    def test_restart_once_after_the_window(self):
        g = self.stop_then_clear(self.guard())
        self.at(g, 1099.0)
        self.assertEqual(self.starts, [])
        state = self.at(g, 1100.0)
        # the owner's unit, inactive before the stop, is not the guard's
        self.assertEqual(self.starts, list(self.UNITS[:2]))
        self.assertEqual(state["guard"]["restart"],
                         {"ts": 1100.0, "started": list(self.UNITS[:2]), "skipped": {}})
        self.assertNotIn("stopped", state["guard"])
        self.assertTrue(any("restart after the stop" in line for line in self.logs))
        with open(self.cfg.state_path) as fh:
            self.assertEqual(json.load(fh)["guard"]["restart"]["started"], list(self.UNITS[:2]))
        # once: units stopped again by hand stay stopped
        for unit in self.UNITS[:2]:
            self.units[unit] = ["inactive", int(self.mono[0] * 10 ** 6)]
        self.at(g, 5000.0)
        self.assertEqual(len(self.starts), 2)

    def test_a_restarted_guard_keeps_the_last_restart(self):
        # codex #288 r1: the guard restarted after a restart of units keeps
        # publishing it (the source of the `guard_restart` alert)
        g = self.stop_then_clear(self.guard())
        restart = self.at(g, 1100.0)["guard"]["restart"]
        g = self.guard()
        self.assertEqual(self.at(g, 1105.0)["guard"]["restart"], restart)
        with open(self.cfg.state_path) as fh:
            self.assertEqual(json.load(fh)["guard"]["restart"], restart)
        # an ill-typed record is not taken back
        with open(self.cfg.state_path) as fh:
            saved = json.load(fh)
        saved["guard"]["restart"] = {"ts": "soon", "started": [], "skipped": {}}
        with open(self.cfg.state_path, "w") as fh:
            json.dump(saved, fh)
        self.assertNotIn("restart", self.at(self.guard(), 1110.0)["guard"])

    def test_never_a_unit_stopped_since_by_someone_else(self):
        g = self.stop_then_clear(self.guard())
        # the owner started @2 and stopped it again; @1 is running again
        self.units[self.UNITS[1]] = ["inactive", int(self.mono[0] * 10 ** 6)]
        self.units[self.UNITS[0]][0] = "active"
        state = self.at(g, 1100.0)
        self.assertEqual(self.starts, [])
        self.assertEqual(state["guard"]["restart"]["skipped"],
                         {self.UNITS[0]: "running", self.UNITS[1]: "stopped_by_owner"})

    def test_window_restarts_when_the_level_rises(self):
        g = self.stop_then_clear(self.guard())
        self.reading["mem_available_bytes"] = 1 * GIB
        self.assertEqual(self.at(g, 1000.0)["level"], 3)
        self.reading["mem_available_bytes"] = 12 * GIB
        self.at(g, 1001.0)          # level 3 held, calm counting: not low yet
        # the new drain is not acknowledged: stopped again, the record kept
        state = self.at(g, 1130.0)
        self.assertEqual(state["state"], "stopped")
        self.assertEqual(sorted(state["guard"]["stopped"]["units"]), list(self.UNITS[:2]))
        self.assertEqual(self.at(g, 1601.0)["level"], 0)
        self.at(g, 1900.0)
        self.assertEqual(self.starts, [])
        self.at(g, 1901.0)
        self.assertEqual(self.starts, list(self.UNITS[:2]))

    def test_each_unit_keeps_the_instant_of_its_own_stop(self):
        # codex #267 r1: the owner starts @1 again and stops it; a later stop
        # (here, by a restarted guard, level 4 held) finds it stopped and
        # must not move its instant past the owner's stop
        g = self.guard()
        self.at(g, 0.0)
        stop = self.at(g, 130.0)["guard"]["stopped"]["units"]
        self.mono[0] += 70
        self.units[self.UNITS[0]] = ["inactive", int(self.mono[0] * 10 ** 6)]
        g = self.guard()
        self.assertEqual(self.at(g, 300.0)["guard"]["stopped"]["units"], stop)
        self.reading["mem_available_bytes"] = 12 * GIB
        self.at(g, 301.0)
        self.assertEqual(self.at(g, 901.0)["level"], 0)
        state = self.at(g, 1201.0)
        self.assertEqual(self.starts, [self.UNITS[1]])
        self.assertEqual(state["guard"]["restart"]["skipped"],
                         {self.UNITS[0]: "stopped_by_owner"})

    def test_restart_kept_from_the_format_before_r1(self):
        # codex #267 r2: a state written before {unit: instant} ({units:
        # [unit…], mono}) keeps its pending restart across the upgrade
        g = self.guard()
        self.at(g, 0.0)
        record = self.at(g, 130.0)["guard"]["stopped"]
        with open(self.cfg.state_path) as fh:
            saved = json.load(fh)
        saved["guard"]["stopped"] = dict(record, units=sorted(record["units"]),
                                         mono=max(record["units"].values()))
        with open(self.cfg.state_path, "w") as fh:
            json.dump(saved, fh)
        g = self.guard()
        self.assertEqual(g.enforcer.stopped["units"], record["units"])
        self.reading["mem_available_bytes"] = 12 * GIB
        self.at(g, 200.0)
        self.assertEqual(self.at(g, 800.0)["level"], 0)
        self.at(g, 1100.0)
        self.assertEqual(self.starts, list(self.UNITS[:2]))

    def test_a_stop_in_a_new_boot_records_that_boot(self):
        # codex #267 r2: the record of boot A survives a reboot in the state;
        # systemd starts the units, the guard stops them again in boot B:
        # the record is boot B's, the restart is not skipped as `reboot`
        g = self.guard()
        self.at(g, 0.0)
        self.assertEqual(self.at(g, 130.0)["guard"]["stopped"]["boot_id"], "boot-a")
        self.boot[0], self.mono[0] = "boot-b", 50.0
        for unit in self.UNITS[:2]:
            self.units[unit] = ["active", 0]
        g = self.guard()
        state = self.at(g, 140.0)
        self.assertEqual(state["state"], "stopped")
        self.assertEqual(state["guard"]["stopped"]["boot_id"], "boot-b")
        self.reading["mem_available_bytes"] = 12 * GIB
        self.at(g, 200.0)
        self.assertEqual(self.at(g, 800.0)["level"], 0)
        state = self.at(g, 1100.0)
        self.assertEqual(self.starts, list(self.UNITS[:2]))
        self.assertEqual(state["guard"]["restart"]["skipped"], {})

    def test_a_manual_drain_after_the_stop_keeps_it(self):
        # codex #267 r1: a manual drain asked while stopped, or cancelled
        # before level 4, still keeps an earlier stop's units stopped
        for cancel_at in (140.0, 900.0):
            self.setUp()
            g = self.stop_then_clear(self.guard()) if cancel_at > 800 else self.guard()
            if cancel_at < 800:
                self.at(g, 0.0)
                self.at(g, 130.0)
            guard.request_drain(self.cfg.drain_request_path, 0.0, None)
            self.at(g, cancel_at - 5)
            guard.cancel_drain(self.cfg.drain_request_path)
            self.reading["mem_available_bytes"] = 12 * GIB
            for t in (cancel_at, cancel_at + 600, cancel_at + 1200, cancel_at + 1800):
                state = self.at(g, t)
            self.assertEqual(state["level"], 0, cancel_at)
            self.assertEqual(self.starts, [], cancel_at)
            self.assertEqual(set(state["guard"]["restart"]["skipped"].values()), {"manual"})

    def test_a_freeze_restarts_the_window(self):
        g = self.stop_then_clear(self.guard())
        self.reading["psi_cpu_10"] = 50.0
        self.assertEqual(self.at(g, 900.0)["level"], 2)
        del self.reading["psi_cpu_10"]
        self.assertEqual(self.at(g, 901.0)["level"], 1)     # thawed: low from 901
        self.at(g, 1100.0)
        self.assertEqual(self.starts, [])
        self.at(g, 1201.0)
        self.assertEqual(self.starts, list(self.UNITS[:2]))

    def test_kept_across_a_guard_restart(self):
        g = self.guard()
        self.at(g, 0.0)
        self.at(g, 130.0)
        self.reading["mem_available_bytes"] = 12 * GIB
        self.at(g, 200.0)
        self.at(g, 800.0)
        self.at(self.guard(), 1100.0)               # a new guard, same boot
        self.assertEqual(self.starts, list(self.UNITS[:2]))

    def test_never_after_a_reboot_nor_a_manual_drain(self):
        g = self.stop_then_clear(self.guard())
        self.boot[0] = "boot-b"
        state = self.at(g, 1100.0)
        self.assertEqual(self.starts, [])
        self.assertEqual(set(state["guard"]["restart"]["skipped"].values()), {"reboot"})
        # a stop of a drain the owner asked
        self.setUp()
        self.reading["mem_available_bytes"] = 12 * GIB
        guard.request_drain(self.cfg.drain_request_path, 0.0, None)
        g = self.guard()
        self.at(g, 0.0)
        self.assertEqual(self.at(g, 130.0)["state"], "stopped")
        self.assertTrue(guard.cancel_drain(self.cfg.drain_request_path))
        for t in (131.0, 800.0, 1500.0):
            state = self.at(g, t)
        self.assertEqual(self.starts, [])
        self.assertEqual(set(state["guard"]["restart"]["skipped"].values()), {"manual"})


def _sample_with_disk_free(free_bytes):
    """The real host reading, with the work_path disk fixed (#5321).

    `guard check` measures the runner's free disk, which made `caps.disk_mb`
    depend on the host. `read_signals` still goes through `sample()` (codex
    #211 r4), but the disk it reads is the one given here."""
    real = guard.resources.sample

    def sample(*args, **kwargs):
        reading = dict(real(*args, **kwargs))
        reading["disk_free_bytes"] = free_bytes
        return reading

    return mock.patch.object(guard.resources, "sample", sample)


class CliTest(unittest.TestCase):
    @staticmethod
    def _config(tmp):
        config = os.path.join(tmp, "guard.json")
        with open(config, "w") as fh:
            json.dump({"host": "vm-test", "max_agents": 2,
                       "state_path": os.path.join(tmp, "s", "state.json"),
                       "ack_path": os.path.join(tmp, "a", "ack.json"),
                       "work_path": tmp,
                       "lend": {"cpu_max": 2, "memory_max": "4GiB",
                                "disk_max": "1MiB"}}, fh)
        return config

    def test_check_and_observe_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            with mock.patch("sys.stdout") as out, \
                    _sample_with_disk_free(guard.DEFAULT_DISK_FREE_MIN + 1024 * 1024):
                self.assertEqual(guard.main(["check", "--config", config, "--json"]), 0)
            printed = "".join(c.args[0] for c in out.write.call_args_list)
            self.assertEqual(json.loads(printed)["schema"], "ameesh-host-state/1")
            # codex #211 r4: the disk of work_path, measured through sample()
            self.assertEqual(json.loads(printed)["caps"].get("disk_mb"), 1)
            # codex #211 r6: the offer as published, in bytes, and the schema
            self.assertEqual(json.loads(printed)["offer"]["memory_max"], 4 * GIB)
            self.assertEqual(schema_errors(schema("ameesh-host-state-1.schema.json"),
                                           json.loads(printed)), [])
            calls = []

            def run(argv, **kw):
                calls.append(argv)
                return mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(guard.subprocess, "run", run), \
                    mock.patch.object(guard.signal, "signal"):
                self.assertEqual(guard.main(["run", "--once", "--config", config]), 0)
            with open(os.path.join(tmp, "s", "state.json")) as fh:
                self.assertEqual(json.load(fh)["guard"]["mode"], "enforce")
            self.assertEqual(calls[:2], [
                ["systemctl", "stop", "ameesh-guard-freeze-ameesh-guest.service"],
                ["systemctl", "thaw", "ameesh-guest.slice"]])
            self.assertTrue(any(c[1:3] == ["set-property", "--runtime"] for c in calls))
            calls.clear()
            with mock.patch.object(guard.subprocess, "run", run):
                self.assertEqual(
                    guard.main(["run", "--observe", "--once", "--config", config]), 0)
            self.assertEqual([c for c in calls if c[0] == "systemctl"
                              and c[1] in ("set-property", "freeze", "thaw")], [])
            with open(os.path.join(tmp, "s", "state.json")) as fh:
                self.assertEqual(json.load(fh)["guard"]["kind"], "ameesh-guard")
            # manual drain: requested, seen by check, withdrawn
            drain = os.path.join(tmp, "drain.json")
            with open(config) as fh:
                raw = json.load(fh)
            with open(config, "w") as fh:
                json.dump(dict(raw, drain_request_path=drain), fh)
            with mock.patch("sys.stdout"):
                self.assertEqual(guard.main(["drain", "--for", "600", "--config", config]), 0)
            with open(drain) as fh:
                self.assertEqual(json.load(fh)["schema"], "ameesh-guard-drain/1")
            with mock.patch("sys.stdout") as out:
                guard.main(["check", "--config", config])
            self.assertIn("reason manual",
                          "".join(c.args[0] for c in out.write.call_args_list))
            with mock.patch("sys.stdout"):
                self.assertEqual(guard.main(["drain", "--cancel", "--config", config]), 0)
            self.assertFalse(os.path.exists(drain))
            with mock.patch("sys.stderr"):
                self.assertEqual(guard.main(["drain", "--for", "0", "--config", config]), 2)
            with open(config, "w") as fh:
                json.dump({"lend": {"cpu_max": "three"}}, fh)
            # mesh-design #459 r4c: an offer the canon refuses no longer stops
            # the guard; `check` reports the closed state and exits 0
            with mock.patch("sys.stdout") as out, mock.patch("sys.stderr"):
                self.assertEqual(guard.main(["check", "--config", config, "--json"]), 0)
            closed = json.loads("".join(c.args[0] for c in out.write.call_args_list))
            self.assertEqual((closed["level"], closed["reason"]), (3, "offer_invalid"))
            self.assertEqual(closed["caps"]["max_concurrent"], 0)
            self.assertEqual(schema_errors(schema("ameesh-host-state-1.schema.json"),
                                           closed), [])
            # the slice goes into the freezing unit's ExecStopPost= line
            with open(config, "w") as fh:
                json.dump({"slice": "x.slice --now", "lend": {"cpu_max": 1}}, fh)
            with mock.patch("sys.stderr"):
                self.assertEqual(guard.main(["check", "--config", config]), 1)

    def test_check_with_disk_below_the_floor_is_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            with mock.patch("sys.stdout") as out, \
                    _sample_with_disk_free(guard.DEFAULT_DISK_FREE_MIN - 1):
                self.assertEqual(guard.main(["check", "--config", config, "--json"]), 0)
            printed = "".join(c.args[0] for c in out.write.call_args_list)
            state = json.loads(printed)
            self.assertEqual(state["caps"].get("disk_mb"), 0)
            self.assertEqual(schema_errors(schema("ameesh-host-state-1.schema.json"),
                                           state), [])


class ClosedOfferTest(unittest.TestCase):
    """mesh-design #459 r4c: an absent or refused offer starts the guard
    CLOSED — level 3, `max_concurrent` 0, nothing lendable published, the
    offer published empty — and that level 3 applies to a guest unit already
    started."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l159e-closed-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = []
        self.clock = [1000.0]
        self.reading = {"cpu_count": 4, "mem_total_bytes": 16 * GIB,
                        "mem_available_bytes": 12 * GIB, "disk_free_bytes": 50 * GIB}
        patcher = mock.patch.multiple(guard.resources, lendable=None, measure=None,
                                      create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, lend_value):
        return guard.GuardConfig(
            host="vm-test", max_agents=2, offer=guard.parse_offer(lend_value),
            state_path=os.path.join(self.tmp, "state.json"),
            ack_path=os.path.join(self.tmp, "ack.json"),
            drain_request_path=os.path.join(self.tmp, "drain.json"))

    def fake(self, argv, **kw):
        self.calls.append(argv)
        return mock.Mock(returncode=0, stdout="", stderr="")

    def guard(self, cfg, observe=False):
        return guard.Guard(cfg, observe=observe, signals=lambda now: dict(self.reading),
                           clock=lambda: self.clock[0],
                           enforcer=guard.Enforcer(cfg, run=self.fake, log=lambda t: None,
                                                   clock=lambda: self.clock[0]))

    def stops(self):
        return [c[1:] for c in self.calls if c[1] in ("stop", "kill")
                and not c[2].startswith(guard.FREEZE_UNIT)]

    def test_les_offres_absentes_ou_refusees_ferment_la_garde(self):
        cases = ((None, "no_offer"),
                 (lend(reclaim={"disk_paths": ["rel/x"]}), "offer_invalid"),
                 (lend(reclaim={"disk_free_min": 0}), "offer_invalid"))
        for raw, reason in cases:
            with self.subTest(reason=reason, raw=raw):
                with mock.patch("sys.stderr", new_callable=io.StringIO):
                    cfg = self.config(raw)
                    g = self.guard(cfg)
                # several cycles, before the drain's deadline: the level never
                # comes back down and nothing lendable is published
                for now in (1000.0, 1005.0, 1010.0, 1015.0):
                    self.clock[0] = now
                    state = g.step()
                    where = (reason, now)
                    self.assertEqual(state["level"], 3, where)
                    self.assertEqual(state["state"], "draining", where)
                    self.assertEqual(state["reason"], reason, where)
                    self.assertEqual(state["caps"]["max_concurrent"], 0, where)
                    for key in ("cpu_share", "memory_mb", "memory_high_mb", "disk_mb"):
                        self.assertNotIn(key, state["caps"], where)
                    self.assertEqual(state["offer"], {}, where)
                    self.assertEqual(state["measure"]["lendable"],
                                     {"cpu": None, "memory_bytes": None, "disk_bytes": None},
                                     where)
                    self.assertEqual(schema_errors(
                        schema("ameesh-host-state-1.schema.json"), state), [], where)
                self.assertEqual(self.stops(), [],
                                 "a closed offer stops nothing before the deadline")
                os.unlink(cfg.state_path)

    def test_une_unite_invitee_deja_lancee_est_drainee_puis_arretee(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            cfg = self.config(lend(reclaim={"disk_free_min": 0}))
            g = self.guard(cfg)
        state = g.step()
        self.assertEqual((state["level"], state["state"]), (3, "draining"))
        self.assertEqual(state["drain_deadline_ts"], 1090.0)   # since_ts + 90 s
        self.clock[0] = 1085.0
        g.step()
        self.assertEqual(self.stops(), [], "the guest's jobs run until the deadline")
        self.clock[0] = 1090.0
        g.step()
        self.assertEqual(self.stops(), [["stop", "ameesh-job-*.scope"]],
                         "the deadline drains the jobs of a guest already started")
        self.clock[0] = 1130.0                    # since_ts + drain + grace, no ack
        self.assertEqual(g.step()["level"], 4)
        self.clock[0] = 1135.0
        g.step()
        self.assertIn(["stop", "ameesh-guest.slice"], self.stops(),
                      "without an acknowledgement, level 4 stops the guest")
        # a guest that acknowledges: level 3 forever, never stopped
        os.unlink(cfg.state_path)
        if os.path.exists(cfg.ack_path):
            os.unlink(cfg.ack_path)
        self.calls.clear()
        self.clock[0] = 2000.0
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            g = self.guard(cfg)
        drain = g.step()
        with open(cfg.ack_path, "w") as fh:
            json.dump({"schema": "ameesh-host-ack/1", "seq": drain["seq"],
                       "state": "draining", "drained": True, "in_turn": []}, fh)
        for t in (2090.0, 2130.0, 3000.0):
            self.clock[0] = t
            state = g.step()
            self.assertEqual((state["level"], state["caps"]["max_concurrent"], state["state"]),
                             (3, 0, "draining"), t)
        self.assertEqual([s for s in self.stops() if s[-1] == "ameesh-guest.slice"], [],
                         "an acknowledged drain never stops the guest")


if __name__ == "__main__":
    unittest.main()
