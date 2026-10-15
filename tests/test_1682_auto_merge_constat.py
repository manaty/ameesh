# SPDX-License-Identifier: AGPL-3.0-only
"""#1682 — auto-fusion GitHub : une PR fusionnée par l'auto-merge est un fait
constaté, pas une éjection.

Deux fenêtres du même défaut, dans `_land` mode `pr` : entre la lecture de la
PR et le désarmement (`gh pr merge --disable-auto` échoue alors sur une PR
déjà fusionnée), et entre deux passes (plus aucune PR ouverte). Si la fusion
porte sur le SHA gelé et la cible du lot, elle est enregistrée comme atterrie
(et ouvre le lot de relecture §2 pour un lot léger) ; une fusion à une autre
tête (réécriture #1162) ou dans une autre base reste traitée comme avant —
fail closed pour la tête, alerte `WRONG BASE` pour la base.
"""
from __future__ import annotations

import json
import subprocess

from .test_1656_auto_merge import AutoGh
from .test_l160e2_land import PrLandTest


class RaceGh(AutoGh):
    """L'auto-fusion se déclenche dans la fenêtre : le désarmement échoue sur
    une PR déjà fusionnée, et `gh pr list --state merged` la retrouve."""

    def __init__(self, *, merge_on_disable=False, merged=False, merged_head=None,
                 merged_base="main"):
        super().__init__()
        self.merge_on_disable = merge_on_disable
        self.merged = merged
        self.merged_head = merged_head
        self.merged_base = merged_base

    def __call__(self, args, timeout=60.0):
        if args[:2] == ["pr", "list"] and "--state" in args and "merged" in args:
            self.calls.append(args)
            rows = ([{"number": 9, "headRefOid": self.merged_head,
                      "baseRefName": self.merged_base}] if self.merged else [])
            return subprocess.CompletedProcess(args, 0, json.dumps(rows), "")
        if args[:2] == ["pr", "merge"] and "--disable-auto" in args and self.merge_on_disable:
            self.calls.append(args)
            self.disarm_calls += 1
            self.merged, self.auto = True, False
            return subprocess.CompletedProcess(
                args, 1, "", "cannot disable auto-merge on a merged pull request")
        if args[:2] == ["pr", "list"] and self.merged:
            # plus de PR ouverte : la fusion est un fait
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 0, "[]", "")
        return super().__call__(args, timeout=timeout)


class AutoMergeConstatTest(PrLandTest):
    def race(self, *, head=None, base="main"):
        """Le lot 4 est en file, l'auto-merge armé, et la fusion se déclenche
        au moment du désarmement — à `head` (le gel par défaut) et dans la
        cible `base`."""
        gh = RaceGh(merge_on_disable=True, merged_base=base)
        self.queued(4)
        gh.head = self.tip(4)
        gh.merged_head = head or self.tip(4)
        gh.auto = True
        return gh

    def test_the_merge_fired_by_the_window_is_constated_not_ejected(self):
        gh = self.race()
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "landed", out)
        self.assertEqual(out["pr"], 9)
        row = self.fake.queue_rows[4]
        self.assertEqual(row["state"], "landed")
        self.assertIn(self.tip(4)[:12], row["reason"])
        self.assertEqual(gh.disarm_calls, 1)
        self.assertFalse(any("--match-head-commit" in c for c in gh.calls))
        # the queue never replayed this merge: the record says who merged, and
        # never that the queue's gates passed on the merged tree
        self.assertIn("constated", row["reason"])
        self.assertNotIn("merged by ameesh", row["reason"])
        self.assertTrue(any("did not run on the merged tree" in body
                            for _, body, _ in self.sent))
        self.assertFalse(any("Gates passed on the merged tree" in body
                             for _, body, _ in self.sent))

    def test_a_merge_at_another_head_stays_fail_closed(self):
        gh = self.race(head="f" * 40)
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("auto-merge", self.fake.queue_rows[4]["reason"])
        self.assertFalse(any("--match-head-commit" in c for c in gh.calls))

    def test_a_merge_found_before_the_pass_is_constated(self):
        gh = RaceGh()
        self.queued(4)
        gh.merged, gh.merged_head = True, self.tip(4)
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "landed", out)
        self.assertEqual(out["pr"], 9)
        self.assertEqual(self.fake.queue_rows[4]["state"], "landed")

    def test_a_merge_into_another_base_is_still_reported(self):
        gh = self.race(base="release")
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "landed", out)
        self.assertEqual(out["wrong_base"], "release")
        self.assertIn("WRONG BASE", self.fake.queue_rows[4]["reason"])

    def test_a_closed_pr_without_a_matching_merge_is_still_ejected(self):
        gh = RaceGh()
        self.queued(4)
        gh.merged, gh.merged_head = True, "f" * 40
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("no open PR", self.fake.queue_rows[4]["reason"])

    def test_following_constates_a_merge_fired_by_the_window(self):
        gh = RaceGh()
        self.queued(4)
        gh.head = self.tip(4)
        first = self.land(4, gh=gh)
        self.assertEqual(first["result"], "landing", first)
        # CI verte, auto-merge armé sur la tête d'atterrissage : GitHub fusionne
        # entre la lecture de la PR et le désarmement
        gh.merge_on_disable = True
        gh.auto, gh.head, gh.ci = True, first["sha"], "green"
        gh.merged_head = first["sha"]
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "landed", out)
        self.assertEqual(out["pr"], 9)
        self.assertEqual(self.fake.queue_rows[4]["state"], "landed")
        self.assertEqual(gh.disarm_calls, 1)

    def test_following_still_ejects_when_no_merge_is_found(self):
        gh = RaceGh()
        self.queued(4)
        gh.head = self.tip(4)
        first = self.land(4, gh=gh)
        gh.auto, gh.head, gh.ci = True, first["sha"], "pending"
        gh.disable_error = "HTTP 403: must have admin rights"
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("auto-merge", self.fake.queue_rows[4]["reason"])
