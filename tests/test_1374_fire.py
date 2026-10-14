# SPDX-License-Identifier: AGPL-3.0-only
"""#1374 : le feu du pilote au gel — atterrissage automatique à la CI verte.

Le feu se donne sur la tête exacte du gel (`ameesh work fire <lot> --sha S`),
se retire (`--withdraw`), et ne vaut jamais pour un autre gel. Le passage
(`fire.run_pass`) franchit **d'abord la porte des verdicts** (avis comptés par
classe, indépendants, sans blocage non levé), puis ouvre la PR ou entre en file,
puis fusionne au geste effectif sous `--match-head-commit` — jamais en laissant
une auto-fusion GitHub armée entre deux passages.

Témoins demandés par le lot : lot sans feu, mauvaise tête, feu retiré, CI
incomplète ou rouge, événement de fusion terminal ; témoins du gel corrigé :
verdicts avant PR/file/CI, avis indépendants et blocages non masqués, sortie de
file et garde de `land` après retrait, jalons relus sous bail, bail renouvelé,
et autorité pilote déclarée. Tests sans base (routage, jalons, GitHub simulé),
puis sur Postgres (règles de `give`/`withdraw`, bail, CLI, garde de `land`)."""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import fire, land, merge_queue, storage, work

from .support import PgTestCase

A = "a" * 40
B = "b" * 40

#: familles simulées pour la porte des avis (le registre est lu en vrai, ou
#: remplacé par ce dictionnaire dans les tests sans base).
FAM = {"author": "deepseek", "rev1": "codex", "rev2": "claude", "rev3": "codex",
       "ghost": "", "other": "deepseek"}


def _mark(kind, sha="", **extra):
    row = {"kind": kind, "sha": sha, "actor": "pilote", "at_ts": 0.0, "id": 0}
    row.update(extra)
    return row


def _marks(*rows):
    return [dict(row, at_ts=float(i), id=i) for i, row in enumerate(rows, start=1)]


def _verdict(actor, sha=A, verdict="ok", **extra):
    return _mark("verdict", sha, verdict=verdict, actor=actor, **extra)


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


def _family(db, name):
    return FAM.get(str(name or "").strip(), "")


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


