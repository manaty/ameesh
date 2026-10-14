# SPDX-License-Identifier: AGPL-3.0-only
"""#1374 : le feu du pilote au gel — atterrissage automatique à la CI verte.

Le feu se donne sur la tête exacte du gel (`ameesh work fire <lot> --sha S`),
se retire (`--withdraw`), et ne vaut jamais pour un autre gel. Le passage
(`fire.run_pass`) arme l'auto-fusion GitHub dès que les portes sont vertes
(verdicts de la classe, CI verte sur la tête, cible non-release), invalide un
feu dont la tête a bougé, et réconcilie une PR fusionnée.

Témoins demandés par le lot : lot sans feu, mauvaise tête, feu retiré, CI
incomplète ou rouge, événement de fusion terminal. Tests sans base (routage et
jalons, GitHub simulé), puis sur Postgres (règles de `give`/`withdraw`, bail,
CLI)."""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import fire, storage, work

from .support import PgTestCase

A = "a" * 40
B = "b" * 40


def _mark(kind, sha="", **extra):
    row = {"kind": kind, "sha": sha, "actor": "pilote", "at_ts": 0.0, "id": 0}
    row.update(extra)
    return row


def _marks(*rows):
    return [dict(row, at_ts=float(i), id=i) for i, row in enumerate(rows, start=1)]


def _status(**extra):
    row = {"number": 7, "state": "OPEN", "head": A, "base": "main", "draft": False,
           "ci": "green", "merged": False, "merge_commit": "", "armed": False,
           "url": "https://forge/pr/7"}
    row.update(extra)
    return row


def _info(**extra):
    row = {"slug": "manaty/ameesh", "allow_auto_merge": True, "default_branch": "main",
           "private": True, "merge": True, "squash": True, "rebase": True}
    row.update(extra)
    return row


class MarkersTest(unittest.TestCase):
    """Les jalons du feu : le dernier `fire` non suivi d'un `unfire`."""

    def test_active_and_given(self):
        rows = _marks(_mark("frozen", A), _mark("fire", A, actor="pilote"))
        self.assertEqual(fire.active(rows)["sha"], A)
        self.assertEqual(fire.given(rows)["actor"], "pilote")
        withdrawn = _marks(_mark("frozen", A), _mark("fire", A), _mark("unfire", A))
        self.assertIsNone(fire.active(withdrawn))
        self.assertEqual(fire.given(withdrawn)["sha"], A)
        again = _marks(_mark("frozen", B), _mark("fire", A), _mark("unfire", A),
                       _mark("fire", B))
        self.assertEqual(fire.active(again)["sha"], B)

    def test_same_commit_without_repository(self):
        self.assertTrue(fire.same_commit(A, A.upper()))
        self.assertFalse(fire.same_commit(A, B))
        self.assertFalse(fire.same_commit("", A))

    def test_state_covered_and_withdrawn(self):
        item = {"id": 3, "title": "t", "state": "build", "branch": "lot/3-x"}
        covered = _marks(_mark("frozen", A), _mark("fire", A))
        with mock.patch.object(fire.work, "get", return_value=item), \
                mock.patch.object(fire.work, "milestones", return_value=covered):
            now = fire.state(None, 3)
        self.assertTrue(now["active"])
        self.assertTrue(now["covered"])
        moved = _marks(_mark("frozen", B), _mark("fire", A))
        with mock.patch.object(fire.work, "get", return_value=item), \
                mock.patch.object(fire.work, "milestones", return_value=moved):
            now = fire.state(None, 3)
        self.assertTrue(now["active"])
        self.assertFalse(now["covered"])
        gone = _marks(_mark("frozen", A), _mark("fire", A), _mark("unfire", A))
        with mock.patch.object(fire.work, "get", return_value=item), \
                mock.patch.object(fire.work, "milestones", return_value=gone):
            now = fire.state(None, 3)
        self.assertFalse(now["active"])
        self.assertTrue(now["withdrawn"])


