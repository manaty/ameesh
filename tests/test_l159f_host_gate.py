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

from .support import REPO, PgTestCase

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
    def cfg(self, **kw):
        return config_mod.Config(**kw)

    def test_a_lending_host_has_a_closed_file_gate(self):
        self.assertEqual(host_gate.gate_kind(self.cfg(), lending=True), "file")
        gate = host_gate.from_config(self.cfg(), lending=True)
        self.assertEqual((gate.path, gate.ack_path, gate.fallback, gate.lending),
                         (capacity_guard.STATE_PATH, capacity_guard.ACK_PATH, "stopped", True))

    def test_a_classic_host_has_no_gate_unless_configured(self):
        self.assertIsNone(host_gate.from_config(self.cfg(), lending=False))
        gate = host_gate.from_config(self.cfg(host_gate="file"), lending=False)
        self.assertEqual((gate.fallback, gate.lending), ("available", False))
        gate = host_gate.from_config(self.cfg(host_gate="file", host_gate_fallback="stopped"))
        self.assertTrue(gate.lending)  # closed by default: freshness of a lending host

    def test_none_disables_even_on_a_lending_host(self):
        self.assertIsNone(host_gate.from_config(self.cfg(host_gate="none"), lending=True))

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
        env = {"AMEESH_CONFIG": "/nulle-part.json", "AMEESH_HOST_GATE": "File",
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

        def runner():
            gate = types.SimpleNamespace(max_concurrent=lambda: 1,
                                         gate=types.SimpleNamespace(ack_path=ack))
            return types.SimpleNamespace(host_gate=gate, lock=threading.Lock(),
                                         workers={}, _gate_slots={})

        first, second = runner(), runner()
        admitted, slot = Runner._gate_slot(first, {"name": "a"})
        self.assertTrue(admitted)
        first._gate_slots["a"] = slot
        self.assertEqual(Runner._gate_slot(second, {"name": "b"}), (False, None))
        Runner._release_slot(first, "a")  # a's worker ended
        admitted, slot = Runner._gate_slot(second, {"name": "b"})
        self.assertTrue(admitted)
        host_gate.SlotPool.release(slot)

    def test_a_lower_cap_counts_every_held_slot(self):
        pool = host_gate.SlotPool(self.dir)
        held = [pool.acquire(3) for _ in range(3)]
        host_gate.SlotPool.release(held[0])  # slot-0 free, slot-1 and slot-2 held
        self.assertIsNone(pool.acquire(2))
        self.assertIsNotNone(pool.acquire(3))


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
        self.assertIn("porte d'hôte file", out)

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
        self.assertIn("point sûr", out)

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
