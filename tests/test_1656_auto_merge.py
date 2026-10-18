# SPDX-License-Identifier: AGPL-3.0-only
"""#1656 — auto-fusion GitHub : une tête réécrite sous un auto-merge armé ne
doit pas atterrir. `gh pr merge --auto --match-head-commit` n'est vérifié
qu'à l'armement ; ensuite GitHub fusionne ce qu'il trouve. Témoins : la file
désarme avant toute réécriture de tête, avant d'attendre la CI et avant de
fusionner elle-même ; si le désarmement échoue, rien n'est poussé et le lot
est éjecté (fail closed)."""
from __future__ import annotations

import dataclasses
import json
import subprocess

from ameesh import land, merge_pr

from .test_1003_merge_pr import FakeGh as MergeGh
from .test_1003_merge_pr import _Base as MergePrBase
from .test_1003_merge_pr import view as merge_view
from . import test_l160e2_land as _land
from .test_l160e2_land import FakeGh, _git


class AutoGh(FakeGh):
    """Le faux `gh` des atterrissages, avec l'auto-fusion de la PR."""

    def __init__(self):
        super().__init__()
        self.auto = False
        self.disable_error = None
        self.disarm_calls = 0

    def __call__(self, args, timeout=60.0):
        if args[:2] == ["pr", "merge"] and "--disable-auto" in args:
            self.calls.append(args)
            self.disarm_calls += 1
            if self.disable_error:
                return subprocess.CompletedProcess(args, 1, "", self.disable_error)
            self.auto = False
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["pr", "list"] and "--state" in args and "merged" in args:
            # #1682 : le faux `gh` ne modelise que la PR ouverte ; par defaut
            # aucune PR fusionnee — un desarmement qui echoue n'est pas une
            # fusion (les temoins de la fenetre posent `merged` eux-memes).
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 0, "[]", "")
        if args[:2] == ["pr", "list"]:
            proc = super().__call__(args, timeout=timeout)
            rows = json.loads(proc.stdout or "[]")
            if rows:
                rows[0]["autoMergeRequest"] = ({"enabledAt": "2026-10-15T12:28:00Z"}
                                               if self.auto else None)
            return subprocess.CompletedProcess(args, proc.returncode, json.dumps(rows),
                                               proc.stderr)
        return super().__call__(args, timeout=timeout)


class AutoMergeTest(_land.PrLandTest):
    def test_pr_status_exposes_the_armed_auto_merge(self):
        gh = AutoGh()
        self.queued(4)
        gh.head = self.tip(4)
        self.assertFalse(land.pr_status("o/r", "lot/4-d", gh=gh)["auto"])
        gh.auto = True
        self.assertTrue(land.pr_status("o/r", "lot/4-d", gh=gh)["auto"])

    def test_the_head_rewrite_disarms_first(self):
        gh = AutoGh()
        self.queued(4)
        gh.head = self.tip(4)
        gh.auto = True
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "landing", out)
        self.assertEqual(gh.disarm_calls, 1)
        self.assertFalse(gh.auto)
        # la tête a bien été réécrite (le merge est poussé après le désarmement)
        self.assertEqual(_git(self.origin, "rev-parse", "lot/4-d"), out["sha"])

    def test_a_failed_disarm_refuses_the_head_rewrite(self):
        gh = AutoGh()
        self.queued(4)
        gh.head = self.tip(4)
        gh.auto = True
        gh.disable_error = "HTTP 403: must have admin rights"
        before = _git(self.origin, "rev-parse", "lot/4-d")
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "ejected", out)
        row = self.fake.queue_rows[4]
        self.assertIn("auto-merge", row["reason"])
        self.assertEqual(_git(self.origin, "rev-parse", "lot/4-d"), before)

    def test_following_disarms_before_waiting_then_merges_itself(self):
        gh = AutoGh()
        self.queued(4)
        gh.head = self.tip(4)
        first = self.land(4, gh=gh)
        self.assertEqual(first["result"], "landing", first)
        # CI en cours : l'auto-merge armé est désarmé, rien n'est fusionné
        gh.auto, gh.head, gh.ci = True, first["sha"], "pending"
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "landing", out)
        self.assertEqual(gh.disarm_calls, 1)
        self.assertFalse(any("--match-head-commit" in c for c in gh.calls))
        # CI verte : ameesh fusionne lui-même la tête exacte, jamais l'auto
        gh.auto, gh.ci = True, "green"
        done = self.land(4, gh=gh)
        self.assertEqual(done["result"], "landed", done)
        merge_calls = [c for c in gh.calls if "--match-head-commit" in c]
        self.assertEqual(len(merge_calls), 1)
        self.assertEqual(merge_calls[0][-2:], ["--match-head-commit", first["sha"]])
        self.assertEqual(gh.disarm_calls, 2)

    def test_following_ejects_when_the_disarm_fails(self):
        gh = AutoGh()
        self.queued(4)
        gh.head = self.tip(4)
        first = self.land(4, gh=gh)
        gh.auto, gh.head, gh.ci = True, first["sha"], "pending"
        gh.disable_error = "boom"
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("auto-merge", self.fake.queue_rows[4]["reason"])
        self.assertFalse(any("--match-head-commit" in c for c in gh.calls))

    def test_an_unarmed_pr_is_never_disarmed(self):
        gh = AutoGh()
        self.queued(4)
        gh.head = self.tip(4)
        first = self.land(4, gh=gh)
        self.assertEqual(first["result"], "landing", first)
        gh.head, gh.ci = first["sha"], "green"
        self.assertEqual(self.land(4, gh=gh)["result"], "landed")
        self.assertEqual(gh.disarm_calls, 0)

    def test_a_changed_head_under_auto_merge_is_neutralized_then_ejected(self):
        gh = AutoGh()
        self.queued(4)
        gh.head = self.tip(4)
        self.land(4, gh=gh)
        gh.auto, gh.ci = True, "green"
        gh.head = "f" * 40
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("moved", self.fake.queue_rows[4]["reason"])
        self.assertEqual(gh.disarm_calls, 1)
        self.assertFalse(gh.auto, "l'armement est neutralisé avant l'éjection")
        self.assertFalse(any("--match-head-commit" in c for c in gh.calls))

    def test_a_rewritten_head_with_the_same_content_is_still_refused(self):
        """Le SHA est la preuve : un contenu identique sous une nouvelle tête
        n'a pas de parts validées et ne doit pas atterrir sous l'auto-merge."""
        gh = AutoGh()
        self.queued(4)
        gh.head = self.tip(4)
        first = self.land(4, gh=gh)
        self.assertEqual(first["result"], "landing", first)
        _git(self.repo, "fetch", "-q", "origin", "lot/4-d:lot/4-d")
        tree = _git(self.repo, "rev-parse", "lot/4-d^{tree}")
        parent = _git(self.repo, "rev-parse", "lot/4-d^")
        moved = _git(self.repo, "commit-tree", tree, "-p", parent, "-m",
                     "same content, new sha")
        _git(self.repo, "push", "-q", "-f", "origin", "%s:lot/4-d" % moved)
        self.assertEqual(_git(self.origin, "rev-parse", moved + "^{tree}"), tree)
        gh.auto, gh.head, gh.ci = True, moved, "green"
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("moved", self.fake.queue_rows[4]["reason"])
        self.assertEqual(gh.disarm_calls, 1)
        self.assertFalse(any("--match-head-commit" in c for c in gh.calls))


