# SPDX-License-Identifier: AGPL-3.0-only
"""L159f: the host gate on `main` — the classic runner obeys the state a
capacity guard writes (study L159 sections 1.5 to 1.8, docs/GARDE-DE-CAPACITE.md).

Unit tests without a database (contract agreement, defaults, freeze, the
controller, background work), then the runner itself on the bench's Postgres
with the fake `claude` harness, and relocation reading the destination gate.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest

from ameesh import background, capacity_guard, config as config_mod, host_gate, registry
from ameesh import relocation, storage
from ameesh.runner import AgentWorker

from .support import EMPTY_CONFIG, REPO, PgTestCase

GOLDEN = os.path.join(REPO, "tests", "dore", "capacity_guard")


def _golden(name: str) -> dict:
    with open(os.path.join(GOLDEN, name), encoding="utf-8") as fh:
        return json.load(fh)


def _write_state(path: str, **fields) -> dict:
    doc = {"schema": capacity_guard.SCHEMA_HOST_STATE, "seq": 1, "state": "available",
           "expires_ts": time.time() + 90}
    doc.update(fields)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)
    return doc


class ContractAgreementTest(unittest.TestCase):
    """The gate and `capacity_guard.gate_state` are one rule."""

    def test_golden_freshness(self):
        for case in _golden("freshness.json")["cases"]:
            with self.subTest(case["why"]):
                fallback = "stopped" if case["lending"] else "available"
                try:
                    state = host_gate.parse_state(case["doc"], now=case["now"],
                                                  lending=case["lending"]).state
                except ValueError:
                    state = fallback
                self.assertEqual(state, case["expect"])

    def test_non_finite_numbers_are_unreadable(self):
        doc = {"schema": capacity_guard.SCHEMA_HOST_STATE, "state": "draining", "seq": 3,
               "expires_ts": 1090.0}
        for key, value in (("drain_deadline_ts", float("nan")),
                           ("drain_deadline_ts", float("inf")),
                           ("until_ts", "soon")):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    host_gate.parse_state(dict(doc, **{key: value}), now=1000.0, lending=True)
        with self.assertRaises(ValueError):
            host_gate.parse_state(dict(doc, caps={"max_concurrent": float("nan")}),
                                  now=1000.0, lending=True)

    def test_unknown_fields_are_ignored(self):
        state = host_gate.parse_state(
            {"schema": capacity_guard.SCHEMA_HOST_STATE, "state": "available", "seq": 4,
             "expires_ts": 1090.0, "measure": {"x": 1}, "guard": {"kind": "ameesh-guard"},
             "caps": {"max_concurrent": 2, "cpu_weight": 10}},
            now=1000.0, lending=True)
        self.assertEqual((state.state, state.max_concurrent), ("available", 2))


class DefaultsTest(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        # Gate choices in each case model a fresh process.
        for name in ("_guest_identity", "_native_identity"):
            patch = mock.patch.object(host_gate, name, False)
            patch.start()
            self.addCleanup(patch.stop)

    def cfg(self, **kw):
        return config_mod.Config(**dict({"runner_id": "guest-test"}, **kw))

    def test_a_lending_host_has_a_closed_file_gate(self):
        self.assertEqual(host_gate.gate_kind(self.cfg(), lending=True), "file")
        gate = host_gate.from_config(self.cfg(), lending=True)
        self.assertEqual((gate.path, gate.ack_path, gate.fallback, gate.lending),
                         (capacity_guard.STATE_PATH, capacity_guard.ACK_PATH, "stopped", True))

    def test_a_classic_host_has_no_gate_unless_configured(self):
        self.assertIsNone(host_gate.from_config(self.cfg(runner_id="native-test"), lending=False))
        gate = host_gate.from_config(self.cfg(host_gate="file"), lending=False)
        self.assertEqual((gate.fallback, gate.lending), ("available", False))
        gate = host_gate.from_config(self.cfg(host_gate="file", host_gate_fallback="stopped"))
        self.assertTrue(gate.lending)  # closed by default: freshness of a lending host

    def test_none_disables_even_on_a_lending_host(self):
        self.assertIsNone(host_gate.from_config(self.cfg(runner_id="native-test", host_gate="none"), lending=True))

    def test_drain_seconds(self):
        self.assertEqual(host_gate.drain_seconds(self.cfg()), 90.0)
        self.assertEqual(host_gate.drain_seconds(self.cfg(), {"drain_seconds": 120}), 120.0)
        self.assertEqual(host_gate.drain_seconds(self.cfg(host_gate_drain=30),
                                                 {"drain_seconds": 120}), 30.0)

    def test_paths_are_the_directory_form(self):
        # a `rename` is only visible through a mount of the directory
        self.assertEqual(capacity_guard.STATE_PATH, "/run/ameesh-gate/state/state.json")
        self.assertEqual(capacity_guard.ACK_PATH, "/run/ameesh-gate/ack/ack.json")

    def test_environment(self):
        env = {"AMEESH_CONFIG": EMPTY_CONFIG, "AMEESH_HOST_GATE": "File",
               "AMEESH_HOST_GATE_FALLBACK": "stopped", "AMEESH_HOST_GATE_DRAIN": "45",
               "AMEESH_HOST_GATE_PATH": "/g/state/state.json"}
        cfg = config_mod.load(env=env)
        self.assertEqual((cfg.host_gate, cfg.host_gate_fallback, cfg.host_gate_drain,
                          cfg.host_gate_path), ("file", "stopped", 45.0, "/g/state/state.json"))
        cfg = config_mod.load(env=dict(env, AMEESH_HOST_GATE_DRAIN="inf"))
        self.assertEqual(cfg.host_gate_drain, 0.0)
        for key, value in (("AMEESH_HOST_GATE", "socket"),
                           ("AMEESH_HOST_GATE_FALLBACK", "open")):
            with self.subTest(key=key), self.assertRaises(SystemExit):
                config_mod.load(env=dict(env, **{key: value}))


class FileGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "state"))
        os.makedirs(os.path.join(self.tmp, "ack"))
        self.path = os.path.join(self.tmp, "state", "state.json")
        self.ack = os.path.join(self.tmp, "ack", "ack.json")

    def gate(self, lending: bool, **kw) -> host_gate.FileGate:
        return host_gate.FileGate(self.path, self.ack, lending=lending,
                                  fallback="stopped" if lending else "available",
                                  use_inotify=False, **kw)

    def test_absent_state(self):
        self.assertEqual(self.gate(True).state().state, "stopped")
        self.assertEqual(self.gate(False).state().state, "available")

    def test_stale_state_on_a_lending_host(self):
        logs: list[str] = []
        _write_state(self.path, expires_ts=time.time() - 1)
        self.assertEqual(self.gate(True, log=logs.append).state().state, "stopped")
        self.assertIn("stale", logs[0])
        _write_state(self.path, expires_ts=None)
        self.assertEqual(self.gate(True).state().state, "stopped")
        self.assertEqual(self.gate(False).state().state, "available")

    def test_a_huge_integer_closes_the_gate(self):
        # 10**400 is valid JSON; math.isfinite on it raises OverflowError: the
        # state is unreadable (fallback), never left `available`
        for field in ({"expires_ts": 10 ** 400}, {"until_ts": 10 ** 400},
                      {"caps": {"max_concurrent": 10 ** 400}}):
            with self.subTest(field=sorted(field)):
                _write_state(self.path, **field)
                self.assertEqual(self.gate(True).state().state, "stopped")

    def test_ack_goes_to_its_own_directory(self):
        self.gate(True).acknowledge(host_gate.GateAck(seq=8, state="draining", drained=True))
        with open(self.ack, encoding="utf-8") as fh:
            ack = json.load(fh)
        self.assertEqual(sorted(ack), ["drained", "held", "in_turn", "schema", "seq",
                                       "state", "ts"])
        self.assertEqual((ack["seq"], ack["drained"]), (8, True))
        # the contract's closed acknowledgement
        valid = [c["doc"] for c in _golden("ack.json")["valid"]]
        self.assertTrue(any(sorted(v) == sorted(ack) for v in valid))


    def read_ack(self) -> dict:
        with open(self.ack, encoding="utf-8") as fh:
            return json.load(fh)

    def test_an_idle_runner_never_says_drained_for_a_busy_one(self):
        # codex r2 #279: ack.json is shared by the host's runners, `drained`
        # was computed by each alone: an idle one overwrote a busy one's ack
        code = ("import sys, time; sys.path.insert(0, %r)\n"
                "from ameesh import host_gate\n"
                "gate = host_gate.FileGate(%r, %r, lending=True, fallback='stopped',"
                " use_inotify=False)\n"
                "gate.acknowledge(host_gate.GateAck(seq=3, state='draining',"
                " in_turn=('busy',), held=('busy',)))\n"
                "print('acked', flush=True)\n"
                "time.sleep(60)\n" % (os.path.join(REPO, "src"), self.path, self.ack))
        busy = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(busy.stdout.close)
        self.addCleanup(busy.wait)
        self.addCleanup(busy.kill)
        self.assertEqual(busy.stdout.readline().strip(), "acked")
        idle = self.gate(True)
        self.addCleanup(idle.close)
        self.assertTrue(idle.acknowledge(host_gate.GateAck(seq=3, state="draining",
                                                           drained=True)))
        ack = self.read_ack()
        self.assertEqual((ack["seq"], ack["drained"], ack["held"], ack["in_turn"]),
                         (3, False, ["busy"], ["busy"]))
        # the busy runner dies: its part goes, the next merge says drained
        busy.kill()
        busy.wait()
        self.assertTrue(idle.merge())
        ack = self.read_ack()
        self.assertEqual((ack["drained"], ack["held"]), (True, []))
        parts = os.listdir(os.path.join(self.tmp, "ack", "parts"))
        self.assertEqual(len([n for n in parts if n.endswith(".json")]), 1)  # the dead part went

    def test_a_closed_gate_writes_no_part(self):
        # codex r3 #279: an ack (a tick running during `stop`) after close()
        # recreated the part, which then held the host's ack until exit
        gate = self.gate(True)
        self.assertTrue(gate.acknowledge(host_gate.GateAck(seq=2, state="draining")))
        gate.close()
        self.assertFalse(gate.acknowledge(host_gate.GateAck(seq=2, state="draining")))
        gate.merge()  # nothing of its own left to merge
        parts = os.path.join(self.tmp, "ack", "parts")
        self.assertEqual([n for n in os.listdir(parts) if not n.startswith(".")], [])

    def test_a_once_turn_keeps_the_host_from_being_drained(self):
        # codex r3 #279: with --once, no controller and no part: another
        # runner said the host drained during that turn. The `--once` worker
        # (`gate_foreground`) counts in this runner's part
        _write_state(self.path, seq=5, state="draining", drain_deadline_ts=time.time() + 600)
        idle = host_gate.GateController(
            types.SimpleNamespace(workers={}, lock=threading.Lock()), self.gate(True))
        once_runner = types.SimpleNamespace(workers={}, lock=threading.Lock(),
                                            gate_foreground=FakeWorker("solo", in_turn=True))
        once = host_gate.GateController(once_runner, self.gate(True))
        self.addCleanup(idle.stop)
        self.addCleanup(once.stop)
        for ctl in (idle, once):
            ctl.apply(ctl.gate.state())
        once.tick()
        idle.tick()
        ack = self.read_ack()
        self.assertEqual((ack["seq"], ack["drained"], ack["in_turn"]), (5, False, ["solo"]))
        once_runner.gate_foreground = None  # the turn ended, its lease returned
        once.tick()
        idle.tick()
        self.assertTrue(self.read_ack()["drained"])

    def test_a_part_for_an_older_seq_is_not_drained(self):
        first, second = self.gate(True), self.gate(True)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.acknowledge(host_gate.GateAck(seq=2, state="draining", drained=True))
        second.acknowledge(host_gate.GateAck(seq=3, state="draining", drained=True))
        self.assertEqual((self.read_ack()["seq"], self.read_ack()["drained"]), (3, False))
        first.acknowledge(host_gate.GateAck(seq=3, state="draining", drained=True))
        self.assertTrue(self.read_ack()["drained"])
        # a runner leaving (gate closed) takes its part away
        first.close()
        second.acknowledge(host_gate.GateAck(seq=4, state="draining", drained=True))
        self.assertEqual((self.read_ack()["seq"], self.read_ack()["drained"]), (4, True))

def slot_runner(ack: str, cap, workers=None):
    """A runner reduced to the gate's slots: the real `Runner` methods, on a
    plain object (no database)."""
    from ameesh.runner import Runner

    class SlotRunner:
        _gate_slot = Runner._gate_slot
        _slot_pool = staticmethod(Runner._slot_pool)
        _slot_error = Runner._slot_error
        _hold_slots_of = Runner._hold_slots_of
        gate_hold_slots = Runner.gate_hold_slots
        _release_slot = Runner._release_slot

    runner = SlotRunner()
    runner.host_gate = types.SimpleNamespace(
        max_concurrent=cap if callable(cap) else (lambda: cap),
        gate=types.SimpleNamespace(ack_path=ack))
    runner.lock = threading.Lock()
    runner.workers = workers if workers is not None else {}
    runner._gate_slots = {}
    runner._gate_slots_lock = threading.RLock()
    return runner


class SlotPoolTest(unittest.TestCase):
    """`caps.max_concurrent` per host: two runners share the cap."""

    def setUp(self):
        self.dir = os.path.join(tempfile.mkdtemp(), "ack", "slots")

    def hold_in_another_process(self):
        # another runner of the same host, holding one slot until killed
        code = ("import sys, time; sys.path.insert(0, %r)\n"
                "from ameesh import host_gate\n"
                "slot = host_gate.SlotPool(%r).acquire(1)\n"
                "print('held' if slot is not None else 'refused', flush=True)\n"
                "time.sleep(60)\n" % (os.path.join(REPO, "src"), self.dir))
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertEqual(proc.stdout.readline().strip(), "held")
        return proc

    def test_two_runners_share_the_cap(self):
        other = self.hold_in_another_process()
        pool = host_gate.SlotPool(self.dir)
        self.assertIsNone(pool.acquire(1))  # the other runner's agent counts
        mine = pool.acquire(2)
        self.assertIsNotNone(mine)
        self.assertIsNone(pool.acquire(2))
        host_gate.SlotPool.release(mine)
        # the other runner dies: its slot is free again
        other.kill()
        other.wait()
        self.assertIsNotNone(pool.acquire(1))

    def test_two_runners_capped_at_one_admit_one_agent(self):
        # codex r1 #279: each runner counted only its own workers
        from ameesh.runner import Runner
        ack = os.path.join(os.path.dirname(self.dir), "ack.json")

        first, second = slot_runner(ack, 1), slot_runner(ack, 1)
        admitted, slot = Runner._gate_slot(first, {"name": "a"})
        self.assertTrue(admitted)
        first._gate_slots["a"] = slot
        self.assertEqual(Runner._gate_slot(second, {"name": "b"}), (False, None))
        Runner._release_slot(first, "a")  # a's worker ended
        admitted, slot = Runner._gate_slot(second, {"name": "b"})
        self.assertTrue(admitted)
        host_gate.SlotPool.release(slot)

    def fake_runner(self, cap, workers=None):
        ack = os.path.join(os.path.dirname(self.dir), "ack.json")
        return slot_runner(ack, lambda: cap["value"], workers)

    def test_agents_admitted_without_a_cap_count_when_one_is_set(self):
        # codex r2 #279: a worker admitted without max_concurrent held no slot,
        # so a cap set to 1 later did not count it
        from ameesh.runner import Runner
        cap = {"value": None}
        runner = self.fake_runner(cap)
        admitted, slot = Runner._gate_slot(runner, {"name": "a"})
        self.assertTrue(admitted)
        self.assertIsNotNone(slot)  # a slot even without a cap
        runner._gate_slots["a"] = slot
        runner.workers["a"] = FakeWorker("a")
        cap["value"] = 1
        self.assertEqual(Runner._gate_slot(runner, {"name": "b"}), (False, None))
        # a worker from before the gate (no slot yet) takes one at the next claim
        Runner._release_slot(runner, "a")
        self.assertEqual(Runner._gate_slot(runner, {"name": "b"}), (False, None))
        self.assertIsNotNone(runner._gate_slots["a"])
        Runner._release_slot(runner, "a")

    def test_unusable_slots_refuse_under_a_cap(self):
        # codex r2 #279: the local count admitted one agent per executor
        from ameesh.runner import Runner
        os.makedirs(os.path.dirname(self.dir), exist_ok=True)
        with open(self.dir, "w"):  # `slots` is a file: the folder is unusable
            pass
        cap = {"value": 1}
        runner = self.fake_runner(cap)
        self.assertEqual(Runner._gate_slot(runner, {"name": "a"}), (False, None))
        cap["value"] = None  # without a cap, nothing to count: admitted
        self.assertEqual(Runner._gate_slot(runner, {"name": "a"}), (True, None))

    def test_workers_from_before_the_gate_hold_their_slot_at_once(self):
        # codex r3 #279: a worker from before the gate took its slot only at
        # its runner's next claim; with nothing to claim on A, B admitted past
        # the cap. The controller makes them hold one as it starts
        ack = os.path.join(os.path.dirname(self.dir), "ack.json")
        a = slot_runner(ack, 1, workers={"old": FakeWorker("old")})
        b = slot_runner(ack, 1)
        gate = FakeGate(host_gate.GateState("available", 1))
        ctl = host_gate.GateController(a, gate)
        self.addCleanup(ctl.stop)
        ctl.start()  # A claims nothing, ever
        self.assertIsNotNone(a._gate_slots.get("old"))
        self.assertEqual(host_gate.SlotPool(self.dir).acquire(1), None)
        self.assertEqual(b._gate_slot({"name": "new"}), (False, None))
        # and at each tick: a worker A registered meanwhile (no claim) too
        a.workers["late"] = FakeWorker("late")
        ctl.tick()
        self.assertIsNotNone(a._gate_slots.get("late"))
        for name in ("old", "late"):
            a._release_slot(name)

    def test_a_failed_slot_open_leaks_no_slot(self):
        # codex r3 #279: slot-0 free and opened, opening slot-1 fails: the fd
        # of slot-0 stayed open and locked, the slot lost until the runner died
        pool = host_gate.SlotPool(self.dir)
        host_gate.SlotPool.release(pool.acquire(None))  # slot-0, now free
        os.makedirs(os.path.join(self.dir, "slot-1"))  # opening it fails
        with self.assertRaises(OSError):
            pool.acquire(2)
        os.rmdir(os.path.join(self.dir, "slot-1"))
        held = [pool.acquire(1)]
        self.assertIsNotNone(held[0])  # slot-0 is free again
        host_gate.SlotPool.release(held[0])

    def test_a_lower_cap_counts_every_held_slot(self):
        pool = host_gate.SlotPool(self.dir)
        held = [pool.acquire(3) for _ in range(3)]
        host_gate.SlotPool.release(held[0])  # slot-0 free, slot-1 and slot-2 held
        self.assertIsNone(pool.acquire(2))
        self.assertIsNotNone(pool.acquire(3))


class PartialSyncKeepsTheGateTest(unittest.TestCase):
    def test_a_sync_missing_a_canon_neither_removes_nor_changes_the_gate(self):
        # codex r2 #279: after a complete sync, a sync where only the classic
        # canon was read refreshed the limits on that subset and removed the
        # gate of a lending host, while `_gate_settled` stayed set
        from unittest import mock
        from ameesh.runner import Runner
        calls: list = []
        runner = types.SimpleNamespace(host="h1", _sync_host_gate=lambda *a: calls.append(a))
        # #3101 : refresh_host_limits compte les admissions épinglées du canon ;
        # le faux canon d'un sync partiel en porte zéro.
        canon = types.SimpleNamespace(host=None, hosts=[], placements=[])
        mine = {"limits": {}, "max_agents": None, "origin": None}
        with mock.patch("ameesh.resources.host_limits", return_value=mine), \
                mock.patch("ameesh.menage.effective", return_value=None):
            Runner.refresh_host_limits(runner, canon, complete=False)
            self.assertEqual(calls, [])
            Runner.refresh_host_limits(runner, canon)
            self.assertEqual(calls, [(False, None)])


class FakeWorker:
    def __init__(self, name: str, in_turn: bool = False):
        self.name = name
        self.proc = object() if in_turn else None
        self.wake = threading.Event()
        self.host_yield = None
        self.yields: list[str] = []
        self.alive = True

    def is_alive(self):
        return self.alive

    def yield_to_host(self, reason):
        self.host_yield = reason
        self.yields.append(reason)


class FakeGate:
    def __init__(self, state):
        self.st = state
        self.acks: list = []
        self.path, self.fallback = "/fake", "stopped"
        self.fail_acks = 0

    def state(self):
        return self.st

    def wait_change(self, timeout):
        time.sleep(min(timeout, 0.05))
        return self.st

    def acknowledge(self, ack):
        if self.fail_acks:
            self.fail_acks -= 1
            return False
        self.acks.append(ack)
        return True

    def close(self):
        pass


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.runner = types.SimpleNamespace(
            workers={}, lock=threading.Lock(), wake_all=threading.Event(),
            background=background.Tracker(types.SimpleNamespace(db=None,
                                                                container_runtime=lambda: None),
                                          log=lambda _m: None))
        self.gate = FakeGate(host_gate.GateState("available", 1))
        self.ctl = host_gate.GateController(self.runner, self.gate, drain_s=90,
                                            clock=lambda: self.now)
        self.ctl.apply(self.gate.state())

    def state(self, name, seq, **kw):
        st = host_gate.GateState(name, seq, **kw)
        self.gate.st = st
        self.ctl.apply(st)
        return st

    def test_draining_yields_at_the_deadline_then_drains(self):
        worker = FakeWorker("a", in_turn=True)
        self.runner.workers["a"] = worker
        self.state("draining", 2, drain_deadline_ts=1060.0, reason="app_memory")
        self.assertTrue(worker.wake.is_set())
        ack = self.ctl.tick()
        self.assertEqual((ack.seq, ack.state, ack.in_turn, ack.drained),
                         (2, "draining", ("a",), False))
        self.assertEqual(worker.yields, [])  # before the deadline: the safe point decides
        self.now = 1060.0
        self.ctl.tick()
        self.assertEqual(worker.yields, ["host drain deadline"])
        worker.alive = False
        ack = self.ctl.tick()
        self.assertTrue(ack.drained)
        self.assertEqual(self.gate.acks[-1].seq, 2)

    def test_the_deadline_also_stops_a_turn_closed_at_its_safe_point(self):
        # the harness stopped at its safe point, but a child keeps its output
        # open: the turn is still running at the deadline, and is stopped
        from ameesh import runner as runner_mod
        self.assertEqual(runner_mod.AgentWorker.HOST_SAFE_POINT, host_gate.SAFE_POINT)
        worker = FakeWorker("a", in_turn=True)
        worker.host_yield = host_gate.SAFE_POINT
        self.runner.workers["a"] = worker
        self.state("draining", 2, drain_deadline_ts=1060.0, reason="app_memory")
        self.ctl.tick()
        self.assertEqual(worker.yields, [])
        self.now = 1060.0
        self.ctl.tick()
        self.assertEqual(worker.yields, ["host drain deadline"])
        self.ctl.tick()
        self.assertEqual(worker.yields, ["host drain deadline"])  # once

    def test_a_failed_ack_is_written_again(self):
        # a transient write failure: the same ack (here `drained`) is retried at
        # the next tick, not taken for published
        self.state("stopped", 3, reason="host_memory")
        self.gate.fail_acks = 1
        ack = self.ctl.tick()
        self.assertTrue(ack.drained)
        self.assertEqual([a.seq for a in self.gate.acks if a.drained], [])
        self.ctl.tick()
        self.assertEqual([(a.seq, a.drained) for a in self.gate.acks][-1], (3, True))

    def test_stopped_yields_at_once_and_a_deadline_never_moves_back(self):
        worker = FakeWorker("a", in_turn=True)
        self.runner.workers["a"] = worker
        self.state("draining", 2, drain_deadline_ts=1500.0)
        self.state("draining", 3, drain_deadline_ts=2000.0)
        self.assertEqual(self.ctl.deadline, 1500.0)
        self.state("stopped", 4)
        self.ctl.tick()
        self.assertEqual(worker.yields, ["host stopped"])

    def test_background_work_is_cut_at_the_deadline_and_counted(self):
        entry = self.runner.background.start(agent="a", turn_id="t" * 8, pgid=None,
                                             members=[], containers=[], grace_s=1200,
                                             now=self.now)
        self.state("draining", 2)  # no deadline in the state: now + 90 s
        ack = self.ctl.tick()
        self.assertEqual(entry.deadline, 1090.0)
        self.assertTrue(entry.capped)
        self.assertFalse(ack.drained)  # background work still running
        entry.state = "done"
        self.assertTrue(self.ctl.tick().drained)

    def test_refresh_reads_the_gate_not_the_last_applied_state(self):
        # r4 P2: the guard closed the gate since the loop's last pass;
        # `tick` alone keeps the applied state, `refresh` reads the file
        self.gate.st = host_gate.GateState("draining", 2, reason="app_busy")
        self.ctl.tick()
        self.assertFalse(self.ctl.holds_turns())
        self.assertFalse(self.ctl.refresh().may_claim)
        self.assertTrue(self.ctl.holds_turns())
        self.assertEqual(self.gate.acks[-1].state, "draining")

    def test_refresh_never_reopens_with_a_state_read_before_a_newer_one(self):
        # r5: refresh reads available seq 1; meanwhile the loop applies
        # stopped seq 2; refresh's late apply must not reopen the controller
        read = self.gate.state

        def racing_read():
            st = read()
            self.state("stopped", 2, reason="host_memory")  # the loop, in between
            return st

        self.gate.state = racing_read
        self.assertFalse(self.ctl.refresh().may_claim)
        self.assertTrue(self.ctl.holds_turns())
        self.assertEqual(self.ctl.current.seq, 2)
        # the same seq closing (the fallback of an unreadable file) applies
        self.gate.state = read
        self.state("available", 3)
        self.gate.st = host_gate.fallback_state("stopped", 3)
        self.assertFalse(self.ctl.refresh().may_claim)

    def test_available_reopens_and_wakes_the_runner(self):
        self.state("stopped", 2)
        self.assertTrue(self.ctl.holds_turns())
        self.runner.wake_all.clear()
        self.state("available", 3, caps={"max_concurrent": 1})
        self.assertFalse(self.ctl.holds_turns())
        self.assertTrue(self.runner.wake_all.is_set())
        self.assertEqual(self.ctl.max_concurrent(), 1)
        self.assertIsNone(self.ctl.deadline)

    def test_frozen_time(self):
        self.state("available", 2, freeze={"since_ts": 990.0, "until_ts": 1020.0})
        self.state("available", 3)  # thawed; the interval stays known
        self.assertEqual(self.ctl.frozen_between(980.0, 1100.0), 30.0)
        self.assertEqual(self.ctl.frozen_between(1000.0, 1100.0), 20.0)
        self.assertEqual(self.ctl.frozen_between(1030.0, 1100.0), 0.0)
        # without an end, and never more than 60 s
        self.now = 2000.0
        self.state("available", 4, freeze={"since_ts": 1900.0, "until_ts": 2500.0})
        self.assertEqual(self.ctl.frozen_between(1800.0, 2100.0), 60.0)
        # a freeze goes with `available` only
        span = host_gate.GateState("draining", 5,
                                   freeze={"since_ts": 1.0, "until_ts": 2.0}).freeze_span
        self.assertIsNone(span)

    def test_snapshot_bounds_the_reason(self):
        self.state("draining", 9, reason="x" * 500)
        snap = self.ctl.snapshot()
        self.assertEqual((snap["gate_state"], snap["gate_seq"], len(snap["gate_reason"])),
                         ("draining", 9, host_gate.REASON_MAX))


class TurnDurationTest(unittest.TestCase):
    def test_frozen_time_is_not_counted(self):
        fake = types.SimpleNamespace(
            context_max_tokens=lambda: 0, turn_max_seconds=lambda: 100,
            session_policy=lambda: "par-lot",
            runner=types.SimpleNamespace(gate_frozen_between=lambda a, b: 30.0))
        self.assertIsNone(AgentWorker.turn_close_due(fake, 1000.0, 0, now=1120.0))
        self.assertIsNotNone(AgentWorker.turn_close_due(fake, 1000.0, 0, now=1130.0))


class RelocationGateTest(unittest.TestCase):
    def test_gate_open(self):
        offer = {"memory_max": 1}
        self.assertTrue(relocation.gate_open({}, None))
        self.assertFalse(relocation.gate_open({}, offer))  # lending, no gate recorded
        fresh = {"gate_state": "available", "sampled_ts": 1000.0}
        self.assertTrue(relocation.gate_open(fresh, offer, now=1010.0))
        for state in ("draining", "stopped"):
            self.assertFalse(relocation.gate_open(dict(fresh, gate_state=state), None,
                                                  now=1010.0))
        # a destination that died after publishing `available`: its reading
        # ages, and its gate no longer counts as open
        self.assertFalse(relocation.gate_open(fresh, offer, now=1000.0 + 301))
        self.assertTrue(relocation.gate_open(fresh, offer, now=1000.0 + 301, max_age_s=600))
        self.assertFalse(relocation.gate_open({"gate_state": "available"}, offer))


class ClaimErrorTest(PgTestCase):
    def test_a_claim_error_gives_the_slot_back(self):
        # codex r2 #279: an exception of registry.claim kept the slot reserved
        import dataclasses
        from unittest import mock
        from ameesh.runner import Runner
        self.register("solo", "claude", cwd=self.tmp, prompt="consigne")
        runner = Runner(dataclasses.replace(self.cfg, poll=1.0), self.db, once=False)
        self.addCleanup(runner.stop.set)
        slots = os.path.join(self.tmp, "gate", "ack", "slots")
        runner.host_gate = types.SimpleNamespace(
            max_concurrent=lambda: 1, holds_turns=lambda: False,
            gate=types.SimpleNamespace(ack_path=os.path.join(os.path.dirname(slots), "ack.json")))
        runner._gate_settled.set()
        with mock.patch.object(registry, "claim", side_effect=RuntimeError("db gone")):
            with self.assertRaises(RuntimeError):
                runner.sweep()
        self.assertEqual(runner._gate_slots, {})
        slot = host_gate.SlotPool(slots).acquire(1)  # the only slot is free
        self.assertIsNotNone(slot)
        host_gate.SlotPool.release(slot)


class OnceTurnTest(PgTestCase):
    """codex r3 #279: `--once` runs its turn in the foreground, outside
    `workers`; its runner had neither controller nor ack part."""

    def once_runner(self, holds):
        import dataclasses
        from ameesh.runner import Runner
        self.register("solo", "claude", cwd=self.tmp, prompt="consigne")
        runner = Runner(dataclasses.replace(self.cfg, poll=1.0), self.db, once=True)
        self.addCleanup(runner.stop.set)
        self.events: list = []
        runner.host_gate = types.SimpleNamespace(
            max_concurrent=lambda: None, holds_turns=lambda: holds["value"],
            describe=lambda: "draining (test, seq 2)",
            tick=lambda: self.events.append(("tick", runner.gate_foreground is not None)),
            refresh=lambda: (runner.host_gate.tick(),
                             types.SimpleNamespace(may_claim=not holds["value"]))[1],
            gate=types.SimpleNamespace(ack_path=os.path.join(self.tmp, "gate", "ack.json")))
        runner._gate_settled.set()
        return runner

    def test_the_part_says_held_before_the_turn_and_free_after(self):
        from unittest import mock
        from ameesh import runner as runner_mod
        runner = self.once_runner({"value": False})

        def turn(worker, _wait):
            self.events.append(("turn", runner.gate_foreground is worker))
            return True

        release = runner_mod.AgentWorker.release_lease

        def slow_release(worker):
            # r4 P1: while the lease is being returned (a slow base), a tick
            # of the controller must still see it held
            runner.host_gate.tick()
            done = release(worker)
            self.events.append(("released", runner.gate_foreground is worker))
            return done

        with mock.patch.object(runner_mod.AgentWorker, "process_once", turn), \
                mock.patch.object(runner_mod.AgentWorker, "release_lease", slow_release):
            runner.sweep()
        self.assertEqual(self.events, [("tick", True), ("turn", True), ("tick", True),
                                       ("released", True), ("tick", False)])
        self.assertTrue(runner.did_turn)

    def test_a_lease_the_base_did_not_take_back_stays_counted(self):
        # L159f4: release_lease failed (DbError): the lease stays held until
        # it expires, so the part never says the host free before
        from unittest import mock
        from ameesh import runner as runner_mod
        runner = self.once_runner({"value": False})
        runner.RELEASE_RETRY_S = 0.05
        calls = []

        def flaky_release(worker):
            calls.append(runner.gate_foreground is worker)
            return len(calls) >= 3  # the base answers at the third try

        with mock.patch.object(runner_mod.AgentWorker, "process_once", return_value=True), \
                mock.patch.object(runner_mod.AgentWorker, "release_lease", flaky_release):
            runner.sweep()
        self.assertEqual(calls, [True, True, True])
        ticks = [held for kind, held in self.events if kind == "tick"]
        self.assertTrue(all(ticks[:-1]), self.events)  # held while not returned
        self.assertEqual(ticks[-1], False)

    def test_a_lease_never_taken_back_is_held_until_it_expires(self):
        from unittest import mock
        from ameesh import runner as runner_mod
        runner = self.once_runner({"value": False})
        runner.RELEASE_RETRY_S = 0.05
        expiry = {}

        def turn(worker, _wait):
            # r1 P2: the expiry read in the base is not compared with this
            # host's wall clock (here 120 s ahead: the base's expiry looks
            # past); only the monotonic bound taken after the base answered
            worker.lease_deadline = time.time() - 60
            worker.lease_held_until = time.monotonic() + 0.3
            expiry["at"] = worker.lease_held_until
            return True

        with mock.patch.object(runner_mod.AgentWorker, "process_once", turn), \
                mock.patch.object(runner_mod.AgentWorker, "release_lease",
                                  return_value=False):
            runner.sweep()
            freed = time.monotonic()
        self.assertGreaterEqual(freed, expiry["at"])
        self.assertEqual(self.events[-1], ("tick", False))
        self.assertTrue(all(held for kind, held in self.events[:-1] if kind == "tick"))

    def test_a_renewal_in_flight_is_awaited_before_the_expiry_is_fixed(self):
        # r2 P2: the return failed while a renewal was in flight; it ends
        # after the turn and extends the lease: the wait follows the new bound
        import threading
        from unittest import mock
        from ameesh import runner as runner_mod
        runner = self.once_runner({"value": False})
        runner.RELEASE_RETRY_S = 0.05
        bounds = {}

        def turn(worker, _wait):
            worker.lease_held_until = time.monotonic() + 0.05

            def renewal():
                time.sleep(0.2)  # the base answers after the failed return
                worker.lease_held_until = bounds["renewed"] = time.monotonic() + 0.3
            worker.heartbeat_thread = threading.Thread(target=renewal, daemon=True)
            worker.heartbeat_thread.start()
            return True

        with mock.patch.object(runner_mod.AgentWorker, "process_once", turn), \
                mock.patch.object(runner_mod.AgentWorker, "release_lease",
                                  return_value=False):
            runner.sweep()
            freed = time.monotonic()
        self.assertGreaterEqual(freed, bounds["renewed"])
        self.assertEqual(self.events[-1], ("tick", False))
        self.assertTrue(all(held for kind, held in self.events[:-1] if kind == "tick"))

    def _detached_renewal(self, answer, release):
        """A turn whose renewal outlives `call_budget()`: `answer()` is its
        late outcome (an expiry, or an exception); `release` the return."""
        import threading
        from unittest import mock
        from ameesh import runner as runner_mod
        runner = self.once_runner({"value": False})
        runner.RELEASE_RETRY_S = 0.05
        bounds = {}
        entered, may_answer = threading.Event(), threading.Event()

        def renew(*_a):
            entered.set()
            may_answer.wait(10)
            return answer()

        def turn(worker, _wait):
            runner.lease_ttl = 0.3  # the bound a late answer gives
            bounds["start"] = time.monotonic()
            worker.lease_held_until = bounds["start"] + 0.05
            worker.lease_deadline = time.time() + 60
            with mock.patch.object(runner_mod.registry, "renew", renew), \
                    mock.patch.object(worker, "call_budget", return_value=0.01):
                self.assertEqual(worker._renew_call(), (False, None, None))
                # #5155: the detached call passes the schema check first; the
                # patch must outlast it, or the call reaches the real `renew`
                self.assertTrue(entered.wait(10))
            may_answer.set()
            return True

        guard = threading.Timer(10, runner.stop.set)  # a wait that never ends fails
        guard.start()
        self.addCleanup(guard.cancel)
        with mock.patch.object(runner_mod.AgentWorker, "process_once", turn), \
                mock.patch.object(runner_mod.AgentWorker, "release_lease", release):
            runner.sweep()
        bounds["freed"] = time.monotonic()
        return bounds

    def _late(self, outcome):
        def answer():
            time.sleep(0.2)  # committed; the answer comes late
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return answer

    def _held_until_the_late_bound(self, bounds):
        # the late answer (after 0.2 s) holds the lease 0.3 s more
        self.assertGreaterEqual(bounds["freed"], bounds["start"] + 0.5)
        self.assertEqual(self.events[-1], ("tick", False))
        self.assertTrue(all(held for kind, held in self.events[:-1] if kind == "tick"))

    def test_a_detached_renewal_is_awaited_before_the_expiry_is_fixed(self):
        # r3 P2: the renewal committed but answered after `call_budget()`:
        # the heartbeat has ended, its detached call still extends the bound
        self._held_until_the_late_bound(self._detached_renewal(
            self._late(time.time() + 60), lambda _w: False))

    def test_a_detached_renewal_answering_an_error_may_have_committed(self):
        # r4 P2: committed, then its answer is lost (`Unavailable`): the
        # bound is extended all the same
        from ameesh import db as db_mod
        self._held_until_the_late_bound(self._detached_renewal(
            self._late(db_mod.Unavailable("connection lost")), lambda _w: False))

    def test_a_stuck_renewal_does_not_stop_the_release_retries(self):
        # r4 P2: the detached renewal never answers; the base comes back and
        # takes the return at its third try: the part is freed then
        import threading
        stuck = threading.Event()
        self.addCleanup(stuck.set)
        calls = []

        def release(_worker):
            calls.append(time.monotonic())
            return len(calls) >= 3

        bounds = self._detached_renewal(lambda: stuck.wait(), release)
        self.assertEqual(len(calls), 3)
        self.assertLess(bounds["freed"] - bounds["start"], 5)
        self.assertEqual(self.events[-1], ("tick", False))

    def test_release_is_tried_even_after_renewal_failures(self):
        # r1 P2: three generic DbErrors at renewal set `lease_lost` before the
        # lease expires; the base still gives it to us, so the return is
        # tried (fenced by the epoch) and its failure reported
        import threading
        import types
        from unittest import mock
        from ameesh import db as db_mod, runner as runner_mod
        worker = types.SimpleNamespace(
            watchdog_stop=threading.Event(), lease_lost=threading.Event(),
            db=None, name="a1", epoch=7, runner=types.SimpleNamespace(runner_id="r1"),
            _depot_push=mock.Mock(),
            _release_session_lock=mock.Mock(),  # L82: released with the lease
            _close_resident=mock.Mock())   # L76: nothing outlives the lease
        worker.lease_lost.set()
        with mock.patch.object(runner_mod.registry, "release",
                               side_effect=db_mod.DbError("down")) as release:
            self.assertFalse(runner_mod.AgentWorker.release_lease(worker))
        release.assert_called_once_with(None, "a1", "r1", 7)
        worker._depot_push.assert_called_once_with(final=True)
        worker._release_session_lock.assert_called_once_with()
        worker._close_resident.assert_called_once_with("lease released")
        with mock.patch.object(runner_mod.registry, "release", return_value=False):
            self.assertTrue(runner_mod.AgentWorker.release_lease(worker))  # not ours

    def test_a_gate_closed_before_the_turn_runs_none(self):
        from unittest import mock
        from ameesh import runner as runner_mod
        holds = {"value": False}
        runner = self.once_runner(holds)
        refresh = runner.host_gate.refresh

        def closing_refresh():
            holds["value"] = True  # the guard closed the gate meanwhile
            return refresh()

        # holds_turns (the applied state) still says open: only the gate
        # read again by `refresh` stops the turn (r4 P2)
        runner.host_gate.holds_turns = lambda: False
        runner.host_gate.refresh = closing_refresh
        with mock.patch.object(runner_mod.AgentWorker, "process_once",
                               side_effect=AssertionError("no turn")):
            runner.sweep()
        self.assertFalse(runner.did_turn)
        self.assertIsNone(registry.get(self.db, "solo").get("lease_owner"))


class RelocationPlanTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM host_resources")

    def releve(self, host, mem, **gate):
        reading = {"host": host, "mem_available_bytes": mem, "swap_used_bytes": 0,
                   "load1": 0.0, "cpu_count": 8, "disk_free_bytes": 10 ** 12,
                   "disk_path": "/tmp", "turns_in_progress": 0}
        reading.update(gate)
        storage.of(self.db).hosts.record(reading)

    def test_never_towards_a_host_that_withdraws(self):
        self.releve("poste", 0)
        self.releve("vm", 8 * 1024 ** 3, gate_state="draining", gate_seq=4,
                    gate_reason="app_busy")
        self.releve("autre", 2 * 1024 ** 3)
        latest = storage.of(self.db).hosts.latest("vm")
        self.assertEqual((latest["gate_state"], latest["gate_seq"], latest["gate_reason"]),
                         ("draining", 4, "app_busy"))
        plan = relocation.plan(["poste", "vm", "autre"], "poste", db=self.db)
        self.assertEqual(plan["target"], "autre")
        self.releve("vm", 8 * 1024 ** 3, gate_state="available", gate_seq=5)
        plan = relocation.plan(["poste", "vm", "autre"], "poste", db=self.db)
        self.assertEqual(plan["target"], "vm")
        # the vm's executor stops recording: its last `available` ages out
        self.db.execute("UPDATE host_resources SET sampled_at = now() - interval '10 minutes'"
                        " WHERE host = 'vm'", ())
        plan = relocation.plan(["poste", "vm", "autre"], "poste", db=self.db)
        self.assertEqual(plan["target"], "autre")


class RunnerObeysTheGateTest(PgTestCase):
    """A real runner (subprocess) with a `file` gate in the sandbox."""

    def setUp(self):
        super().setUp()
        root = os.path.join(self.tmp, "gate")
        os.makedirs(os.path.join(root, "state"))
        os.makedirs(os.path.join(root, "ack"))
        self.state_path = os.path.join(root, "state", "state.json")
        self.ack_path = os.path.join(root, "ack", "ack.json")

    def ack(self) -> dict:
        try:
            with open(self.ack_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def launch(self, name: str, **env):
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd, prompt="consigne du tour")
        proc = self.runner_popen("--agents", name, "--poll", "1", env=self.env(
            AMEESH_HOST_GATE="file", AMEESH_HOST_GATE_FALLBACK="stopped",
            AMEESH_HOST_GATE_PATH=self.state_path, AMEESH_HOST_GATE_ACK=self.ack_path,
            **env))
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait(10)))
        return proc

    def stop(self, proc) -> str:
        proc.terminate()
        out, _ = proc.communicate(timeout=60)
        return out

    def test_closed_by_default_then_claims_when_available(self):
        proc = self.launch("ferme")  # no state yet: stopped
        time.sleep(3)
        self.assertEqual(self.turns(), [])
        self.assertEqual(self.wait_for(self.ack)["state"], "stopped")
        _write_state(self.state_path, seq=2)
        self.wait_for(lambda: len(self.turns()) == 1, timeout=30)
        self.wait_for(lambda: registry.get(self.db, "ferme")["turns"] == 1, timeout=30)
        out = self.stop(proc)
        self.assertIn("host gate file", out)

    def test_draining_stops_the_turn_at_a_safe_point_and_returns_the_lease(self):
        _write_state(self.state_path, seq=2)
        calls = [{"tool": True, "sleep": 1.0} for _ in range(20)]
        proc = self.launch("retrait", AMEESH_TEST_CLAUDE_CALLS=json.dumps(calls))
        self.wait_for(lambda: len(self.turns()) == 1, timeout=30)
        start = time.monotonic()
        _write_state(self.state_path, seq=3, state="draining", reason="app_memory",
                     drain_deadline_ts=time.time() + 60)
        ack = self.wait_for(lambda: (self.ack().get("seq") == 3 and self.ack().get("drained")
                                     and self.ack()), timeout=30)
        self.assertLess(time.monotonic() - start, 15)  # a safe point, not the deadline
        self.assertEqual((ack["held"], ack["in_turn"]), ([], []))
        row = registry.get(self.db, "retrait")
        self.assertEqual(row["pending_prompt"], "consigne du tour")  # back in the queue
        self.assertEqual(int(row["turns"]), 0)
        self.assertIn("lease_owner", row)
        self.assertIsNone(row["lease_owner"])  # lease returned
        # back to available: claimed again, without a human
        _write_state(self.state_path, seq=4)
        self.wait_for(lambda: len(self.turns()) == 2, timeout=30)
        out = self.stop(proc)
        self.assertIn("safe point", out)

    def test_deadline_stops_a_turn_without_safe_point(self):
        _write_state(self.state_path, seq=2)
        proc = self.launch("echeance", AMEESH_TEST_SLEEP="60")
        self.wait_for(lambda: len(self.turns()) == 1, timeout=30)
        _write_state(self.state_path, seq=3, state="draining",
                     drain_deadline_ts=time.time() + 2)
        ack = self.wait_for(lambda: (self.ack().get("seq") == 3 and self.ack().get("drained")
                                     and self.ack()), timeout=30)
        self.assertTrue(ack["drained"])
        self.assertEqual(registry.get(self.db, "echeance")["pending_prompt"],
                         "consigne du tour")
        out = self.stop(proc)
        self.assertIn("host drain deadline", out)


if __name__ == "__main__":
    unittest.main()