class ReadyTest(unittest.TestCase):
    """Les portes : verdicts de la classe, tête, CI, cible."""

    ROW = {"id": 42, "title": "t", "branch": "lot/42-x"}

    def _ready(self, marks, status, klass, *, project=None, target="main"):
        with mock.patch.object(fire.lot_class, "of_lot", return_value=klass):
            return fire.ready(None, None, self.ROW, marks, status, repo=None,
                              project=project, target=target)

    def test_light_needs_no_verdict(self):
        ok, why, klass = self._ready(_marks(_mark("frozen", A)), _status(),
                                     {"class": "light"})
        self.assertTrue(ok, why)

    def test_sensible_needs_a_verdict_on_the_freeze(self):
        marks = _marks(_mark("frozen", A))
        ok, why, _ = self._ready(marks, _status(), {"class": "sensible"})
        self.assertFalse(ok)
        self.assertIn("no review verdict", why)
        good = _marks(_mark("frozen", A), _mark("verdict", A, verdict="ok"))
        ok, why, _ = self._ready(good, _status(), {"class": "sensible"})
        self.assertTrue(ok, why)
        other = _marks(_mark("frozen", A), _mark("verdict", B, verdict="ok"))
        ok, why, _ = self._ready(other, _status(), {"class": "sensible"})
        self.assertFalse(ok)
        blocked = _marks(_mark("frozen", A), _mark("verdict", A, verdict="blocked"))
        ok, _, _ = self._ready(blocked, _status(), {"class": "sensible"})
        self.assertFalse(ok)

    def test_release_is_never_a_fire(self):
        ok, why, _ = self._ready(_marks(_mark("frozen", A)), _status(), {"class": "release"})
        self.assertFalse(ok)
        self.assertIn("release", why)

    def test_ci_incomplete_or_red_does_not_arm(self):
        marks = _marks(_mark("frozen", A))
        for ci in ("pending", "red"):
            ok, why, _ = self._ready(marks, _status(ci=ci), {"class": "light"})
            self.assertFalse(ok)
            self.assertIn(ci, why)

    def test_draft_and_wrong_base_do_not_arm(self):
        marks = _marks(_mark("frozen", A))
        ok, why, _ = self._ready(marks, _status(draft=True), {"class": "light"})
        self.assertFalse(ok)
        self.assertIn("draft", why)
        ok, why, _ = self._ready(marks, _status(base="develop"), {"class": "light"})
        self.assertFalse(ok)
        self.assertIn("targets", why)


