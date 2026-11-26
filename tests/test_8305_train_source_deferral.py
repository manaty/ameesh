# SPDX-License-Identifier: AGPL-3.0-only
"""#8305 : la source CI auditée différée au lot qui la change, pas au train.

Le prévol de composition lit l'arbre final et remonte les arbres exacts de
chaque lot. Un seul lot fautif est retiré (les lots indépendants restent dans
le paquet), son journal porte la raison, et la porte de publication garde le
dernier mot sur tout héritage de base ou lecture Git impossible.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import (resolutions, train, train_admissions, train_ci, train_git,
                    train_runtime, work)
from . import test_1467_train_runtime
from .support import PgTestCase
from .test_1467_train import row
from .test_l160e2_land import _Land
from .test_l160b_radar import _git, _write

SOURCE = ".github/workflows/lot-status.yml"
SELECTOR = "scripts/ci/lot-plan.mjs"


def contract(blob):
    return (train_ci.Workflow(SOURCE, ("suite",), sources=((SOURCE, blob),)),)


def member(lot, sha):
    return dict(work_item_id=lot, generation=lot, target="main", frozen_id=lot,
                frozen_sha=sha, source="github.com/o/r")


class GitFixture(unittest.TestCase):
    """Un dépôt minimal : base auditée, deux lots, une composition finale."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _write(self.repo, SOURCE, "audited\n")
        _write(self.repo, SELECTOR, "selector\n")
        _write(self.repo, "shared.py", "base\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        self.base = _git(self.repo, "rev-parse", "HEAD")
        self.expected = _git(self.repo, "rev-parse", "HEAD:" + SOURCE)
        self.selector_expected = _git(self.repo, "rev-parse", "HEAD:" + SELECTOR)

    def tearDown(self):
        self.tmp.cleanup()

    def branch(self, name, edits, base=None):
        _git(self.repo, "checkout", "-q", "-b", name, base or self.base)
        for path, text in edits.items():
            if text is None:
                os.remove(os.path.join(self.repo, path))
            else:
                _write(self.repo, path, text)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", name)
        tip = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "main")
        return tip

    def compose(self, tips, base=None):
        base = base or self.base
        _git(self.repo, "checkout", "-q", "-B", "integration", base)
        for tip in tips:
            _git(self.repo, "merge", "-q", "--no-ff", "-m", "integration", tip)
        head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "main")
        return head

    def check(self, head, members, blob=None, base=None):
        return train_ci.deferred_source_member(
            self.repo, dict(base=base or self.base, head=head, members=members),
            contract(blob or self.expected), members)


class DynamicContract:
    """Un contrat dont la résolution de tête refuse, mais dont les pins sont lus."""
    def __init__(self, source, blob):
        self.source, self.blob = source, blob
        self.sources_for_called = False

    def audited_sources(self):
        return {self.source: self.blob}

    def sources_for(self, plan, *, repo, cache=None):
        self.sources_for_called = True
        raise train.PublicationRefused("selector differs from its audited commit")


