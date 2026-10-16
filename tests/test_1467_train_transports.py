# SPDX-License-Identifier: AGPL-3.0-only
"""Real Git handoffs and durable selection; qualification is an explicit fixture."""
import copy
import dataclasses
import json
import os
import types
import unittest
from unittest import mock

from ameesh import (plan_git, resolutions, storage, train, train_git, train_incidents,
                    train_transports, train_transport_journal)
from ameesh.canon import GitError
from .test_1467_train import MemoryPorts, row
from .test_l160b_radar import _git, _write
from .test_l160e2_land import _Land


class NormalizedQualificationTest(unittest.TestCase):
    def setUp(self):
        self.facts = dict(source="github.com/o/r", target="main", lot=2, source_head="a" * 40,
                          base="b" * 40, head="c" * 40, tree="d" * 40, delta="e" * 64,
                          ref="refs/ameesh/resolutions/2/fixture")
        self.reference = dict(kind="work-event", id=7)
        self.qualification = dict(kind="mechanical", references=[self.reference])
        self.originals = [dict(reference=self.reference, author="pilot", record=dict(
            id=7, work_item_id=2, actor="pilot", note="original qualification", created_ts=1))]
        self.resolver = train_transport_journal.Normalized()

    def event(self, number=8, *, state="maintained", actor="pilot", **change):
        value = dict(schema=train_transport_journal.QUALIFICATION_SCHEMA, work_item_id=2,
            frozen_sha=self.facts["source_head"], base=self.facts["base"], head=self.facts["head"],
            kind="mechanical", state=state, source_reference=self.reference)
        value.update(change)
        return dict(id=number, work_item_id=2, actor=actor, created_ts=2, note=json.dumps(value))

    def resolve(self, events, *, facts=None):
        return self.resolver(None, facts=facts or self.facts, qualification=self.qualification,
                             originals=self.originals, history=dict(events=events, milestones=[]))

    def test_exact_original_author_normalization_preserves_existing_references(self):
        answer = self.resolve([self.event()])
        self.assertEqual(answer, dict(facts=self.facts, qualification=self.qualification, active=True))

    def test_latest_state_uses_database_time_and_id_even_when_input_is_unordered(self):
        self.assertIsNone(self.resolve([self.event(9, state="withdrawn"), self.event(8)]))
        self.assertTrue(self.resolve([self.event(10), self.event(9, state="withdrawn"),
                                      self.event(8)])["active"])

    def test_payload_cannot_supply_author_or_lot(self):
        for change in (dict(actor="another"), dict(work_item_id=99), dict(author="pilot")):
            with self.subTest(change=change), self.assertRaises(train.TrainError):
                self.resolve([self.event(**change)])
        event = self.event()
        event["work_item_id"] = 99
        with self.assertRaisesRegex(train.TrainError, "another lot"):
            self.resolve([event])

    def test_closed_bounds_do_not_rebind_a_historical_freeze_or_another_base(self):
        for change in (dict(source_head="f" * 40), dict(base="f" * 40), dict(head="f" * 40)):
            with self.subTest(change=change):
                self.assertIsNone(self.resolve([self.event()], facts=dict(self.facts, **change)))
        self.assertTrue(self.resolve([self.event(9, state="withdrawn", base="f" * 40),
                                      self.event(8)])["active"])

    def test_prose_and_unknown_json_do_not_qualify(self):
        event = self.event()
        for text in ("mechanical GO " + self.facts["head"], '{"unrelated":true}',
                     "The format is " + train_transport_journal.QUALIFICATION_SCHEMA):
            event["note"] = text
            self.assertIsNone(self.resolve([event]))
            self.assertTrue(self.resolve([self.event(), event])["active"])
        for change in (dict(schema="unknown/1"), dict(frozen_sha="a" * 7), dict(state="ok")):
            with self.subTest(change=change), self.assertRaises(train.TrainError):
                self.resolve([self.event(**change)])

    def test_duplicate_json_keys_are_ambiguous_even_after_an_earlier_maintained_state(self):
        event = self.event(9)
        event["note"] = event["note"][:-1] + ',"state":"withdrawn"}'
        with self.assertRaisesRegex(train.TrainError, "unreadable"):
            self.resolve([self.event(), event])

    def test_reference_requires_an_exact_integer_id_and_closed_shape(self):
        self.reference["id"] = 1
        self.originals[0]["record"]["id"] = 1
        for reference in (dict(kind="work-event", id=True), dict(self.reference, extra="unknown")):
            event = self.event(source_reference=reference)
            with self.subTest(reference=reference), self.assertRaises(train.TrainError):
                self.resolve([self.event(), event])