class ReviewGateTest(unittest.TestCase):
    """La porte des avis (#1374, P1-1) : comptés par classe et par domaine
    (SQL/sécurité/prod = deux avis), indépendants, aucun blocage masqué par un
    `ok` plus récent d'un AUTRE relecteur."""

    ITEM = {"assignee": "author"}
    #: un fichier sensible hors SQL/sécurité/prod : un avis d'une autre famille
    PLAIN = {"path": "docs/design/notes.md", "class": "sensitive", "rule": ["docs/**"]}
    #: un fichier SQL : deux avis, familles distinctes
    SQL = {"path": "src/ameesh/migrations/0437_x.sql", "class": "sensitive",
           "rule": ["src/ameesh/migrations/**", "**/*.sql"]}
    #: la porte de fusion est de la sécurité : deux avis
    GATE = {"path": "src/ameesh/merge_queue.py", "class": "sensitive",
            "rule": ["src/ameesh/merge_queue.py"]}

    def gate(self, marks, klass=None, frozen=A, item=None):
        if isinstance(klass, str):
            klass = {"class": klass, "doubt": False}
            if klass["class"] == "sensitive":
                klass["files"] = [self.PLAIN]
        with mock.patch.object(merge_queue, "actor_family", side_effect=_family):
            return merge_queue.review_gate(None, marks, frozen,
                                           klass=klass or {"class": "sensitive",
                                                           "doubt": False,
                                                           "files": [self.PLAIN]},
                                           item=item or self.ITEM)

    def test_light_needs_no_verdict(self):
        out = self.gate(_marks(_mark("frozen", A)), klass="light")
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual(out["required"], 0)

    def test_blocked_blocks_even_a_light_lot(self):
        out = self.gate(_marks(_mark("frozen", A), _verdict("rev1", verdict="blocked")),
                        klass="light")
        self.assertFalse(out["ok"])
        self.assertIn("blocked verdict", out["why"])
        self.assertIn("rev1", out["why"])

    def test_normal_needs_one_reviewer_that_is_not_the_author(self):
        out = self.gate(_marks(_mark("frozen", A)), klass="normal")
        self.assertFalse(out["ok"])
        self.assertEqual(out["required"], 1)
        out = self.gate(_marks(_mark("frozen", A), _verdict("author")), klass="normal")
        self.assertFalse(out["ok"], "l'auteur ne relit pas son propre travail")
        out = self.gate(_marks(_mark("frozen", A), _verdict("ghost")), klass="normal")
        self.assertTrue(out["ok"], out["why"])

    def test_sensitive_outside_critical_domains_needs_one_other_family(self):
        out = self.gate(_marks(_mark("frozen", A)))
        self.assertFalse(out["ok"])
        self.assertEqual(out["required"], 1)
        out = self.gate(_marks(_mark("frozen", A), _verdict("rev1")))
        self.assertTrue(out["ok"], out["why"])
        unknown = self.gate(_marks(_mark("frozen", A), _verdict("ghost")))
        self.assertFalse(unknown["ok"], "une famille inconnue ne prouve pas l'indépendance")
        same = self.gate(_marks(_mark("frozen", A), _verdict("other")))
        self.assertFalse(same["ok"], "la famille de l'auteur ne compte pas")

    def test_sql_and_merge_gate_need_two_distinct_families(self):
        for entry in (self.SQL, self.GATE):
            klass = {"class": "sensitive", "files": [entry]}
            one = self.gate(_marks(_mark("frozen", A), _verdict("rev1")), klass=klass)
            self.assertFalse(one["ok"], entry["path"])
            self.assertEqual(one["required"], 2, entry["path"])
            two = self.gate(_marks(_mark("frozen", A), _verdict("rev1"), _verdict("rev2")),
                            klass=klass)
            self.assertTrue(two["ok"], two["why"])
            same = self.gate(_marks(_mark("frozen", A), _verdict("rev1"), _verdict("rev3")),
                             klass=klass)
            self.assertFalse(same["ok"], "deux avis de la même famille ne comptent qu'une fois")
            unknown = self.gate(_marks(_mark("frozen", A), _verdict("rev1"), _verdict("ghost")),
                                klass=klass)
            self.assertFalse(unknown["ok"], "une famille inconnue ne prouve pas l'indépendance")

    def test_author_family_never_counts(self):
        klass = {"class": "sensitive", "files": [self.SQL]}
        out = self.gate(_marks(_mark("frozen", A), _verdict("rev1"), _verdict("other")),
                        klass=klass)
        self.assertFalse(out["ok"], "l'avis de la famille de l'auteur ne compte pas")
        self.assertEqual([a["actor"] for a in out["accepted"]], ["rev1"])

    def test_a_doubt_keeps_one_verdict_without_supposing_a_family(self):
        klass = {"class": "sensitive", "doubt": True, "files": []}
        out = self.gate(_marks(_mark("frozen", A)), klass=klass)
        self.assertFalse(out["ok"])
        self.assertEqual(out["required"], 1)
        out = self.gate(_marks(_mark("frozen", A), _verdict("ghost")), klass=klass)
        self.assertTrue(out["ok"], out["why"])
        out = self.gate(_marks(_mark("frozen", A), _verdict("author")), klass=klass)
        self.assertFalse(out["ok"], "l'auteur ne relit pas son propre travail")

    def test_a_newer_ok_from_another_reviewer_does_not_mask_a_block(self):
        klass = {"class": "sensitive", "files": [self.SQL]}
        marks = _marks(_mark("frozen", A), _verdict("rev1", verdict="blocked"),
                       _verdict("rev2", verdict="ok"))
        out = self.gate(marks, klass=klass)
        self.assertFalse(out["ok"])
        self.assertIn("rev1", out["why"])
        lifted = _marks(_mark("frozen", A), _verdict("rev1", verdict="blocked"),
                        _verdict("rev2"), _verdict("rev1", verdict="ok"))
        out = self.gate(lifted, klass=klass)
        self.assertTrue(out["ok"], out["why"])

    def test_verdict_on_another_freeze_is_ignored(self):
        out = self.gate(_marks(_mark("frozen", B), _verdict("rev1", sha=B),
                               _verdict("rev2", sha=B)))
        self.assertFalse(out["ok"], "un avis sur un autre gel n'autorise rien")

    def test_release_keeps_the_verdict_in_the_queue_gate(self):
        # la file garde un avis pour une release (jamais légère, L160e) ; c'est
        # le **feu** qui refuse une release (test_release_is_refused, ReadyTest).
        out = self.gate(_marks(_mark("frozen", A)), klass="release")
        self.assertFalse(out["ok"])
        self.assertEqual(out["required"], 1)
        self.assertIn("class: release", out["why"])
        ok = self.gate(_marks(_mark("frozen", A), _verdict("rev1")), klass="release")
        self.assertTrue(ok["ok"], ok["why"])


