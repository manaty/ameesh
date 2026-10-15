# SPDX-License-Identifier: AGPL-3.0-only
"""Train safety: durable intents, fresh CI after isolation, exact publication."""
from __future__ import annotations

import copy
import dataclasses
import contextlib
import io
import os
import types
import unittest
from unittest import mock

from ameesh import land, merge_queue, radar, resolutions, train, train_ci, train_git

from .test_l160e2_land import _Land
from .test_l160b_radar import _git


def row(lot, **kw):
    return dict(work_item_id=lot, generation=lot, frozen_id=lot,
                frozen_sha=("%040x" % lot), target="main", source="github.com/o/r",
                branch="lot/%d" % lot, queued_ts=0, **kw)


class MemoryPorts:
    """External effects and CAS persistence are independent of engine instances."""
    def __init__(self, rows):
        self.rows, self.document = rows, None
        self.base = "a" * 40
        self.epoch = 0
        self.held = False
        self.effects, self.probes, self.notices = [], [], []
        self.ci_by_head = {}
        self.fired, self.admitted = True, True
        self.edges = []

    def acquire(self, epoch):
        if epoch is not None:
            return epoch if self.held and self.epoch == epoch else None
        if self.held:
            return None
        self.epoch += 1
        self.held = True
        return self.epoch

    def release(self, epoch):
        if self.epoch == epoch:
            self.held = False

    def load(self):
        return copy.deepcopy(self.document)

    def save(self, state, expected, epoch):
        if not self.held or epoch != self.epoch:
            raise train.TrainError("stale target lease")
        if expected != (self.document or {}).get("revision", 0):
            raise train.TrainError("stale train revision")
        self.document = copy.deepcopy(dict(state, revision=expected + 1))
        return self.load()

    def entries(self):
        return copy.deepcopy(self.rows)

    def dependencies(self, rows):
        return copy.deepcopy(self.edges)

    def target(self):
        return self.base

    def qualify(self, rows, base, mode):
        if not self.admitted:
            raise train.TrainError("unknown historical author or missing review")
        return dict(base=base, mode=mode, members=[train.identity(r) for r in rows])

    def compose(self, base, rows, nonce):
        self.probes.append([r["work_item_id"] for r in rows])
        token = train.digest([base, [train.identity(r) for r in rows], nonce])[:40]
        return dict(head=token, tree=train.digest([token, "tree"])[:40])

    def publish(self, plan, epoch):
        self.effects.append(("publish", plan["head"]))

    def start_ci(self, plan):
        self.effects.append(("ci", plan["head"]))

    def ci(self, plan):
        return dict(head=plan["head"], tree=plan["tree"], complete=True,
                    status=self.ci_by_head.get(plan["head"], "pending"))

    def authorize(self, plan):
        return dict(id=1, head=plan["head"]) if self.fired else None

    def promote(self, plan, fire, epoch):
        self.effects.append(("promote", plan["head"]))
        self.base = plan["head"]

    def reconcile(self, plan, fire, epoch):
        if self.base != plan["head"]:
            raise train.TrainError("remote target does not prove promotion")
        self.effects.append(("reconcile", plan["head"]))
        return dict(id=fire["id"], state="landed", plan=copy.deepcopy(plan))

    def quarantine(self, rows, reason, epoch):
        ids = {r["work_item_id"] for r in rows}
        self.rows = [r for r in self.rows if r["work_item_id"] not in ids]
        self.effects.append(("quarantine", sorted(ids)))

    def notify(self, reason, state):
        self.notices.append(reason)