class TransportGitTest(_Land):
    def setUp(self):
        super().setUp()
        self.member = self.queued(2)
        _git(self.repo, "push", "-q", "origin", "lot/1-a:main")
        self.clone = resolutions.Replayer(self.repo, os.path.join(self.tmp, "transport.clone"))
        self.repository = train_git.Repository(self.project, self.clone)
        self.base = self.repository.target("main")
        self.receipt = self.handoff(self.base)
        self.reader = mock.Mock(side_effect=lambda **kw: copy.deepcopy(self.receipt))

    def handoff(self, base, suffix="qualified-fixture"):
        _git(self.clone.path, "checkout", "-q", "--detach", base)
        conflict = resolutions._run(self.clone.path, "merge", "--no-ff", "--no-commit",
                                     self.member["frozen_sha"])
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("shared.py", resolutions.unmerged(self.clone.path))
        _write(self.clone.path, "shared.py", "resolved\ndeux\ntrois\nquatre\ncinq\nsix\nsept\n")
        _git(self.clone.path, "add", "shared.py")
        _git(self.clone.path, "commit", "-qm", "qualified composer handoff fixture")
        head = _git(self.clone.path, "rev-parse", "HEAD")
        receipt = dict(schema=train_transports.SCHEMA, source=self.repository.source,
            target="main", admission=list(train.identity(self.member)), base=base,
            head=head, tree=_git(self.clone.path, "rev-parse", "HEAD^{tree}"),
            delta=train_transports.delta(self.clone.path, base, head),
            ref="refs/ameesh/resolutions/2/" + suffix,
            qualification=dict(kind="adapted", references=[dict(kind="work-event", id=1)]))
        _git(self.clone.path, "push", "-q", self.origin, head + ":" + receipt["ref"])
        return receipt

    def compose(self):
        self.repository.transports = self.reader
        return self.repository.compose("main", self.base, [self.member], "same-intent")

    def plan(self):
        return dict(source=self.repository.source, target="main", base=self.base,
                    members=[self.member], branch="integration/train-transport-fixture",
                    nonce="same-intent", **self.compose())

    def test_raw_conflict_accepts_qualified_transport_without_replacing_source_admission(self):
        with self.assertRaises(train.MemberDeferred):
            self.repository.compose("main", self.base, [self.member])
        before = copy.deepcopy(self.member)
        result = self.compose()
        self.assertEqual(self.member, before)
        self.assertEqual(result["transports"], [self.receipt])
        self.assertEqual(result["tree"], self.receipt["tree"])
        parents = _git(self.clone.path, "rev-list", "--parents", "-n", "1", result["head"]).split()[1:]
        self.assertIn(self.member["frozen_sha"], parents)
        self.assertIn(self.receipt["head"], parents)
        self.assertEqual(_git(self.clone.path, "show", result["head"] + ":shared.py").splitlines()[0],
                         "resolved")
        self.assertEqual(self.compose(), result)

    def test_wrong_lot_generation_freeze_source_target_or_base_is_named_before_composition(self):
        original = copy.deepcopy(self.receipt)
        changes = [dict(source="another/repository"), dict(target="develop"), dict(base="f" * 40)]
        for index in (0, 1, 3, 4):
            admission = list(original["admission"])
            admission[index] = (admission[index] + 1 if isinstance(admission[index], int)
                                else "f" * 40)
            changes.append(dict(admission=admission))
        for change in changes:
            self.receipt = dict(original, **change)
            with self.subTest(change=change), self.assertRaisesRegex(
                    train.MemberDeferred, "transport #2 base=.*another"):
                self.compose()
        self.assertEqual(_git(self.origin, "rev-parse", "main"), self.base)

    def test_delta_tree_ancestry_and_qualification_are_checked_on_real_objects(self):
        original = copy.deepcopy(self.receipt)
        for change in (dict(delta="0" * 64), dict(tree=self.base),
                       dict(head=self.member["frozen_sha"]),
                       dict(ref=original["ref"] + " "),
                       dict(qualification=dict(kind="mechanical", references=[])),
                       dict(qualification=dict(kind="mechanical", references=["a GO note"]))):
            self.receipt = dict(original, **change)
            with self.subTest(change=change), self.assertRaisesRegex(train.MemberDeferred, "transport #2"):
                self.compose()

    def test_a_ref_alone_never_supplies_qualification(self):
        self.receipt = {key: self.receipt[key] for key in ("head", "ref")}
        with self.assertRaisesRegex(train.MemberDeferred, "structured qualified"):
            self.compose()

    def test_unreadable_transport_objects_are_a_named_composition_exclusion(self):
        with mock.patch.object(train_transports, "verify", side_effect=GitError("object read unavailable")):
            with self.assertRaisesRegex(train.MemberDeferred, "transport #2 base=.*object read unavailable"):
                self.compose()

    def test_delta_receipt_is_independent_of_local_diff_display_configuration(self):
        expected = self.receipt["delta"]
        for name, value in (("color.diff", "always"), ("diff.noprefix", "true"),
                            ("diff.algorithm", "histogram"), ("core.quotePath", "false")):
            _git(self.clone.path, "config", name, value)
        self.assertEqual(train_transports.delta(self.clone.path, self.base, self.receipt["head"]), expected)

    def test_missing_or_withdrawn_reader_refuses_persisted_receipt(self):
        plan = self.plan()
        for reader in (None, lambda **kw: None):
            self.repository.transports = reader
            with self.subTest(reader=reader), self.assertRaisesRegex(train.MemberDeferred, "transport #2"):
                self.repository.validate_transports(plan)

    def test_fresh_clone_reconstructs_the_same_json_plan_and_keeps_transport_parent(self):
        plan = json.loads(json.dumps(self.plan()))
        clone = resolutions.Replayer(self.repo, os.path.join(self.tmp, "restarted.clone"))
        repository = train_git.Repository(self.project, clone, transports=self.reader)
        clone.ensure()
        self.assertIsNone(plan_git._commit(clone.path, plan["head"]))
        self.assertIsNone(plan_git._commit(clone.path, self.receipt["head"]))
        with mock.patch.dict(os.environ, GIT_AUTHOR_DATE="2026-10-16T04:00:00Z",
                             GIT_COMMITTER_DATE="2026-10-16T04:00:00Z"):
            self.assertEqual(repository._validate(plan), ("main", "refs/heads/" + plan["branch"]))
        self.assertEqual(_git(clone.path, "rev-parse", plan["head"] + "^{tree}"), plan["tree"])
        self.assertEqual(repository.validate_transports(plan), plan["transports"])

    def test_a_packet_that_loses_the_transport_parent_is_refused(self):
        plan = self.plan()
        plan["head"] = self.repository._commit(plan["tree"],
            [self.base, self.member["frozen_sha"]], "missing transport ancestor")
        with self.assertRaisesRegex(train.TrainError, "lost a qualified transport parent"):
            self.repository._validate(plan)

    def test_clean_prefix_keeps_the_exact_transport_base_after_clock_change_and_clone_loss(self):
        first = self.queued(4)
        self.repository.compose("main", self.base, [first])
        prefix = _git(self.clone.path, "rev-parse", "HEAD")
        self.receipt = self.handoff(prefix, "after-clean-fixture")
        def reader(**context):
            return copy.deepcopy(self.receipt) if context["row"]["work_item_id"] == 2 else None
        self.repository.transports = reader
        merged = self.repository.compose("main", self.base, [first, self.member], "same-intent")
        plan = dict(source=self.repository.source, target="main", base=self.base,
                    members=[first, self.member], branch="integration/train-prefix-fixture",
                    nonce="same-intent", **merged)
        clone = resolutions.Replayer(self.repo, os.path.join(self.tmp, "prefix-restart.clone"))
        repository = train_git.Repository(self.project, clone, transports=reader)
        with mock.patch.dict(os.environ, GIT_AUTHOR_DATE="2026-10-16T04:00:00Z",
                             GIT_COMMITTER_DATE="2026-10-16T04:00:00Z"):
            self.assertEqual(repository._validate(json.loads(json.dumps(plan))),
                             ("main", "refs/heads/" + plan["branch"]))
        self.assertEqual(merged["transports"][0]["base"], prefix)

    def test_incident_facts_use_qualified_delta_but_preserve_source_lot_identity(self):
        plan = self.plan()
        plan["schema"] = train.SCHEMA
        plan["proof"] = dict(schema="ameesh-train-admissions/1", source=plan["source"],
            base=self.base, members=[dict(admission=list(train.identity(self.member)))])
        ops = types.SimpleNamespace(train_read=lambda key: dict(phase="landed", plan=plan))
        project = dataclasses.replace(self.project, github="o/r", target="main")
        with mock.patch.object(storage, "of", return_value=types.SimpleNamespace(work=ops)):
            facts = train_incidents.PacketFacts(None, project, self.clone.path)
            rows = facts.lands("o/r", plan["head"])
        self.assertEqual([(r["sha"], r["lot"], r["files"], r["transport"]) for r in rows],
                         [(self.member["frozen_sha"], 2, ["shared.py"], self.receipt["head"])])


