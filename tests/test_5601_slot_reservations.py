# SPDX-License-Identifier: AGPL-3.0-only
"""Slot reservations by project or class; admission by class, then date (#5601)."""
import os
import threading
import types
import unittest
from unittest.mock import Mock, patch

from ameesh import canon, capacity_plan as plan, runner as runner_mod, slot_reservations as sr

from . import test_canon


class Fil:
    def __init__(self, agent=None):
        self.agent = agent or {}

    def is_alive(self):
        return True


def fake_runner(reservations, max_agents, actionable):
    """Just what the claim pass reads: workers, Host limits, next lots."""
    store = Mock()
    store.lot_sessions.actionable.side_effect = lambda name, today=None: actionable.get(name, [])
    run = types.SimpleNamespace(lock=threading.Lock(), workers={}, db=Mock(),
                                _host_max_agents=max_agents, _host_pinned_admissions=None,
                                _host_slot_reservations=reservations)
    store.work.get.return_value = {"state": "build", "priority": 2}
    for method in ("_admission_order", "_reservation_admits", "admits_new_worker",
                   "reservation_turn_allowed", "_lot_fingerprint"):
        setattr(run, method, types.MethodType(getattr(runner_mod.Runner, method), run))
    return run, store


def leave(run, name):
    """The sweep: a finished worker leaves `workers`, then `_release_slot`."""
    del run.workers[name]
    (getattr(run, "_admitting", None) or set()).discard(name)


def nxt(project, priority, created_ts):
    return [{"id": int(created_ts), "project": project, "priority": priority,
             "created_ts": created_ts}]


class PureTest(unittest.TestCase):
    def test_parse_keys_and_problems(self):
        got, problems = sr.parse({"project:nexlink": 2, "class: 1": 1, "class:x": 1,
                                  "acme": 1, "project:y": -1, "project:z": 0})
        self.assertEqual(got, {"project:nexlink": 2, "class:1": 1})
        self.assertEqual(len(problems), 3)
        self.assertEqual(sr.parse([1]), ({}, ["`policy.slot_reservations`: mapping expected"]))

    def test_witness_fifo_unchanged_without_reservation(self):
        """Guard of the lot: no reservation, the registry order is kept even
        when the next lots would order otherwise, and no lot is read."""
        candidates = [{"name": "old"}, {"name": "p1"}, {"name": "new"}]
        demands = {"old": nxt("a", 3, 30)[0], "p1": nxt("a", 1, 20)[0], "new": nxt("a", 2, 10)[0]}
        self.assertEqual(sr.order(candidates, demands, {}), candidates)
        run, store = fake_runner(None, 2, {})
        with patch("ameesh.storage.of", return_value=store):
            self.assertEqual(run._admission_order(candidates), candidates)
        store.lot_sessions.actionable.assert_not_called()
        run.workers = {"x": Fil()}
        self.assertTrue(run.admits_new_worker({"name": "y"}))
        run.workers["y"] = Fil()
        self.assertFalse(run.admits_new_worker({"name": "z"}))

    def test_admission_by_class_then_date(self):
        candidates = [{"name": "idle"}, {"name": "p2-old"}, {"name": "p2-new"},
                      {"name": "p1"}, {"name": "pilote", "releve": "election"}]
        demands = {"idle": None, "p2-old": nxt("a", 2, 10)[0],
                   "p2-new": nxt("a", 2, 20)[0], "p1": nxt("a", 1, 30)[0]}
        self.assertEqual([c["name"] for c in sr.order(candidates, demands, {"class:1": 1})],
                         ["pilote", "p1", "p2-old", "p2-new", "idle"])

    def test_decide_keeps_unfilled_reservations(self):
        res = {"class:1": 1, "project:nexlink": 1}
        self.assertEqual(sr.decide(res, {}, 0, 3, []), (True, None))
        self.assertEqual(sr.decide(res, {None: 1}, 1, 3, []), (False, None))
        self.assertEqual(sr.decide(res, {None: 1}, 1, 3, ["class:2", "project:nexlink"]),
                         (True, "project:nexlink"))
        held = {None: 1, "project:nexlink": 1}
        self.assertEqual(sr.decide(res, held, 2, 3, ["class:2", "project:nexlink"]),
                         (False, None))
        self.assertEqual(sr.decide(res, held, 2, 3, ["class:1", "project:a"]), (True, "class:1"))
        self.assertEqual(sr.decide(res, {}, 0, None, []), (True, None))