class ActTest(unittest.TestCase):
    """Le routage d'un passage sur un lot : jamais deux gestes concurrents."""

    ROW = {"id": 42, "title": "un lot", "branch": "lot/42-x", "assignee": "agent",
           "app": "ameesh", "branch_target": "main", "state": "build"}

    def _act(self, marks, status, *, ready=(True, "", {"class": "light"}), info=None,
             project=None, dry_run=False, opened=None):
        names = ("_repo_of", "_slug", "_project", "_pr_status", "_repo_info", "_target",
                 "ready", "_arm_auto", "_disarm_auto", "_reconcile", "_invalidate",
                 "_disarm", "_open_pr", "_queue", "_passive", "_wake")
        with mock.patch.multiple(fire, **{name: mock.DEFAULT for name in names}) as patched:
            patched["_repo_of"].return_value = None
            patched["_slug"].return_value = "manaty/ameesh"
            patched["_project"].return_value = project
            patched["_pr_status"].return_value = status
            patched["_repo_info"].return_value = info if info is not None else _info()
            patched["_target"].return_value = "main"
            patched["ready"].return_value = ready
            patched["_arm_auto"].return_value = (True, "auto-merge armed")
            patched["_disarm_auto"].return_value = (True, "auto-merge disarmed")
            patched["_reconcile"].return_value = {"lot": 42, "result": "merged"}
            patched["_invalidate"].return_value = {"lot": 42, "result": "invalidated"}
            patched["_disarm"].return_value = {"lot": 42, "result": "disarmed"}
            patched["_open_pr"].return_value = opened
            patched["_queue"].return_value = {"lot": 42, "result": "queued"}
            out = fire._act(None, None, self.ROW, marks, gh=lambda *a, **k: None,
                            dry_run=dry_run, repo=None, cache={})
        return out, patched

    def test_lot_without_fire_is_idle(self):
        out, calls = self._act([], None)
        self.assertEqual(out["result"], "idle")
        calls["_arm_auto"].assert_not_called()
        calls["_disarm"].assert_not_called()
        calls["_reconcile"].assert_not_called()

    def test_withdrawn_fire_disarms(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A),
                                      _mark("unfire", A)), _status(armed=True))
        self.assertEqual(out["result"], "disarmed")
        calls["_disarm"].assert_called_once()
        self.assertIn("no active fire", calls["_disarm"].call_args.args[3])
        calls["_arm_auto"].assert_not_called()

    def test_merged_pr_is_terminal(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)),
                               _status(state="MERGED", merged=True, merge_commit=B))
        self.assertEqual(out["result"], "merged")
        calls["_reconcile"].assert_called_once()
        calls["_arm_auto"].assert_not_called()
        calls["_invalidate"].assert_not_called()

    def test_wrong_freeze_invalidates(self):
        out, calls = self._act(_marks(_mark("frozen", B), _mark("fire", A)), _status())
        self.assertEqual(out["result"], "invalidated")
        calls["_invalidate"].assert_called_once()
        self.assertIn("the freeze is", calls["_invalidate"].call_args.args[4])
        calls["_arm_auto"].assert_not_called()

    def test_moved_head_invalidates(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)),
                               _status(head=B))
        self.assertEqual(out["result"], "invalidated")
        calls["_invalidate"].assert_called_once()
        self.assertIn("the PR head moved", calls["_invalidate"].call_args.args[4])
        calls["_arm_auto"].assert_not_called()

    def test_ready_arms_once(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), _status())
        self.assertEqual(out["result"], "armed")
        calls["_arm_auto"].assert_called_once()
        self.assertEqual(calls["_arm_auto"].call_args.args[2], A)
        self.assertEqual(calls["_passive"].call_count, 2)

    def test_already_armed_is_idempotent(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)),
                               _status(armed=True))
        self.assertEqual(out["result"], "armed")
        calls["_arm_auto"].assert_not_called()
        calls["_disarm"].assert_not_called()

    def test_ci_red_does_not_arm(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), _status(),
                               ready=(False, "the CI of PR #7 is pending on the freeze a",
                                      {"class": "light"}))
        self.assertEqual(out["result"], "not-ready")
        calls["_arm_auto"].assert_not_called()

    def test_gates_lost_disarms_an_armed_pr(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)),
                               _status(armed=True),
                               ready=(False, "the CI of PR #7 is red on the freeze a",
                                      {"class": "sensible"}))
        self.assertEqual(out["result"], "disarmed")
        calls["_disarm"].assert_called_once()
        calls["_arm_auto"].assert_not_called()

    def test_dry_run_arms_nothing(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), _status(),
                               dry_run=True)
        self.assertEqual(out["result"], "would-arm")
        calls["_arm_auto"].assert_not_called()

    def test_missing_pr_is_opened_then_armed(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), None,
                               opened=(_status(), "opened"))
        self.assertEqual(out["result"], "armed")
        calls["_open_pr"].assert_called_once()
        calls["_arm_auto"].assert_called_once()

    def test_unpublishable_pr_is_not_opened(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), None,
                               opened=(None, "title not publishable (secret)"))
        self.assertEqual(out["result"], "no-pr")
        calls["_arm_auto"].assert_not_called()

    def test_closed_pr_is_not_reopened(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)),
                               _status(state="CLOSED"))
        self.assertEqual(out["result"], "pr-closed")
        calls["_arm_auto"].assert_not_called()

    def test_land_project_enters_the_queue(self):
        project = types.SimpleNamespace(name="nexlink", land="queue", merge_method="merge",
                                        ci_required=False)
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), _status(),
                               project=project)
        self.assertEqual(out["result"], "queued")
        calls["_queue"].assert_called_once()
        calls["_arm_auto"].assert_not_called()

    def test_repository_without_auto_merge_falls_back_to_the_queue(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), _status(),
                               info=_info(allow_auto_merge=False))
        self.assertEqual(out["result"], "queued")
        calls["_queue"].assert_called_once()
        calls["_arm_auto"].assert_not_called()