class TransportSelectionTest(unittest.TestCase):
    def make(self):
        self.ports = MemoryPorts([row(1), row(2), row(3)])
        self.policy = train.Policy(enabled=True, build_beta=False)
        self.engine = train.Engine(self.ports, source="github.com/o/r", target="main", policy=self.policy)

    def test_incompatible_member_and_dependents_wait_without_ejecting_queue_or_blocking_others(self):
        self.make()
        self.ports.edges = [dict(work_item_id=2, blocker_id=1, blocking=True)]
        compose = self.ports.compose
        def selected(base, members, nonce):
            if any(member["work_item_id"] == 1 for member in members):
                raise train.MemberDeferred(row(1), "transport #1: intermediate base changed")
            return compose(base, members, nonce)
        self.ports.compose = selected
        outcome = self.engine.step(3600)
        self.assertEqual(outcome["result"], "ci")
        self.assertEqual([r["work_item_id"] for r in outcome["plan"]["members"]], [3])
        self.assertEqual([r["work_item_id"] for r in self.ports.document["deferred"]], [1, 2])
        self.assertIn("transport #1", self.ports.document["selection"][0]["reason"])
        self.assertEqual([r["work_item_id"] for r in self.ports.rows], [1, 2, 3])
        self.assertEqual([effect[0] for effect in self.ports.effects], ["publish", "ci"])

    def test_all_deferred_produces_a_named_empty_packet_with_no_external_effect(self):
        self.make()
        def deferred(base, members, nonce):
            raise train.MemberDeferred(members[0], "context unavailable #%d" % members[0]["work_item_id"])
        self.ports.compose = deferred
        outcome = self.engine.step(3600)
        self.assertEqual(outcome["result"], "empty")
        self.assertEqual(len(outcome["selection"]), 3)
        self.assertFalse(self.ports.effects)
        self.assertFalse(self.ports.held)

    def test_receipt_is_persisted_separately_and_revalidated_before_each_effect(self):
        self.make()
        compose = self.ports.compose
        receipt = dict(admission=list(train.identity(row(1))), qualification="fixture")
        self.ports.compose = lambda *args: dict(compose(*args), transports=[receipt])
        self.ports.validate_transports = mock.Mock(side_effect=lambda plan: [receipt])
        outcome = self.engine.step(3600)
        plan = outcome["plan"]
        self.assertEqual(plan["transports"], [receipt])
        self.assertNotIn("transports", plan["proof"])
        self.ports.document = json.loads(json.dumps(self.ports.document))
        self.ports.validate_transports.assert_called()
        # The original reservation epoch, plan and CI head survive restart.
        restarted = train.Engine(self.ports, source="github.com/o/r", target="main", policy=self.policy)
        restarted.epoch = self.engine.epoch
        self.ports.ci_by_head[plan["head"]] = "green"
        self.assertEqual(restarted.step(3601)["result"], "landed")
        self.assertEqual([e[0] for e in self.ports.effects], ["publish", "ci", "promote"])

    def test_withdrawal_during_ci_never_promotes_or_launches_another_ci(self):
        self.make()
        compose = self.ports.compose
        self.ports.compose = lambda *args: dict(compose(*args), transports=[dict(fixture=True)])
        self.ports.validate_transports = lambda plan: plan["transports"]
        plan = self.engine.step(3600)["plan"]
        self.ports.ci_by_head[plan["head"]] = "green"
        self.ports.validate_transports = lambda plan: []
        result = self.engine.step(3601)
        self.assertEqual(result["result"], "refused")
        self.assertEqual([e[0] for e in self.ports.effects], ["publish", "ci"])