class RunnerTest(unittest.TestCase):
    def test_claim_pass_admits_by_class_and_keeps_reserved_slot(self):
        """3 places, one reserved for class 1: two P2 agents fill the shared
        places, a third P2 waits, the P1 agent gets the reserved one."""
        lots = {"a": nxt("acme", 2, 10), "b": nxt("acme", 2, 20),
                "c": nxt("acme", 2, 30), "urgent": nxt("acme", 1, 40)}
        run, store = fake_runner({"class:1": 1}, 3, lots)
        candidates = [{"name": n} for n in ("c", "b", "a", "urgent")]
        with patch("ameesh.storage.of", return_value=store):
            ordered = run._admission_order(candidates)
        self.assertEqual([c["name"] for c in ordered], ["urgent", "a", "b", "c"])
        admitted = []
        for agent in [{"name": n} for n in ("a", "b", "c", "urgent")]:
            if run.admits_new_worker(agent):
                admitted.append(agent["name"])
                run.workers[agent["name"]] = Fil(agent)
        self.assertEqual(admitted, ["a", "b", "urgent"])
        self.assertEqual(run._slot_reservations["urgent"], "class:1")
        # the reserved holder leaves: its place goes to class 1 only
        leave(run, "urgent")
        self.assertFalse(run.admits_new_worker({"name": "c"}))

    def test_resident_worker_changing_lot_is_judged_again(self):
        """B cx3: cap 2, class:1 = 1. A, admitted on a P1 lot, moves on to a
        P2 lot: B (P2) took the other place, so A must give its lease back
        and C (P1) gets the reserved place."""
        lots = {"a": nxt("acme", 1, 10), "b": nxt("acme", 2, 20), "c": nxt("acme", 1, 30)}
        run, store = fake_runner({"class:1": 1}, 2, lots)
        with patch("ameesh.storage.of", return_value=store):
            run._admission_order([{"name": "a"}, {"name": "b"}, {"name": "c"}])
        for name in ("a", "b"):
            self.assertTrue(run.admits_new_worker({"name": name}))
            run.workers[name] = Fil({"name": name})
        self.assertEqual(run._slot_reservations, {"a": "class:1", "b": None})
        # A's next lot is still P1: its turn starts, the reserved place is kept
        self.assertTrue(run.reservation_turn_allowed("a", {}, nxt("acme", 1, 11)[0]))
        # A's next lot is P2: no shared place left, the worker gives its lease back
        self.assertFalse(run.reservation_turn_allowed("a", {}, nxt("acme", 2, 12)[0]))
        leave(run, "a")
        self.assertTrue(run.admits_new_worker({"name": "c"}))
        self.assertEqual(run._slot_reservations["c"], "class:1")
        # with a shared place free, a worker leaving its reserved class keeps going
        run, _store = fake_runner({"class:1": 1}, 3, {})
        run.workers = {"a": Fil({"name": "a"}), "b": Fil({"name": "b"})}
        run._slot_reservations = {"a": "class:1", "b": None}
        self.assertTrue(run.reservation_turn_allowed("a", {}, nxt("acme", 2, 12)[0]))
        self.assertIsNone(run._slot_reservations["a"])

    def test_project_reservation_follows_the_agent_project(self):
        """Option A (mesh-design): `project:<p>` keeps places for the agents of
        project <p>, without predicting their turn. B cx2: a reviewer of acme
        with no assigned lot (review mail only) is admitted; a reviewer of
        another project is not."""
        run, store = fake_runner({"project:acme": 1}, 1, {})
        reviewer = {"name": "rev", "team": "acme"}
        other = {"name": "rev2", "team": "other"}
        with patch("ameesh.storage.of", return_value=store):
            run._admission_order([other, reviewer])
        self.assertFalse(run.admits_new_worker(other))
        self.assertTrue(run.admits_new_worker(reviewer))
        self.assertEqual(run._slot_reservations["rev"], "project:acme")
        run.workers["rev"] = Fil(reviewer)
        # its later turns keep the place whatever the class of their lot
        self.assertTrue(run.reservation_turn_allowed("rev", reviewer, nxt("x", 3, 1)[0]))
        self.assertEqual(run._slot_reservations["rev"], "project:acme")
        # declared `projects` count too
        self.assertTrue(sr.decide({"project:b": 1}, {}, 0, 1, ["project:b"])[0])

    def test_no_admit_refuse_cycle_on_unchanged_state(self):
        """Invariant of the decision: cap 1, class:1 = 1. Admitted on its P1
        lot, the agent's pick runs a P2 lot (scheduled wake): refused, lease
        released. On the same state the next admission judges that P2 lot and
        refuses: no "admitted, refused at pick, readmitted" loop."""
        lots = {"a": nxt("acme", 1, 10)}
        run, store = fake_runner({"class:1": 1}, 1, lots)
        agent = {"name": "a"}
        with patch("ameesh.storage.of", return_value=store):
            run._admission_order([agent])
            self.assertTrue(run.admits_new_worker(agent))
            run.workers["a"] = Fil(agent)
            self.assertFalse(run.reservation_turn_allowed("a", agent, nxt("acme", 2, 20)[0]))
            leave(run, "a")
            for _ in range(3):     # same mail, lots and wakes: refused every pass
                run._admission_order([agent])
                self.assertFalse(run.admits_new_worker(agent))
            # a turn that passes the pick clears the remembered refusal
            self.assertTrue(run.reservation_turn_allowed("a", agent, nxt("acme", 1, 10)[0]))
            run._admission_order([agent])
            self.assertTrue(run.admits_new_worker(agent))
            # an unchanged lot keeps refusing, however long the wait; a
            # change of the refused lot (moved, answered, reassigned) lifts it
            run.reservation_turn_allowed("a", agent, nxt("acme", 2, 20)[0])
            run._admission_order([agent])
            self.assertFalse(run.admits_new_worker(agent))
            store.work.get.return_value = {"state": "delivered", "priority": 2}
            run._admission_order([agent])
            self.assertTrue(run.admits_new_worker(agent))
            self.assertNotIn("a", run._refused_demands)