class ReadyTest(unittest.TestCase):
    """Les portes du feu : verdicts d'abord (sans PR), puis PR/CI."""

    ROW = {"id": 42, "title": "t", "branch": "lot/42-x", "assignee": "author"}

    def _verdicts(self, marks, klass, *, repo=None, project=None, target="main"):
        with mock.patch.object(fire.lot_class, "of_lot", return_value=klass), \
                mock.patch.object(merge_queue, "actor_family", side_effect=_family):
            return fire.ready_verdicts(None, None, self.ROW, marks, repo=repo,
                                       project=project, target=target)

    def test_light_passes_without_a_pr(self):
        ok, why, klass = self._verdicts(_marks(_mark("frozen", A)), {"class": "light"})
        self.assertTrue(ok, why)
        self.assertEqual(klass["class"], "light")

    def test_sensitive_requires_a_family_before_any_pr(self):
        marks = _marks(_mark("frozen", A))
        klass = {"class": "sensitive", "doubt": False, "files": [
            {"path": "docs/x.md", "class": "sensitive", "rule": ["docs/**"]}]}
        ok, why, _ = self._verdicts(marks, klass)
        self.assertFalse(ok)
        self.assertIn("independent review", why)
        good = _marks(_mark("frozen", A), _verdict("rev1"))
        ok, why, _ = self._verdicts(good, klass)
        self.assertTrue(ok, why)

    def test_sql_needs_two_families_before_any_pr(self):
        klass = {"class": "sensitive", "files": [
            {"path": "src/ameesh/migrations/0437_x.sql", "class": "sensitive",
             "rule": ["**/*.sql"]}]}
        ok, why, _ = self._verdicts(_marks(_mark("frozen", A), _verdict("rev1")), klass)
        self.assertFalse(ok)
        self.assertIn("2 independent review", why)
        ok, why, _ = self._verdicts(_marks(_mark("frozen", A), _verdict("rev1"),
                                           _verdict("rev2")), klass)
        self.assertTrue(ok, why)

    def test_release_is_refused(self):
        ok, why, _ = self._verdicts(_marks(_mark("frozen", A), _verdict("rev1")),
                                    {"class": "release"})
        self.assertFalse(ok)
        self.assertIn("release", why)

    def test_pr_gates_need_a_green_ci_on_the_frozen_head(self):
        for ci in ("pending", "red"):
            ok, why = fire.ready_pr(_status(ci=ci), target="main", project=None, frozen=A)
            self.assertFalse(ok)
            self.assertIn(ci, why)
        ok, why = fire.ready_pr(_status(draft=True), target="main", project=None, frozen=A)
        self.assertFalse(ok)
        self.assertIn("draft", why)
        ok, why = fire.ready_pr(_status(base="develop"), target="main", project=None, frozen=A)
        self.assertFalse(ok)
        self.assertIn("targets", why)
        ok, why = fire.ready_pr(_status(), target="main", project=None, frozen=A)
        self.assertTrue(ok, why)