class DisarmGh(MergeGh):
    """`gh` de `merge_pr` : l'appel `--disable-auto` ne décale pas les rangs
    des vues (FakeGh les compte) ; il est relevé à part."""

    def __init__(self, first, fresh=None, later=None):
        super().__init__(first, fresh=fresh, later=later)
        self.disarms: list = []
        self.disable_error = None

    def __call__(self, args, *, timeout=60.0):
        if args[:2] == ["pr", "merge"] and "--disable-auto" in args:
            if self.disable_error:
                return subprocess.CompletedProcess(["gh", *args], 1, "", self.disable_error)
            self.disarms.append(list(args))
            return subprocess.CompletedProcess(["gh", *args], 0, "", "")
        return super().__call__(args, timeout=timeout)


class AutoMergeDirectTest(MergePrBase):
    def test_the_direct_merge_disarms_before_its_proof(self):
        gh = DisarmGh(merge_view(headRefOid=self.head,
                                 autoMergeRequest={"enabledAt": "2026-10-15T12:28:00Z"}),
                      later=lambda: merge_view(state="MERGED", headRefOid=self.head,
                                               mergeCommit={"oid": self.tip()}))
        out = self.merge(gh)
        self.assertEqual(out["state"], "merged", out)
        self.assertEqual(len(gh.disarms), 1)
        self.assertEqual(gh.disarms[0][:2], ["pr", "merge"])
        self.assertTrue(all(c[:2] == ["pr", "view"] for c in gh.calls), gh.calls)

    def test_a_failed_disarm_writes_nothing(self):
        gh = DisarmGh(merge_view(headRefOid=self.head,
                                 autoMergeRequest={"enabledAt": "2026-10-15T12:28:00Z"}))
        gh.disable_error = "HTTP 403: must have admin rights"
        with self.assertRaises(merge_pr.MergePrError) as caught:
            self.merge(gh)
        self.assertIn("auto-merge", str(caught.exception))
        self.assertEqual(self.tip(), self.base)      # rien n'a été poussé


class AutoMergeEarlyEjectTest(_land.PrLandTest):
    def test_an_ejection_before_the_push_still_disarms(self):
        """Relecture dev1 : un lot éjecté avant la poussée (porte, conflit, CI)
        ne laisse pas l'auto-merge armé pour la prochaine tête de l'auteur."""
        self.project = dataclasses.replace(self.project, gates=("echo boom; exit 3",))
        gh = AutoGh()
        self.queued(4)
        gh.head = self.tip(4)
        gh.auto = True
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("gate", self.fake.queue_rows[4]["reason"])
        self.assertEqual(gh.disarm_calls, 1)
        self.assertFalse(gh.auto)