class ConcurrencyTest(unittest.TestCase):
    def test_turn_changes_are_atomic(self):
        """B1 cx3: cap 3, class:1 = 1, A and B on shared places switch to P1
        at the same time. The second check waits while the first is inside
        its decision, then sees class:1 taken: one holder only."""
        run, _store = fake_runner({"class:1": 1}, 3, {})
        run.workers = {n: Fil({"name": n}) for n in ("a", "b")}
        run._slot_reservations = {"a": None, "b": None}
        inside, go = threading.Event(), threading.Event()
        real = sr.decide

        def slow(*args):
            if threading.current_thread().name == "first":
                inside.set()
                go.wait(5)
            return real(*args)

        results = {}

        def turn(name):
            results[name] = run.reservation_turn_allowed(name, {"name": name},
                                                         nxt("acme", 1, 1)[0])

        with patch.object(sr, "decide", side_effect=slow):
            first = threading.Thread(target=turn, args=("a",), name="first")
            first.start()
            self.assertTrue(inside.wait(5))
            second = threading.Thread(target=turn, args=("b",), name="second")
            second.start()
            second.join(0.3)
            self.assertTrue(second.is_alive(), "second check waits for the first")
            go.set()
            first.join(5)
            second.join(5)
        self.assertEqual(results, {"a": True, "b": True})
        self.assertEqual(sorted(map(str, run._slot_reservations.values())),
                         ["None", "class:1"])

    def test_admitted_not_yet_registered_counts(self):
        """An agent admitted by the claim pass but not yet a live worker holds
        its place against a concurrent turn change, until `_release_slot`."""
        run, store = fake_runner({"class:1": 1}, 2, {"n": nxt("acme", 1, 1)})
        run._release_slot = types.MethodType(runner_mod.Runner._release_slot, run)
        run._gate_slots_lock, run._gate_slots = threading.Lock(), {}
        run.workers = {"a": Fil({"name": "a"})}
        run._slot_reservations = {"a": None}
        with patch("ameesh.storage.of", return_value=store):
            run._admission_order([{"name": "n"}])
        self.assertTrue(run.admits_new_worker({"name": "n"}))   # class:1, not registered
        self.assertTrue(run.reservation_turn_allowed("a", {}, nxt("acme", 2, 2)[0]))
        self.assertFalse(run.admits_new_worker({"name": "z"}), "n's place is counted")
        run._release_slot("n")
        self.assertNotIn("n", run._admitting)


