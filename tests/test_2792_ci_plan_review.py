# SPDX-License-Identifier: AGPL-3.0-only
"""#2792 — le plan de CI (`tests/parts/`) reste un delta relu du véhicule.

Le verdict dev1 sur #2766 a relevé que `packet_reviews._TEST` exempte tout
`tests/**`, donc aussi `tests/parts/*.txt`, qui pilotent la CI (chemins
sensibles §8). Un second constat demande d'aligner les autres appelants de
`review_gate` sur la lecture du delta propre du véhicule.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import lot_class, merge_queue, packet_candidates, packet_reviews

from .test_l160b_radar import _git


def _write(repo, path, text):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(text)


def _commit_all(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _merge(repo, branch, lot):
    _git(repo, "merge", "-q", "--no-ff", branch, "-m",
         "Merge #%d — composition\n\nameesh-work: %d" % (lot, lot))
    return _git(repo, "rev-parse", "HEAD")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2792-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _write(self.repo, "README.md", "base\n")
        self.base = _commit_all(self.repo, "base")
        # un membre déjà relu, puis sa composition dans le véhicule
        _git(self.repo, "checkout", "-q", "-b", "m1", "main")
        _write(self.repo, "supabase/migrations/528_x.sql", "select 1;\n")
        self.member = _commit_all(self.repo, "membre")
        _git(self.repo, "checkout", "-q", "main")
        _merge(self.repo, "m1", 101)
        self.vehicle = _git(self.repo, "rev-parse", "HEAD")

    def case(self, branch, path, text, message):
        """Un commit du véhicule sur sa propre branche, depuis la composition."""
        _git(self.repo, "checkout", "-q", "-b", branch, self.vehicle)
        _write(self.repo, path, text)
        return _commit_all(self.repo, message)


class ReviewFreeTest(unittest.TestCase):
    def test_a_ci_plan_part_is_not_a_test_adaptation(self):
        for path in ("tests/parts/toujours.txt", "tests/parts/durees.json",
                     "x/tests/parts/toujours.txt", "tests/parts"):
            with self.subTest(path=path):
                self.assertFalse(packet_reviews._is_review_free(path))

    def test_a_test_module_stays_review_free(self):
        for path in ("tests/test_2102.py", "ui/chat.test.ts", "tests/helpers.py"):
            with self.subTest(path=path):
                self.assertTrue(packet_reviews._is_review_free(path))

    def test_a_packet_manifest_stays_review_free(self):
        self.assertTrue(packet_reviews._is_review_free(
            "reviews/integration-paquet-h-1700.md"))


class OwnChangesTest(_Base):
    def own(self, head):
        return packet_reviews.own_changes(self.repo, self.base, head, [self.member])

    def test_a_ci_plan_change_is_part_of_the_vehicle_delta(self):
        head = self.case("case-plan", "tests/parts/toujours.txt", "test_2102\n",
                         "h-1700 : plan de CI")
        own = self.own(head)
        self.assertEqual(own["paths"], ["tests/parts/toujours.txt"])
        self.assertEqual(own["commits"], [head])

    def test_a_test_adaptation_alone_leaves_no_delta(self):
        head = self.case("case-adapt", "tests/test_2102.py", "# fixture\n",
                         "h-1700 : adaptation")
        own = self.own(head)
        self.assertEqual(own["paths"], [])
        self.assertEqual(own["commits"], [])


class GateForwardTest(unittest.TestCase):
    """`review_gate_for_item` lit la porte comme le feu : cfg et cible passés."""

    def test_review_gate_for_item_forwards_cfg_and_target(self):
        sha = "a" * 40
        marks = [{"kind": "frozen", "sha": sha, "actor": "dev1", "at_ts": 1.0}]
        item = {"id": 7, "app": "ameesh", "branch": "integration/paquet"}
        cfg = object()
        with mock.patch.object(merge_queue.plan_git, "latest_freeze",
                               return_value={"sha": sha, "kind": "frozen"}), \
                mock.patch.object(lot_class, "repo_for_freeze",
                                  return_value=("/repo", sha)), \
                mock.patch.object(merge_queue, "_class_base", return_value=sha), \
                mock.patch.object(merge_queue.plan_git, "_commit", return_value=sha), \
                mock.patch.object(lot_class, "default_base",
                                  return_value="main"), \
                mock.patch.object(lot_class, "of_lot",
                                  return_value={"class": "light", "files": []}), \
                mock.patch.object(merge_queue, "review_gate",
                                  return_value={"ok": True}) as gate:
            out = merge_queue.review_gate_for_item(None, cfg, item, marks)
        self.assertEqual(out, {"ok": True})
        self.assertIs(gate.call_args.kwargs["cfg"], cfg)
        self.assertEqual(gate.call_args.kwargs["target"], "main")


class CandidateForwardTest(unittest.TestCase):
    """`packet_candidates` lit la même porte que le feu (#2792, N dev1)."""

    def test_a_candidate_forwards_cfg_and_target(self):
        cfg, frozen = object(), "a" * 40
        row = {"id": 7, "app": "ameesh", "branch": "lot/7-x",
               "branch_target": "main", "title": "", "assignee": ""}
        with mock.patch.object(packet_candidates.plan_git, "freeze_candidate",
                               return_value={"sha": frozen}), \
                mock.patch.object(merge_queue, "pinned", return_value=frozen), \
                mock.patch.object(lot_class, "of_lot",
                                  return_value={"class": "light", "files": []}), \
                mock.patch.object(merge_queue, "review_gate",
                                  return_value={"ok": False, "why": "x"}) as gate, \
                mock.patch.object(packet_candidates, "merge_tree",
                                  return_value={"state": "clean"}), \
                mock.patch.object(packet_candidates, "_ancestor", return_value=False):
            packet_candidates._candidate(
                None, row, target_branch="main", tips={"lot/7-x": frozen},
                where="/repo", package_base="b" * 40, marks=[], cfg=cfg)
        self.assertIs(gate.call_args.kwargs["cfg"], cfg)
        self.assertEqual(gate.call_args.kwargs["target"], "main")

    def test_a_remise_forwards_cfg_and_its_target(self):
        cfg = object()
        frozen, other, sha = "a" * 40, "c" * 40, "b" * 40
        row = {"id": 7, "app": "ameesh", "branch": "lot/7-x",
               "branch_target": "main", "title": "", "assignee": ""}
        with mock.patch.object(packet_candidates, "_git_out",
                               return_value=object()), \
                mock.patch.object(packet_candidates, "_text",
                                  return_value="%s %s" % (frozen, other)), \
                mock.patch.object(packet_candidates, "_ancestor", return_value=True), \
                mock.patch.object(packet_candidates, "merge_tree",
                                  return_value={"state": "clean"}), \
                mock.patch.object(lot_class, "of_lot",
                                  return_value={"class": "light", "files": []}), \
                mock.patch.object(merge_queue, "review_gate",
                                  return_value={"ok": True,
                                                "accepted": ["dev1"]}) as gate:
            out = packet_candidates.evaluate_remise(
                None, row, [], repo="/repo", package_base="d" * 40,
                frozen=frozen, ref="refs/remises/7-x", sha=sha,
                klass={"class": "light", "files": []}, cfg=cfg)
        self.assertTrue(out["ok"])
        self.assertIs(gate.call_args.kwargs["cfg"], cfg)
        self.assertEqual(gate.call_args.kwargs["target"], "main")