class FireDbTest(PgTestCase):
    """Le feu en base : `give`/`withdraw`, bail, passage, CLI."""

    def _frozen_lot(self, sha=A):
        lot = work.add(self.db, title="un lot", branch="lot/1-un-lot")["id"]
        work.milestone(self.db, lot, "frozen", sha=sha, actor="pilote")
        return lot

    def test_give_refuses_without_a_freeze(self):
        lot = work.add(self.db, title="un lot", branch="lot/1-un-lot")["id"]
        with self.assertRaises(fire.FireError) as caught:
            fire.give(self.db, lot, sha=A, actor="pilote")
        self.assertIn("not frozen", str(caught.exception))

    def test_give_refuses_a_head_other_than_the_freeze(self):
        lot = self._frozen_lot(A)
        with self.assertRaises(fire.FireError) as caught:
            fire.give(self.db, lot, sha=B, actor="pilote")
        self.assertIn("the fire names", str(caught.exception))
        with self.assertRaises(fire.FireError):
            fire.give(self.db, lot, sha="not-a-commit", actor="pilote")

    def test_give_then_withdraw(self):
        lot = self._frozen_lot(A)
        row = fire.give(self.db, lot, sha=A, actor="pilote")
        self.assertEqual(row["kind"], "fire")
        now = fire.state(self.db, lot)
        self.assertTrue(now["active"])
        self.assertTrue(now["covered"])
        gone = fire.withdraw(self.db, lot, actor="pilote")
        self.assertEqual(gone["kind"], "unfire")
        now = fire.state(self.db, lot)
        self.assertFalse(now["active"])
        self.assertTrue(now["withdrawn"])
        with self.assertRaises(fire.FireError) as caught:
            fire.withdraw(self.db, lot, actor="pilote")
        self.assertIn("no active fire", str(caught.exception))

    def test_give_refuses_a_closed_lot(self):
        lot = self._frozen_lot(A)
        work.close(self.db, lot, abandoned=True, actor="pilote")
        with self.assertRaises(fire.FireError) as caught:
            fire.give(self.db, lot, sha=A, actor="pilote")
        self.assertIn("no fire to give", str(caught.exception))

    def test_pass_skips_a_lot_without_fire(self):
        lot = work.add(self.db, title="un lot", branch="lot/1-un-lot")["id"]
        self.assertEqual(fire.run_pass(self.cfg, self.db, only_lot=lot), [])

    def test_pass_reports_an_unknown_repository(self):
        lot = self._frozen_lot(A)
        fire.give(self.db, lot, sha=A, actor="pilote")
        out = fire.run_pass(self.cfg, self.db, only_lot=lot, dry_run=True)
        self.assertEqual([e["result"] for e in out], ["no-repository"])

    def test_pass_yields_to_another_host_holding_the_lease(self):
        lot = self._frozen_lot(A)
        fire.give(self.db, lot, sha=A, actor="pilote")
        ops = storage.of(self.db).work
        key = "fire:%d" % lot
        epoch = ops.land_lock(key, "another-host", 60.0)
        self.assertIsNotNone(epoch)
        try:
            out = fire.run_pass(self.cfg, self.db, only_lot=lot)
            self.assertEqual(out, [{"lot": lot, "result": "busy"}])
        finally:
            ops.land_unlock(key, "another-host", epoch)

    def test_cli_state_and_fire_flags(self):
        lot = self._frozen_lot(A)
        shown = self.mesh("work", "fire", str(lot))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("no active fire", shown.stdout)
        refused = self.mesh("work", "milestone", str(lot), "fire", "--sha", A)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("invalid choice", refused.stderr)
        both = self.mesh("work", "fire", str(lot), "--sha", A, "--withdraw")
        self.assertEqual(both.returncode, 2)
        self.assertIn("exclusive", both.stderr)

    def test_cli_gives_and_withdraws(self):
        lot = self._frozen_lot(A)
        given = self.mesh("work", "fire", str(lot), "--sha", A, "--actor", "human:pilote")
        self.assertEqual(given.returncode, 0, given.stderr)
        self.assertIn("fire given", given.stdout)
        self.assertTrue(fire.state(self.db, lot)["active"])
        withdrawn = self.mesh("work", "fire", str(lot), "--withdraw",
                              "--actor", "human:pilote")
        self.assertEqual(withdrawn.returncode, 0, withdrawn.stderr)
        self.assertIn("fire withdrawn", withdrawn.stdout)
        self.assertFalse(fire.state(self.db, lot)["active"])


if __name__ == "__main__":
    unittest.main()