class FingerprintTest(unittest.TestCase):
    def test_wake_or_update_alone_lifts_the_refusal(self):
        """B2 cx3: the fingerprint reads the contract keys of `work.get`
        (`wake_ts`, `updated_ts`): either change alone lifts the refusal."""
        for key in ("wake_ts", "updated_ts"):
            run, store = fake_runner({"class:1": 1}, 1, {"a": nxt("acme", 1, 1)})
            store.work.get.return_value = {"state": "build", "priority": 2,
                                           "wake_ts": 100.0, "updated_ts": 200.0}
            agent = {"name": "a"}
            with patch("ameesh.storage.of", return_value=store):
                self.assertFalse(run.reservation_turn_allowed("a", agent, nxt("acme", 2, 2)[0]))
                run._admission_order([agent])
                self.assertFalse(run.admits_new_worker(agent))
                store.work.get.return_value = dict(store.work.get.return_value,
                                                   **{key: 999.0})
                run._admission_order([agent])
                self.assertTrue(run.admits_new_worker(agent), key)


class CanonTest(test_canon._TmpMixin, unittest.TestCase):
    def fiche(self, line):
        root = self.example_copy()
        path = os.path.join(root, "hotes", "atelier.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace("  max_agents: 4\n", "  max_agents: 4\n  %s\n" % line, 1))
        return canon.load(root, untrusted=True)

    def test_host_policy_reads_reservations(self):
        loaded = self.fiche('slot_reservations: {"project:acme-web": 1, "class:1": 1}')
        self.assertEqual(loaded.host("atelier").policy.slot_reservations,
                         {"project:acme-web": 1, "class:1": 1})
        self.assertNotIn("host-policy-invalid", [f.code for f in canon.validate(loaded)])

    def test_overbooked_or_unreadable_reservation_is_an_error(self):
        for line in ('slot_reservations: {"class:1": 5}', 'slot_reservations: {"acme": 1}'):
            loaded = self.fiche(line)
            self.assertIn("host-policy-invalid", [f.code for f in canon.validate(loaded)])


class PlannerTest(unittest.TestCase):
    def lot(self, i, **kw):
        return dict(dict(id=i, estimate_minutes=60, created_ts=i, project="acme",
                         repository="acme/repo", footprint_paths=["src/m%s" % i]), **kw)

    def test_engine_reads_live_keys(self):
        data = dict(now=1800000000, lots=[self.lot(1), self.lot(2, priority_class=1)],
                    hosts=[{"name": "w", "slots": 2, "reservations": {"class:1": 1}}])
        rows = {r["id"]: r for r in plan.forecast(data)["lots"]}
        self.assertEqual(rows[2]["reservation"], "class:1")
        self.assertIsNone(rows[1]["reservation"])
        data["hosts"][0]["reservations"] = {"project:acme/repo": 2}
        rows = {r["id"]: r for r in plan.forecast(data)["lots"]}
        self.assertEqual({r["reservation"] for r in rows.values()}, {"project:acme/repo"})

    def test_project_snapshot_takes_host_record_reservations(self):
        fiche = types.SimpleNamespace(policy=types.SimpleNamespace(
            slot_reservations={"class:1": 1}))
        canons = [types.SimpleNamespace(host=lambda name: fiche if name == "w" else None)]
        settings = dict(now=1800000000, hosts=[{"name": "w", "slots": 2},
                                               {"name": "x", "slots": 1, "reservations": {}}])
        with patch("ameesh.storage.of") as store, \
                patch("ameesh.work_dependencies.links", return_value={}), \
                patch("ameesh.estimates.report", return_value={"lots": []}):
            store.return_value.work.project_plan_rows.return_value = []
            out = plan.project_snapshot(Mock(), "acme", settings, canons=canons)
        self.assertEqual(out["hosts"][0]["reservations"], {"class:1": 1})
        self.assertEqual(out["hosts"][1]["reservations"], {})


if __name__ == "__main__":
    unittest.main()