class TrainTest(unittest.TestCase):
    def make(self, rows=None, **kw):
        self.ports = MemoryPorts(rows or [row(1)])
        self.engine = train.Engine(self.ports, source="github.com/o/r", target="main",
                                   policy=train.Policy(enabled=True, **kw))
        return self.engine

    def test_disabled_has_no_storage_or_external_effect(self):
        ports = mock.Mock()
        engine = train.Engine(ports, source="x", target="main", policy=train.Policy())
        self.assertEqual(engine.step(3600), {"result": "off"})
        self.assertEqual(ports.mock_calls, [])

    def test_hour_or_five_ready_lots(self):
        engine = self.make()
        self.assertEqual(engine.step(3599)["result"], "idle")
        self.assertEqual(engine.step(3600)["result"], "ci")
        engine = self.make([row(i) for i in range(1, 6)])
        self.assertEqual(engine.step(1)["result"], "ci")

    def test_dependencies_before_priority_and_missing_blocker_waits(self):
        edges = [dict(work_item_id=1, blocker_id=2, blocking=True),
                 dict(work_item_id=3, blocker_id=99, blocking=True)]
        rows = [row(1, priority=1), row(2, priority=2), row(3, priority=1)]
        self.assertEqual([r["work_item_id"] for r in train.ordered(rows, edges)], [2, 1])
        edges.append(dict(work_item_id=2, blocker_id=1, blocking=True))
        self.assertEqual(train.ordered(rows, edges), [])

    def test_red_or_pending_never_promotes(self):
        engine = self.make()
        engine.step(3600)
        self.assertEqual(engine.step(3601)["result"], "ci")
        self.assertFalse(any(e[0] == "promote" for e in self.ports.effects))

    def test_one_ci_and_green_promotes_exact_head(self):
        engine = self.make()
        first = engine.step(3600)
        engine.step(3601)
        self.ports.ci_by_head[first["plan"]["head"]] = "green"
        self.assertEqual(engine.step(3602)["result"], "landed")
        self.assertEqual([e[0] for e in self.ports.effects], ["publish", "ci", "promote"])
        self.assertEqual(self.ports.base, first["plan"]["head"])
        self.assertFalse(self.ports.held)

    def test_missing_fire_and_withdrawal_prevent_promotion(self):
        engine = self.make()
        plan = engine.step(3600)["plan"]
        self.ports.ci_by_head[plan["head"]] = "green"
        self.ports.fired = False
        self.assertEqual(engine.step(3601)["result"], "awaiting-fire")
        self.ports.rows = []  # pilot removed a member from the queue
        self.assertEqual(engine.step(3602)["result"], "refused")
        self.assertNotIn("promote", [e[0] for e in self.ports.effects])

    def test_new_ci_attempt_after_waiting_for_fire_supersedes_green(self):
        engine = self.make()
        plan = engine.step(3600)["plan"]
        self.ports.ci_by_head[plan["head"]] = "green"
        self.ports.fired = False
        self.assertEqual(engine.step(3601)["result"], "awaiting-fire")
        self.ports.fired = True
        self.ports.ci_by_head[plan["head"]] = "pending"
        self.assertEqual(engine.step(3602)["result"], "ci")
        self.assertNotIn("promote", [e[0] for e in self.ports.effects])
        self.ports.ci_by_head[plan["head"]] = "green"
        self.assertEqual(engine.step(3603)["result"], "landed")

    def test_red_after_waiting_for_fire_isolates_instead_of_promoting(self):
        engine = self.make()
        plan = engine.step(3600)["plan"]
        self.ports.ci_by_head[plan["head"]] = "green"
        self.ports.fired = False
        engine.step(3601)
        self.ports.fired = True
        self.ports.ci_by_head[plan["head"]] = "red"
        self.assertEqual(engine.step(3602)["result"], "empty")
        self.assertNotIn("promote", [e[0] for e in self.ports.effects])

    def test_ready_rechecks_exact_complete_ci_after_restart(self):
        for change in ("head", "complete"):
            with self.subTest(change=change):
                engine = self.make()
                plan = engine.step(3600)["plan"]
                self.ports.ci_by_head[plan["head"]] = "green"
                self.ports.fired = False
                engine.step(3601)
                old = self.ports.ci(plan)
                old[change] = "f" * 40 if change == "head" else False
                self.ports.ci = lambda p: old
                self.ports.fired = True
                self.assertEqual(engine.step(3602)["result"], "refused")
                self.assertNotIn("promote", [e[0] for e in self.ports.effects])

    def test_requeue_same_sha_invalidates_old_admission(self):
        engine = self.make()
        engine.step(3600)
        self.ports.rows[0]["generation"] += 1
        self.assertEqual(engine.step(3601)["result"], "refused")

    def test_target_move_unknown_review_wrong_ci_head_refuse(self):
        for change in ("target", "review", "ci"):
            with self.subTest(change=change):
                engine = self.make()
                plan = engine.step(3600)["plan"]
                if change == "target":
                    self.ports.base = "b" * 40
                elif change == "review":
                    self.ports.admitted = False
                else:
                    self.ports.ci = lambda p: dict(head="b" * 40, tree=p["tree"],
                                                   status="green", complete=True)
                self.assertEqual(engine.step(3601)["result"], "refused")
                self.assertNotIn("promote", [e[0] for e in self.ports.effects])

    def test_dispatch_intent_restart_does_not_duplicate_ci(self):
        engine = self.make()
        def lost(plan):
            raise OSError("dispatch response lost")
        self.ports.start_ci = lost
        with self.assertRaises(OSError):
            engine.step(3600)
        self.assertEqual(self.ports.document["phase"], "ci-intent")
        self.ports.held = False  # process died and its lease expired
        resumed = train.Engine(self.ports, source=engine.source, target=engine.target,
                                policy=engine.policy)
        self.assertEqual(resumed.step(3601)["result"], "ci")
        self.assertEqual([e[0] for e in self.ports.effects], ["publish"])

    def test_lost_merge_response_reconciles_without_second_merge(self):
        engine = self.make()
        plan = engine.step(3600)["plan"]
        self.ports.ci_by_head[plan["head"]] = "green"
        original = self.ports.promote
        def lost(plan, fire, epoch):
            original(plan, fire, epoch)
            raise OSError("push response lost")
        self.ports.promote = lost
        with self.assertRaises(OSError):
            engine.step(3601)
        self.assertEqual(self.ports.document["phase"], "promoting")
        self.assertEqual(self.ports.document["fire"], dict(id=1, head=plan["head"]))
        self.ports.held = False  # holder died; a new process resumes the intent
        resumed = train.Engine(self.ports, source=engine.source, target=engine.target,
                               policy=engine.policy)
        self.assertEqual(resumed.step(3602)["result"], "landed")
        self.assertEqual([e[0] for e in self.ports.effects].count("promote"), 1)
        self.assertEqual([e[0] for e in self.ports.effects].count("reconcile"), 1)

    def test_reconciliation_needs_exact_landed_receipt_before_changing_phase(self):
        engine = self.make()
        plan = engine.step(3600)["plan"]
        self.ports.ci_by_head[plan["head"]] = "green"
        original = self.ports.promote
        def lost(plan, fire, epoch):
            original(plan, fire, epoch)
            raise OSError("response lost")
        self.ports.promote = lost
        with self.assertRaises(OSError):
            engine.step(3601)
        self.ports.reconcile = lambda *args: None
        self.assertEqual(engine.step(3602)["result"], "refused")
        self.assertEqual(self.ports.document["phase"], "promoting")
        self.assertEqual([e[0] for e in self.ports.effects].count("promote"), 1)

    def test_bisect_then_retest_remaining_union(self):
        engine = self.make([row(1), row(2)], threshold=2)
        plan = engine.step(0)["plan"]
        self.ports.ci_by_head[plan["head"]] = "red"
        self.assertEqual(engine.step(1)["result"], "recomposed")
        for at, outcome in ((2, "red"), (4, "green")):
            plan = engine.step(at)["plan"]
            self.ports.ci_by_head[plan["head"]] = outcome
            result = engine.step(at + 1)
            self.assertEqual(result["result"], "recomposed")
        final = engine.step(6)["plan"]
        self.assertFalse(final["diagnostic"])
        # Even a final composition with a previously tested tree has a new
        # commit identity, so the preceding probe's CI cannot certify it.
        self.assertEqual([e[0] for e in self.ports.effects].count("ci"), 4)
        self.assertEqual([r["work_item_id"] for r in final["members"]], [2])
        self.assertEqual(engine.step(7)["result"], "ci")
        self.ports.ci_by_head[final["head"]] = "green"
        self.assertEqual(engine.step(8)["result"], "landed")

    def test_green_halves_red_union_pauses_without_loop_or_merge(self):
        engine = self.make([row(1), row(2)], threshold=2)
        plan = engine.step(0)["plan"]
        self.ports.ci_by_head[plan["head"]] = "red"
        engine.step(1)
        for at in (2, 4):
            plan = engine.step(at)["plan"]
            self.ports.ci_by_head[plan["head"]] = "green"
            result = engine.step(at + 1)
        self.assertEqual(result["result"], "paused")
        self.assertEqual(engine.step(36000)["result"], "paused")
        self.assertNotIn("promote", [e[0] for e in self.ports.effects])

    def test_bisection_includes_proven_prerequisite_context(self):
        engine = self.make([row(1), row(2)], threshold=2)
        self.ports.edges = [dict(work_item_id=2, blocker_id=1, blocking=True)]
        root = engine.step(0)["plan"]
        self.ports.ci_by_head[root["head"]] = "red"
        engine.step(1)
        first = engine.step(2)["plan"]
        self.ports.ci_by_head[first["head"]] = "green"
        engine.step(3)
        second = engine.step(4)["plan"]
        self.assertEqual([r["work_item_id"] for r in second["suspects"]], [2])
        self.assertEqual([r["work_item_id"] for r in second["members"]], [1, 2])
        self.ports.ci_by_head[second["head"]] = "red"
        result = engine.step(5)
        self.assertEqual([r["work_item_id"] for r in result["plan"]["members"]], [1])
        self.assertIn(("quarantine", [2]), self.ports.effects)

    def test_faulty_prerequisite_defers_dependent_without_false_blame(self):
        engine = self.make([row(1), row(2)], threshold=2)
        self.ports.edges = [dict(work_item_id=2, blocker_id=1, blocking=True)]
        root = engine.step(0)["plan"]
        self.ports.ci_by_head[root["head"]] = "red"
        engine.step(1)
        first = engine.step(2)["plan"]
        self.ports.ci_by_head[first["head"]] = "red"
        self.assertEqual(engine.step(3)["result"], "empty")
        self.assertEqual([r["work_item_id"] for r in self.ports.rows], [2])
        self.assertEqual([e for e in self.ports.effects if e[0] == "quarantine"],
                         [("quarantine", [1])])

    def test_second_diagnostic_never_reintroduces_quarantined_members(self):
        engine = self.make([row(1), row(2), row(3)], threshold=3)
        plan = engine.step(0)["plan"]
        self.ports.ci_by_head[plan["head"]] = "red"
        engine.step(1)
        first = engine.step(2)["plan"]
        self.ports.ci_by_head[first["head"]] = "red"  # exclude 1
        engine.step(3)
        rest = engine.step(4)["plan"]
        self.ports.ci_by_head[rest["head"]] = "green"  # diagnostic 2,3
        engine.step(5)
        union = engine.step(6)["plan"]
        # A later complete run can fail even when the probe was green.
        self.ports.ci_by_head[union["head"]] = "red"
        # If a new dependency points at the excluded lot, 2 must wait;
        # including 1 as prerequisite context would resurrect a quarantine.
        self.ports.edges = [dict(work_item_id=2, blocker_id=1, blocking=True)]
        last = engine.step(7)["plan"]
        self.assertEqual([r["work_item_id"] for r in last["members"]], [3])
        self.assertNotIn(1, self.ports.probes[-1])