class AttributionTest(GitFixture):
    def test_only_the_lot_whose_delta_changes_the_source_is_named(self):
        bad = self.branch("lot/1-bad", {SOURCE: "changed\n", "a.txt": "a\n"})
        good = self.branch("lot/2-good", {"b.txt": "b\n"})
        head = self.compose([bad, good])
        members = [member(1, bad), member(2, good)]
        found = self.check(head, members)
        self.assertEqual(found[0]["work_item_id"], 1)
        self.assertIn(SOURCE, found[1])
        self.assertIn("canon", found[1])

    def test_a_compliant_final_tree_accepts_an_intermediate_change(self):
        """L'ordre inverse : le lot 2 restaure le blob audité après le lot 1."""
        bad = self.branch("lot/1-bad", {SOURCE: "changed\n", "a.txt": "a\n"})
        good = self.branch("lot/2-good", {SOURCE: "audited\n", "b.txt": "b\n"}, base=bad)
        head = self.compose([bad, good])
        members = [member(1, bad), member(2, good)]
        self.assertIsNone(self.check(head, members))

    def test_a_dynamic_contract_attributes_before_resolving_the_head(self):
        """#8305 : un commentaire dans le sélecteur ne doit pas tout bloquer."""
        bad = self.branch("lot/1-selector", {
            SELECTOR: "selector\n// reviewed comment\n"})
        head = self.compose([bad])
        dynamic = DynamicContract(SELECTOR, self.selector_expected)
        found = train_ci.deferred_source_member(
            self.repo, dict(base=self.base, head=head, members=[member(1, bad)]),
            dynamic, [member(1, bad)])
        self.assertEqual(found[0]["work_item_id"], 1)
        self.assertFalse(dynamic.sources_for_called,
                         "l'attribution ne doit pas résoudre les suites de tête")

    def test_a_deleted_source_names_the_lot_that_deleted_it(self):
        bad = self.branch("lot/1-delete", {SOURCE: None})
        head = self.compose([bad])
        found = self.check(head, [member(1, bad)])
        self.assertEqual(found[0]["work_item_id"], 1)

    def test_an_inherited_mismatch_names_nobody(self):
        _write(self.repo, SOURCE, "inherited\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "inherited mismatch")
        base = _git(self.repo, "rev-parse", "HEAD")
        good = self.branch("lot/1-good", {"a.txt": "a\n"}, base=base)
        head = self.compose([good], base=base)
        self.assertIsNone(self.check(head, [member(1, good)], base=base))

    def test_an_unreadable_repository_names_nobody(self):
        self.assertIsNone(train_ci.deferred_source_member(
            os.path.join(self.tmp.name, "missing"),
            dict(base=self.base, head="a" * 40, members=[]),
            contract(self.expected), []))


class CompositionAttributionTest(_Land):
    """La vraie composition du train : le lot 1 change la source auditée."""
    def setUp(self):
        super().setUp()
        _write(self.repo, SOURCE, "audited\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "audited CI source")
        _git(self.repo, "push", "-q", "origin", "main")
        self.expected = _git(self.repo, "rev-parse", "main:" + SOURCE)
        # The lot branches predate the audited source: bring the new base to
        # each one so the source change is a real delta, not an add/add conflict.
        for branch in ("lot/1-a", "lot/2-b", "lot/3-c", "lot/4-d"):
            _git(self.repo, "checkout", "-q", branch)
            _git(self.repo, "merge", "-q", "--no-ff", "-m", "base source", "main")
            _git(self.repo, "push", "-q", "origin", branch)
        _git(self.repo, "checkout", "-q", "lot/1-a")
        _write(self.repo, SOURCE, "changed by lot 1\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "lot 1 changes the audited source")
        _git(self.repo, "push", "-q", "origin", "lot/1-a")
        _git(self.repo, "checkout", "-q", "main")
        self.clone = resolutions.Replayer(self.repo,
                                         os.path.join(self.tmp, "train.clone"))
        self.repository = train_git.Repository(self.project, self.clone)
        self.contract = contract(self.expected)

    def plan_for(self, lots):
        members = [self.queued(lot) for lot in lots]
        base = self.repository.target("main")
        merged = self.repository.compose("main", base, members)
        return dict(source=self.repository.source, target="main", base=base,
                    members=members, **merged)

    def test_the_final_tree_names_the_workflow_lot_only(self):
        plan = self.plan_for([1, 4])
        found = train_ci.deferred_source_member(
            self.clone.path, plan, self.contract, plan["members"])
        self.assertEqual(found[0]["work_item_id"], 1)
        self.assertIn(SOURCE, found[1])

    def test_runtime_preflight_raises_member_deferred_and_notes_it(self):
        plan = self.plan_for([1, 4])
        runtime = object.__new__(train_runtime.Runtime)
        runtime.repository = self.repository
        runtime.ci_adapter = mock.Mock(workflows=self.contract)
        runtime.admissions = mock.Mock()
        runtime.admissions.note_deferred.return_value = ""
        runtime.target_branch = "main"
        runtime.notify = mock.Mock()
        with self.assertRaises(train.MemberDeferred) as caught:
            runtime.preflight(plan)
        self.assertEqual(caught.exception.admission, train.identity(plan["members"][0]))
        runtime.admissions.note_deferred.assert_called_once()
        self.assertIn(SOURCE, runtime.admissions.note_deferred.call_args.args[1])


class EngineDeferralTest(test_1467_train_runtime.TrainRuntimeTest):
    """Le moteur retire le lot différé et compose/publie les autres."""
    def test_a_named_source_member_is_deferred_alone_and_the_rest_is_published(self):
        self.memory.rows = [row(1), row(2)]
        first = self.memory.rows[0]
        self.admissions.note_deferred.return_value = ""
        with mock.patch.object(train_ci, "deferred_source_member",
                               side_effect=[(first, "source CI auditée changée"), None]):
            outcome = self.engine.step(3600)
        self.assertEqual(outcome["result"], "ci", outcome)
        state = self.ledger.load()
        self.assertEqual([r["work_item_id"] for r in state["plan"]["members"]], [2])
        self.assertEqual(state["selection"][0]["admission"], list(train.identity(first)))
        self.assertIn("source CI auditée changée", state["selection"][0]["reason"])
        self.admissions.note_deferred.assert_called_once_with(
            first, "source CI auditée changée")
        self.assertEqual(self.repository.publish.call_count, 1)

    def test_an_abstaining_preflight_publishes_every_member(self):
        self.memory.rows = [row(1), row(2)]
        with mock.patch.object(train_ci, "deferred_source_member", return_value=None):
            outcome = self.engine.step(3600)
        self.assertEqual(outcome["result"], "ci", outcome)
        self.assertEqual([r["work_item_id"] for r in self.ledger.load()["plan"]["members"]],
                         [1, 2])


class DeferredNoteTest(PgTestCase):
    def test_the_note_is_written_once_and_its_failure_is_reported(self):
        admissions = object.__new__(train_admissions.Admissions)
        admissions.db = object()
        with mock.patch.object(work, "note") as note:
            self.assertEqual(admissions.note_deferred(row(1), "why"), "")
        note.assert_called_once_with(admissions.db, 1, "train: why",
                                     actor="ameesh train", once=True)
        with mock.patch.object(work, "note", side_effect=work.WorkError("journal down")):
            self.assertEqual(admissions.note_deferred(row(1), "why"), "journal down")

    def test_the_exact_deferral_is_journaled_once_on_the_real_lot(self):
        item = work.add(self.db, title="deferred source", app="fixture",
                        source="fixture", actor="fixture")
        admissions = object.__new__(train_admissions.Admissions)
        admissions.db = self.db
        member = dict(row(1), work_item_id=item["id"])
        self.assertEqual(admissions.note_deferred(member, "source changed"), "")
        self.assertEqual(admissions.note_deferred(member, "source changed"), "")
        notes = [event.get("note") or "" for event in work.events(self.db, item["id"])]
        self.assertEqual(notes.count("train: source changed"), 1)