class ActTest(unittest.TestCase):
    """Le routage d'un passage : verdicts avant PR/file/CI, fusion au geste
    effectif, jamais d'auto-fusion laissée armée, jamais deux gestes
    concurrents."""

    ROW = {"id": 42, "title": "un lot", "branch": "lot/42-x", "assignee": "author",
           "app": "ameesh", "branch_target": "main", "state": "build"}

    def _act(self, marks, status, *, gate=(True, "", {"class": "light"}), pr_gate=(True, ""),
             info=None, project=None, dry_run=False, opened=None, fresh=None, renew=True,
             statuses=None, merge=(True, "PR merged")):
        names = ("_repo_of", "_slug", "_project", "_pr_status", "_repo_info", "_target",
                 "ready_verdicts", "ready_pr", "_merge_pr", "_disarm_auto", "_reconcile",
                 "_invalidate", "_disarm", "_open_pr", "_queue", "_passive", "_wake",
                 "_leave_queue", "_fresh", "_renew")
        with mock.patch.multiple(fire, **{name: mock.DEFAULT for name in names}) as patched:
            patched["_repo_of"].return_value = None
            patched["_slug"].return_value = "manaty/ameesh"
            patched["_project"].return_value = project
            if statuses is not None:
                patched["_pr_status"].side_effect = list(statuses)
            else:
                patched["_pr_status"].return_value = status
            patched["_repo_info"].return_value = info if info is not None else _info()
            patched["_target"].return_value = "main"
            patched["ready_verdicts"].return_value = gate
            patched["ready_pr"].return_value = pr_gate
            patched["_merge_pr"].return_value = merge
            patched["_disarm_auto"].return_value = (True, "auto-merge disarmed")
            patched["_reconcile"].return_value = {"lot": 42, "result": "merged"}
            patched["_invalidate"].return_value = {"lot": 42, "result": "invalidated"}
            patched["_disarm"].return_value = {"lot": 42, "result": "disarmed"}
            patched["_open_pr"].return_value = opened
            patched["_queue"].return_value = {"lot": 42, "result": "queued"}
            patched["_leave_queue"].return_value = False
            patched["_fresh"].return_value = fresh if fresh is not None else (self.ROW, marks)
            patched["_renew"].return_value = renew
            out = fire._act(None, None, self.ROW, marks, gh=lambda *a, **k: None,
                            dry_run=dry_run, repo=None, cache={})
        return out, patched

    def test_lot_without_fire_is_idle(self):
        out, calls = self._act([], None)
        self.assertEqual(out["result"], "idle")
        calls["_merge_pr"].assert_not_called()
        calls["_disarm"].assert_not_called()
        calls["_reconcile"].assert_not_called()

    def test_withdrawn_fire_disarms_and_leaves_the_queue(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A),
                                      _mark("unfire", A)), _status(armed=True))
        self.assertEqual(out["result"], "disarmed")
        calls["_disarm"].assert_called_once()
        self.assertIn("no active fire", calls["_disarm"].call_args.args[3])
        calls["_leave_queue"].assert_called_once()
        calls["_merge_pr"].assert_not_called()

    def test_merged_pr_is_terminal(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)),
                               _status(state="MERGED", merged=True, merge_commit=B))
        self.assertEqual(out["result"], "merged")
        calls["_reconcile"].assert_called_once()
        calls["_merge_pr"].assert_not_called()
        calls["_invalidate"].assert_not_called()

    def test_wrong_freeze_invalidates(self):
        out, calls = self._act(_marks(_mark("frozen", B), _mark("fire", A)), _status())
        self.assertEqual(out["result"], "invalidated")
        calls["_invalidate"].assert_called_once()
        self.assertIn("the freeze is", calls["_invalidate"].call_args.args[4])
        calls["_merge_pr"].assert_not_called()

    def test_moved_head_invalidates(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)),
                               _status(head=B))
        self.assertEqual(out["result"], "invalidated")
        calls["_invalidate"].assert_called_once()
        self.assertIn("the PR head moved", calls["_invalidate"].call_args.args[4])
        calls["_merge_pr"].assert_not_called()

    def test_verdicts_gate_before_opening_the_pr(self):
        """L'ordre du gel corrigé (B) : sans avis, aucune PR, donc aucune CI."""
        out, calls = self._act(
            _marks(_mark("frozen", A), _mark("fire", A)), None,
            gate=(False, "2 independent review verdict(s) ok required", {"class": "sensitive"}))
        self.assertEqual(out["result"], "not-ready")
        self.assertIn("independent review", out["detail"])
        calls["_open_pr"].assert_not_called()
        calls["_merge_pr"].assert_not_called()
        calls["_queue"].assert_not_called()

    def test_verdicts_gate_before_the_merge_queue(self):
        project = types.SimpleNamespace(name="nexlink", land="queue", merge_method="merge",
                                        ci_required=False)
        out, calls = self._act(
            _marks(_mark("frozen", A), _mark("fire", A)), _status(), project=project,
            gate=(False, "blocked verdict(s) on the freeze a not lifted: rev1",
                  {"class": "sensitive"}))
        self.assertEqual(out["result"], "not-ready")
        calls["_queue"].assert_not_called()
        calls["_merge_pr"].assert_not_called()

    def test_gate_failure_disarms_an_armed_pr(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)),
                               _status(armed=True),
                               gate=(False, "blocked verdict(s)", {"class": "sensitive"}))
        self.assertEqual(out["result"], "disarmed")
        calls["_disarm"].assert_called_once()
        calls["_merge_pr"].assert_not_called()
        calls["_open_pr"].assert_not_called()

    def test_ready_merges_now_on_the_exact_head(self):
        marks = _marks(_mark("frozen", A), _mark("fire", A))
        out, calls = self._act(marks, _status())
        self.assertEqual(out["result"], "merged")
        calls["_merge_pr"].assert_called_once()
        self.assertEqual(calls["_merge_pr"].call_args.args[2], A)
        calls["_open_pr"].assert_not_called()

    def test_stale_auto_merge_is_disarmed_before_merging(self):
        marks = _marks(_mark("frozen", A), _mark("fire", A))
        out, calls = self._act(marks, None,
                               statuses=[_status(armed=True), _status(), _status()])
        self.assertEqual(out["result"], "merged")
        calls["_disarm"].assert_called_once()
        calls["_merge_pr"].assert_called_once()
        self.assertIn("outside the fire", calls["_disarm"].call_args.args[3])

    def test_ci_pending_does_not_merge(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), _status(),
                               pr_gate=(False, "the CI of PR #7 is pending on the freeze a"))
        self.assertEqual(out["result"], "not-ready")
        calls["_merge_pr"].assert_not_called()

    def test_dry_run_touches_nothing(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), None,
                               dry_run=True)
        self.assertEqual(out["result"], "would-open-pr")
        calls["_open_pr"].assert_not_called()
        calls["_merge_pr"].assert_not_called()
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), _status(),
                               dry_run=True)
        self.assertEqual(out["result"], "would-merge")
        calls["_merge_pr"].assert_not_called()

    def test_land_project_enters_the_queue_after_the_gate(self):
        project = types.SimpleNamespace(name="nexlink", land="queue", merge_method="merge",
                                        ci_required=False)
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), _status(),
                               project=project)
        self.assertEqual(out["result"], "queued")
        calls["_queue"].assert_called_once()
        calls["_merge_pr"].assert_not_called()

    def test_lost_lease_before_the_merge_is_busy(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), _status(),
                               renew=False)
        self.assertEqual(out["result"], "busy")
        calls["_merge_pr"].assert_not_called()

    def test_merge_refused_and_head_moved_invalidates(self):
        marks = _marks(_mark("frozen", A), _mark("fire", A))
        out, calls = self._act(marks, None, statuses=[_status(), _status(head=B)],
                               merge=(False, "head moved"))
        self.assertEqual(out["result"], "invalidated")
        calls["_merge_pr"].assert_called_once()
        calls["_invalidate"].assert_called_once()

    def test_missing_pr_is_opened_only_after_the_gate_then_merged(self):
        marks = _marks(_mark("frozen", A), _mark("fire", A))
        out, calls = self._act(marks, None, opened=(_status(), "opened"))
        self.assertEqual(out["result"], "merged")
        calls["_open_pr"].assert_called_once()
        calls["_merge_pr"].assert_called_once()

    def test_unpublishable_pr_is_not_opened(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)), None,
                               opened=(None, "title not publishable (secret)"))
        self.assertEqual(out["result"], "no-pr")
        calls["_merge_pr"].assert_not_called()

    def test_closed_pr_is_not_reopened(self):
        out, calls = self._act(_marks(_mark("frozen", A), _mark("fire", A)),
                               _status(state="CLOSED"))
        self.assertEqual(out["result"], "pr-closed")
        calls["_merge_pr"].assert_not_called()