class TrainGitTest(_Land):
    def setUp(self):
        super().setUp()
        path = ".github/workflows/tests.yml"
        os.makedirs(os.path.join(self.repo, ".github/workflows"))
        with open(os.path.join(self.repo, path), "w") as fh:
            fh.write("audited test workflow\n")
        script = "planner.py"
        with open(os.path.join(self.repo, script), "w") as fh:
            fh.write("audited executable planner\n")
        os.chmod(os.path.join(self.repo, script), 0o755)
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "add", path, script)
        _git(self.repo, "commit", "-qm", "test CI contract")
        _git(self.repo, "push", "-q", "origin", "main")
        blob = _git(self.repo, "rev-parse", "main:" + path)
        self.workflows = (train_ci.Workflow(path, ("suite",), sources=(
            (path, blob), (script, _git(self.repo, "rev-parse", "main:" + script)))),)
        self.clone = resolutions.Replayer(self.repo, os.path.join(self.tmp, "train.clone"))
        self.repository = train_git.Repository(self.project, self.clone)

    def plan(self):
        members = [self.queued(1), self.queued(4)]
        base = self.repository.target("main")
        merged = self.repository.compose("main", base, members)
        return dict(source=self.repository.source, target="main", base=base, members=members,
                    branch="integration/train-test-1", **merged)

    def test_deterministic_composition_publish_then_atomic_exact_promotion(self):
        plan = self.plan()
        self.assertEqual(self.repository.compose("main", plan["base"], plan["members"]),
                         {k: plan[k] for k in ("head", "tree")})
        self.repository.publish(plan, key="p:main", epoch=1, owner="one", workflows=self.workflows)
        self.assertEqual(_git(self.origin, "rev-parse", "main"), plan["base"])
        self.assertEqual(_git(self.origin, "rev-parse", plan["branch"]), plan["head"])
        self.repository.promote(plan, key="p:main", epoch=1, owner="one")
        self.assertEqual(_git(self.origin, "rev-parse", "main"), plan["head"])
        self.assertEqual(_git(self.origin, "rev-parse", "main^{tree}"), plan["tree"])

    def test_moved_train_ref_and_older_epoch_never_promote(self):
        plan = self.plan()
        self.repository.publish(plan, key="p:main", epoch=2, owner="two", workflows=self.workflows)
        with self.assertRaises(land._LeaseRefused):
            self.repository.promote(plan, key="p:main", epoch=1, owner="one")
        _git(self.origin, "update-ref", "refs/heads/" + plan["branch"], plan["base"])
        with self.assertRaisesRegex(train.TrainError, "ref moved"):
            self.repository.promote(plan, key="p:main", epoch=2, owner="two")
        self.assertEqual(_git(self.origin, "rev-parse", "main"), plan["base"])

    def test_stale_or_missing_ci_contract_refuses_before_any_remote_write(self):
        plan = self.plan()
        stale = dataclasses.replace(self.workflows[0], sources=((self.workflows[0].path, "f" * 40),))
        for contract in ((), (stale,)):
            with self.subTest(contract=contract), self.assertRaises(train.TrainError):
                self.repository.publish(plan, key="p:main", epoch=1, owner="one",
                                        workflows=contract)
            self.assertEqual(_git(self.origin, "for-each-ref", "--format=%(refname)",
                                  "refs/heads/integration/", self.project.lease_ref), "")

    def test_symlink_with_audited_blob_is_not_an_executable_ci_source(self):
        plan = self.plan()
        path, blob = self.workflows[0].sources[0]
        _git(self.clone.path, "read-tree", plan["head"])
        _git(self.clone.path, "update-index", "--cacheinfo", "120000", blob, path)
        tree = _git(self.clone.path, "write-tree")
        head = _git(self.clone.path, "commit-tree", tree, "-p", plan["head"], "-m", "symlink")
        with self.assertRaisesRegex(train.TrainError, "full-suite source differs"):
            self.repository.publish(dict(plan, head=head, tree=tree), key="p:main", epoch=1,
                                    owner="one", workflows=self.workflows)
        self.assertEqual(_git(self.origin, "for-each-ref", "--format=%(refname)",
                              "refs/heads/integration/", self.project.lease_ref), "")

    def test_protected_target_refuses_before_publication(self):
        project = dataclasses.replace(self.project, protected=("main",))
        repository = train_git.Repository(project, self.clone)
        with self.assertRaisesRegex(train.TrainError, "protected"):
            repository.target("main")

    def test_missing_radar_profile_is_diagnosed_without_implicit_clone(self):
        args = types.SimpleNamespace(queue_command="add", repo=self.repo, id=1, actor="runner")
        err = io.StringIO()
        with mock.patch.object(merge_queue, "radar_project", return_value=None), \
                mock.patch.object(merge_queue, "enter") as enter, contextlib.redirect_stderr(err):
            self.assertEqual(merge_queue.run(self.cfg, None, args), 1)
        enter.assert_not_called()
        self.assertIn("radar.projects", err.getvalue())
        self.assertIn("local checkout", err.getvalue())