class LandGuardCallTest(unittest.TestCase):
    """La garde de `land` au dernier geste (#1374, P1-2)."""

    def test_land_refuses_when_the_fire_is_gone(self):
        ejected = {}

        def eject(reason, todo, **extra):
            ejected.update(reason=reason, todo=todo)
            return {"lot": 7, "result": "ejected"}

        landing = types.SimpleNamespace(db=None, lot=7, row={"frozen_sha": A},
                                        project=types.SimpleNamespace(repo="/r"), eject=eject)
        with mock.patch.object(fire, "land_guard", return_value=(False, "fire withdrawn")):
            out = land._fire_guard(landing)
        self.assertEqual(out["result"], "ejected")
        self.assertIn("fire withdrawn", ejected["reason"])
        self.assertIn("queue remove", ejected["todo"])

    def test_land_continues_without_fire_markers(self):
        landing = types.SimpleNamespace(db=None, lot=7, row={"frozen_sha": A},
                                        project=types.SimpleNamespace(repo="/r"),
                                        eject=lambda *a, **k: {"result": "ejected"})
        with mock.patch.object(fire, "land_guard", return_value=(True, "")):
            self.assertIsNone(land._fire_guard(landing))


class FireDbTest(PgTestCase):
    """Le feu en base : `give`/`withdraw`, bail, passage, CLI, garde de land."""

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

    def test_give_and_withdraw_take_the_fire_lease(self):
        lot = self._frozen_lot(A)
        ops = storage.of(self.db).work
        key = "fire:%d" % lot
        epoch = ops.land_lock(key, "another-host", 60.0)
        self.assertIsNotNone(epoch)
        try:
            with self.assertRaises(fire.FireError) as caught:
                fire.give(self.db, lot, sha=A, actor="pilote")
            self.assertIn("another fire operation", str(caught.exception))
        finally:
            ops.land_unlock(key, "another-host", epoch)
        fire.give(self.db, lot, sha=A, actor="pilote")
        epoch = ops.land_lock(key, "another-host", 60.0)
        try:
            with self.assertRaises(fire.FireError) as caught:
                fire.withdraw(self.db, lot, actor="pilote")
            self.assertIn("another fire operation", str(caught.exception))
        finally:
            ops.land_unlock(key, "another-host", epoch)

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

    def test_one_rereads_the_milestones_under_the_lease(self):
        """P1-3 : les jalons lus avant le bail ne servent pas au geste."""
        lot = self._frozen_lot(A)
        fire.give(self.db, lot, sha=A, actor="pilote")
        stale = []          # ce que le relevé avait lu avant de prendre le bail
        seen = {}

        def act(cfg, db, row, marks, **kwargs):
            seen["marks"] = marks
            return {"lot": lot, "result": "idle"}

        with mock.patch.object(fire, "_act", side_effect=act):
            out = fire._one(self.cfg, self.db, work.get(self.db, lot), stale,
                            gh=lambda *a, **k: None, dry_run=False, repo=None, cache={},
                            lock=True)
        self.assertEqual(out["result"], "idle")
        self.assertTrue(fire.active(seen["marks"]),
                        "le passage doit relire le feu sous le bail")
        self.assertTrue(any(m["kind"] == "frozen" for m in seen["marks"]))

    def test_land_guard_without_fire_is_open(self):
        lot = self._frozen_lot(A)
        ok, why = fire.land_guard(self.db, lot, A)
        self.assertTrue(ok, why)

    def test_land_guard_requires_an_active_fire_covering_the_freeze(self):
        lot = self._frozen_lot(A)
        fire.give(self.db, lot, sha=A, actor="pilote")
        ok, why = fire.land_guard(self.db, lot, A)
        self.assertTrue(ok, why)
        ok, why = fire.land_guard(self.db, lot, B)
        self.assertFalse(ok)
        self.assertIn("covers", why)
        fire.withdraw(self.db, lot, actor="pilote")
        ok, why = fire.land_guard(self.db, lot, A)
        self.assertFalse(ok)
        self.assertIn("withdrawn or invalidated", why)

    def test_land_guard_refuses_an_unlifted_block(self):
        lot = self._frozen_lot(A)
        fire.give(self.db, lot, sha=A, actor="pilote")
        work.milestone(self.db, lot, "verdict", sha=A, actor="rev1", verdict="blocked")
        ok, why = fire.land_guard(self.db, lot, A)
        self.assertFalse(ok)
        self.assertIn("blocked verdict", why)
        work.milestone(self.db, lot, "verdict", sha=A, actor="rev1", verdict="ok")
        ok, why = fire.land_guard(self.db, lot, A)
        self.assertTrue(ok, why)

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

    def test_cli_refuses_an_executor_whose_only_authority_is_history(self):
        """P1-5 : avoir confié des lots récemment ne donne pas le feu."""
        lot = self._frozen_lot(A)
        operations = storage.of(self.db).operations
        with mock.patch.object(operations.__class__, "assigners", return_value=["impl1"]):
            self.assertIn("impl1", operations.assigners(since_s=0))
            refused = self.mesh("work", "fire", str(lot), "--sha", A,
                                env=self.env(AGENT_MAIL_NAME="impl1",
                                             AMEESH_ALERT_ORCHESTRATORS=""))
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("pilot's gesture", refused.stderr)
        self.assertFalse(fire.state(self.db, lot)["active"])

    def test_cli_authority_accepts_a_declared_orchestrator(self):
        """Le même exécutant, déclaré orchestrateur, donne le feu."""
        lot = self._frozen_lot(A)
        given = self.mesh("work", "fire", str(lot), "--sha", A,
                          env=self.env(AGENT_MAIL_NAME="impl1",
                                       AMEESH_ALERT_ORCHESTRATORS="impl1"))
        self.assertEqual(given.returncode, 0, given.stderr)
        self.assertTrue(fire.state(self.db, lot)["active"])


if __name__ == "__main__":
    unittest.main()
