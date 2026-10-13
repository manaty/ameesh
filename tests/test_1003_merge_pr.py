# SPDX-License-Identifier: AGPL-3.0-only
"""#1003 REL-11 : fusion hors API gh d'une PR touchant `.github/workflows/**`.

Le geste est testé sur de vrais dépôts git (un `origin` nu, `refs/pull/<pr>/head`
comme GitHub) et un faux `gh` : gardes (gel, cible non déplacée, arbre, PR hors
workflow refusée, mergeabilité et checks), commit de fusion à deux parents,
push compare-and-swap refusé quand la cible bouge, relecture PR MERGED,
`--dry-run`, et chemins « déjà fusionnée ».
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from ameesh import merge_pr, merge_queue, radar, work

from .test_l160b_radar import _git


def _write(repo, path, text):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(text)


class FakeGh:
    """`gh pr view` : la vue initiale, puis `fresh` (la garde fraîche juste
    avant l'écriture, la vue initiale par défaut), puis `later()` pour chaque
    relecture d'après le push."""

    def __init__(self, first: dict, fresh=None, later=None):
        self.first, self.fresh, self.later, self.calls = first, fresh, later, []

    def __call__(self, args, *, timeout=60.0):
        self.calls.append(list(args))
        rank = len(self.calls)
        if rank == 1:
            data = self.first
        elif rank == 2:
            data = self.fresh if self.fresh is not None else self.first
        elif self.later is not None:
            data = self.later()
        else:
            data = self.first
        return subprocess.CompletedProcess(["gh", *args], 0, json.dumps(data), "")


def view(**over):
    data = {"number": 7, "state": "OPEN", "isDraft": False, "baseRefName": "main",
            "baseRefOid": "", "headRefOid": "", "headRefName": "lot/7-work",
            "mergeStateStatus": "CLEAN", "mergeCommit": None, "url": "https://x/7",
            "title": "le lot", "statusCheckRollup": [
                {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED",
                 "conclusion": "SUCCESS"}]}
    data.update(over)
    return data


class _Base(unittest.TestCase):
    LOT = 1003
    BRANCH = "lot/7-work"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-1003-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q")
        _write(self.repo, ".github/workflows/ci.yml", "on: push\n")
        _write(self.repo, "src/app.py", "print('base')\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "base")
        _git(self.repo, "branch", "-M", "main")
        self.base = _git(self.repo, "rev-parse", "main")
        self.origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "clone", "-q", "--bare", self.repo, self.origin],
                       check=True, capture_output=True)
        # la branche de la PR : un fichier de workflow ET un fichier de code
        _git(self.repo, "checkout", "-q", "-b", "lot/7-work")
        _write(self.repo, ".github/workflows/ci.yml", "on: push\njobs:\n  t:\n    steps: []\n")
        _write(self.repo, "src/app.py", "print('head')\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "head")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "remote", "add", "origin", self.origin)
        _git(self.repo, "push", "-q", "origin", "lot/7-work")
        _git(self.origin, "update-ref", "refs/pull/7/head", self.head)
        self.slug = "o/r"
        # le lot : gelé et relu sur la tête courante — l'admission de la file
        # (`readiness`) est la vraie garde, ses lectures de base sont simulées.
        self.lot = self.LOT
        self.lot_branch = self.BRANCH
        self.lot_target = "main"
        self.lot_marks = []
        self.ready_marks()
        for patch in (
                mock.patch.object(work, "get", side_effect=self._lot_row),
                mock.patch.object(work, "milestones",
                                  side_effect=lambda db, n, *a, **kw: list(self.lot_marks))):
            patch.start()
            self.addCleanup(patch.stop)

    def _lot_row(self, db, lot, *a, **kw):
        return {"id": int(lot), "state": "build", "branch": self.lot_branch,
                "branch_target": self.lot_target, "title": "le lot", "app": "ameesh"}

    def ready_marks(self, head=None, *, verdict="ok", sha=None):
        """Le lot gelé sur `head` et son verdict ; `sha` déplace le verdict."""
        head = head or self.head
        self.lot_marks = [
            {"id": 1, "kind": "frozen", "sha": head, "at_ts": 1.0},
            {"id": 2, "kind": "verdict", "verdict": verdict, "sha": sha or head,
             "at_ts": 2.0},
        ]

    def tip(self, ref="main"):
        return _git(self.origin, "rev-parse", ref).lower()

    def gh(self, later=None, fresh=None, **over):
        over.setdefault("headRefOid", self.head)
        return FakeGh(view(**over), fresh=fresh, later=later)

    def merge(self, gh, slug=None, **kw):
        kw.setdefault("target", "main")
        kw.setdefault("fetch_url", self.origin)
        kw.setdefault("push_url", self.origin)
        kw.setdefault("attempts", 2)
        kw.setdefault("poll_s", 0)
        kw.setdefault("sleep", lambda s: None)
        kw.setdefault("lot", self.lot)
        kw.setdefault("repo", self.repo)
        kw.setdefault("db", None)
        kw.setdefault("cfg", None)
        return merge_pr.direct_merge(slug or self.slug, 7, self.head, gh=gh, **kw)

    def move_main(self):
        """Un commit de plus sur `main` dans l'origin, hors du gel."""
        mover = os.path.join(self.tmp, "mover")
        if not os.path.isdir(mover):
            subprocess.run(["git", "clone", "-q", self.origin, mover], check=True,
                           capture_output=True)
        _write(mover, "README.md", "moved %f\n" % time.time())
        _git(mover, "add", "-A")
        _git(mover, "commit", "-q", "-m", "moved")
        _git(mover, "push", "-q", "origin", "main")
        return _git(self.origin, "rev-parse", "main").lower()


class GuardTest(_Base):
    def test_dry_run_builds_the_merge_commit_without_pushing(self):
        gh = self.gh()
        out = self.merge(gh, dry_run=True)
        self.assertEqual(out["state"], "dry-run", out)
        self.assertEqual(self.tip(), self.base)          # rien n'a bougé
        self.assertEqual(out["tree"], out["frozen_tree"])
        self.assertEqual(out["workflow_files"], [".github/workflows/ci.yml"])
        self.assertIn("ameesh-work: %d" % self.lot, out["message"])
        # la vue initiale puis la garde fraîche, aucune relecture d'après push
        self.assertEqual([c[:2] for c in gh.calls], [["pr", "view"], ["pr", "view"]])

    def test_head_moved_is_refused_before_anything(self):
        gh = self.gh(headRefOid="a" * 40)
        with self.assertRaises(merge_pr.MergePrError) as caught:
            self.merge(gh)
        self.assertIn("head moved", str(caught.exception))
        self.assertEqual(self.tip(), self.base)

    def test_a_pr_without_workflow_is_refused(self):
        # une tête qui ne touche pas .github/workflows/** : une branche montée
        # depuis main, déclarée comme branche du lot (gelée et relue)
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "checkout", "-q", "-b", "lot/7-code")
        _write(self.repo, "src/app.py", "print('code only')\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "code only")
        head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "push", "-q", "origin", "lot/7-code")
        _git(self.origin, "update-ref", "refs/pull/7/head", head)
        self.head = head
        self.lot_branch = "lot/7-code"
        self.ready_marks(head)
        with self.assertRaises(merge_pr.MergePrError) as caught:
            self.merge(self.gh(headRefName="lot/7-code"))
        self.assertIn("does not touch .github/workflows/**", str(caught.exception))
        self.assertEqual(self.tip(), self.base)

    def test_a_blocked_pr_is_refused(self):
        with self.assertRaises(merge_pr.MergePrError) as caught:
            self.merge(self.gh(mergeStateStatus="BLOCKED"))
        self.assertIn("mergeStateStatus BLOCKED", str(caught.exception))

    def test_checks_not_green_are_refused(self):
        rollup = [{"__typename": "CheckRun", "name": "tests", "status": "IN_PROGRESS",
                   "conclusion": ""}]
        with self.assertRaises(merge_pr.MergePrError) as caught:
            self.merge(self.gh(statusCheckRollup=rollup))
        self.assertIn("not all concluded and green", str(caught.exception))

    def test_draft_is_refused(self):
        with self.assertRaises(merge_pr.MergePrError) as caught:
            self.merge(self.gh(isDraft=True))
        self.assertIn("draft", str(caught.exception))

    def test_target_moved_since_the_freeze_is_refused(self):
        moved = self.move_main()
        with self.assertRaises(merge_pr.MergePrError) as caught:
            self.merge(self.gh())
        self.assertIn("moved since the freeze", str(caught.exception))
        self.assertEqual(self.tip(), moved)   # le commit de plus est intact

    def test_a_target_that_moves_before_the_fresh_guard_is_refused(self):
        gh = self.gh()
        original = merge_pr.message
        moved = []

        def side_effect(*a, **kw):
            moved.append(self.move_main())
            return original(*a, **kw)

        with mock.patch.object(merge_pr, "message", side_effect=side_effect):
            with self.assertRaises(merge_pr.MergePrError) as caught:
                self.merge(gh)
        self.assertTrue(moved)
        self.assertIn("during the gesture", str(caught.exception))
        self.assertEqual(self.tip(), moved[-1])   # rien n'a été écrasé

    def test_a_target_that_moves_after_the_fresh_guard_is_refused_by_the_lease(self):
        gh = self.gh()
        original, moved = merge_pr._run, []

        def side_effect(cwd, *args, **kw):
            if args and args[0] == "push":
                moved.append(self.move_main())
            return original(cwd, *args, **kw)

        with mock.patch.object(merge_pr, "_run", side_effect=side_effect):
            with self.assertRaises(merge_pr.MergePrError) as caught:
                self.merge(gh)
        self.assertTrue(moved)
        self.assertIn("push refused", str(caught.exception))
        self.assertEqual(self.tip(), moved[-1])   # rien n'a été écrasé

    def test_the_push_url_must_not_be_the_token_path(self):
        for url in ("https://github.com/o/r.git", "http://github.com/o/r.git"):
            with self.subTest(url=url):
                with self.assertRaises(merge_pr.MergePrError) as caught:
                    merge_pr.check_push_url(url)
                self.assertIn("token", str(caught.exception))
        self.assertEqual(merge_pr.check_push_url("git@github.com:o/r.git"),
                         "git@github.com:o/r.git")
        self.assertEqual(merge_pr.check_push_url(self.origin), self.origin)

    def test_the_frozen_commit_must_be_a_full_sha(self):
        for bad in ("", "abc", self.head[:12], "z" * 40):
            with self.subTest(bad=bad):
                with self.assertRaises(merge_pr.MergePrError):
                    merge_pr.check_head(bad)
        self.assertEqual(merge_pr.check_head(self.head.upper()), self.head)

    def test_the_target_branch_is_validated(self):
        with self.assertRaises(merge_pr.MergePrError):
            self.merge(self.gh(), target="..bad")


class IdentityTest(_Base):
    """#1003 B/P1 (R1 delta) : une seule identité de dépôt pour la PR lue, le
    lot, l'admission, la lecture et le push. Le négatif A/B reprend le témoin
    de l'auditeur : deux dépôts qui partagent la base et le gel, B protégé
    (`protected=[main]`) et les remotes du geste sur B — A ne peut pas être
    admis par les conditions de B, ni l'inverse."""

    def _twin(self):
        """Un dépôt B avec les MÊMES commits (base et gel) que A."""
        twin = os.path.join(self.tmp, "twin.git")
        subprocess.run(["git", "clone", "-q", "--bare", self.repo, twin],
                       check=True, capture_output=True)
        _git(twin, "update-ref", "refs/pull/7/head", self.head)
        return twin

    def test_remotes_of_another_repository_are_refused(self):
        twin = self._twin()
        before = _git(twin, "rev-parse", "main").lower()
        with self.assertRaisesRegex(merge_pr.MergePrError, "not the same repository"):
            self.merge(self.gh(), fetch_url=twin, push_url=twin)
        self.assertEqual(before, _git(twin, "rev-parse", "main").lower())
        self.assertEqual(self.tip(), self.base)          # rien n'a bougé non plus

    def test_a_pr_of_another_repository_is_refused(self):
        _git(self.repo, "remote", "set-url", "origin", "https://github.com/a/repo.git")
        with self.assertRaisesRegex(merge_pr.MergePrError, "not the same repository"):
            self.merge(self.gh(), slug="b/repo",
                       fetch_url="https://github.com/a/repo.git",
                       push_url="git@github.com:a/repo.git")

    def test_a_checkout_without_a_readable_origin_is_refused(self):
        _git(self.repo, "remote", "remove", "origin")
        with self.assertRaisesRegex(merge_pr.MergePrError, "cannot verify"):
            self.merge(self.gh())

    def test_an_unverifiable_remote_is_refused(self):
        with self.assertRaisesRegex(merge_pr.MergePrError, "cannot verify"):
            self.merge(self.gh(), fetch_url="somewhere/else")

    def test_the_same_repository_under_another_form_is_accepted(self):
        out = self.merge(self.gh(), dry_run=True, fetch_url=self.origin + "/")
        self.assertEqual(out["state"], "dry-run", out)


class MergeTest(_Base):
    def test_merge_pushes_a_two_parent_commit_at_the_frozen_tree(self):
        gh = self.gh(later=lambda: view(
            state="MERGED", headRefOid=self.head,
            mergeCommit={"oid": self.tip()}))
        out = self.merge(gh)
        self.assertEqual(out["state"], "merged", out)
        tip = self.tip()
        self.assertEqual(tip, out["merge"])
        self.assertEqual(_git(self.origin, "rev-parse", "main^1").lower(), self.base)
        self.assertEqual(_git(self.origin, "rev-parse", "main^2").lower(), self.head)
        self.assertEqual(_git(self.origin, "rev-parse", "main^{tree}").lower(),
                         out["frozen_tree"])
        self.assertIn("#1003/REL-11",
                      _git(self.origin, "log", "-1", "--format=%B", "main"))
        self.assertTrue(all(c[:2] == ["pr", "view"] for c in gh.calls), gh.calls)

    def test_the_lot_trailer_names_the_lot(self):
        gh = self.gh()
        out = self.merge(gh, dry_run=True, lot=1003)
        self.assertIn("ameesh-work: 1003", out["message"])

    def test_a_read_back_that_stays_open_is_reported_pushed_unverified(self):
        gh = self.gh(later=lambda: view(headRefOid=self.head))
        out = self.merge(gh)
        self.assertEqual(out["state"], "pushed-unverified", out)
        self.assertTrue(out["pushed"])
        self.assertIn("check the PR and the branch by hand", out["why"])
        self.assertEqual(self.tip(), out["merge"])   # le push a bien eu lieu

    def test_the_read_back_must_name_the_pushed_commit(self):
        gh = self.gh(later=lambda: view(state="MERGED", headRefOid=self.head,
                                        mergeCommit={"oid": "b" * 40}))
        out = self.merge(gh)
        self.assertEqual(out["state"], "pushed-unverified", out)
        self.assertEqual(out["pr_merge"], "b" * 40)

    def test_an_already_merged_pr_is_verified_without_pushing(self):
        _git(self.origin, "update-ref", "refs/heads/main", self.head)
        gh = self.gh(state="MERGED", mergeCommit={"oid": self.head})
        out = self.merge(gh)
        self.assertEqual(out["state"], "already", out)
        self.assertTrue(out["content_verified"])
        self.assertEqual(self.tip(), self.head)
        self.assertEqual([c[:2] for c in gh.calls], [["pr", "view"]])

    def test_an_already_merged_pr_whose_head_is_not_in_the_target_is_unverified(self):
        gh = self.gh(state="MERGED", mergeCommit={"oid": "c" * 40})
        out = self.merge(gh)
        self.assertEqual(out["state"], "already-unverified", out)
        self.assertFalse(out["content_verified"])
        self.assertEqual(self.tip(), self.base)

    def test_a_closed_pr_is_refused(self):
        with self.assertRaises(merge_pr.MergePrError) as caught:
            self.merge(self.gh(state="CLOSED"))
        self.assertIn("not open", str(caught.exception))


class AdmissionGuardTest(_Base):
    """#1003 B1 : l'admission de la file (`readiness`) est rejouée telle quelle
    — lot gelé, verdict `ok` sur la tête, cible protégée, cible du lot,
    `ci_required` — et rien n'est écrit quand elle refuse."""

    def test_the_lot_is_required(self):
        with self.assertRaisesRegex(merge_pr.MergePrError, "lot is required"):
            self.merge(self.gh(), lot=None)

    def test_no_verdict_on_the_freeze_is_refused(self):
        self.lot_marks = [{"id": 1, "kind": "frozen", "sha": self.head, "at_ts": 1.0}]
        with self.assertRaisesRegex(merge_pr.MergePrError, "no review verdict ok"):
            self.merge(self.gh())
        self.assertEqual(self.tip(), self.base)

    def test_a_blocked_verdict_is_refused(self):
        self.ready_marks(verdict="blocked")
        with self.assertRaisesRegex(merge_pr.MergePrError, "no review verdict ok"):
            self.merge(self.gh())
        self.assertEqual(self.tip(), self.base)

    def test_a_verdict_on_another_commit_is_refused(self):
        self.ready_marks(sha=self.base)
        with self.assertRaisesRegex(merge_pr.MergePrError, "no review verdict ok"):
            self.merge(self.gh())
        self.assertEqual(self.tip(), self.base)

    def test_a_branch_that_moved_after_the_freeze_is_refused(self):
        _write(self.repo, "src/more.py", "x\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "after freeze")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "push", "-q", "origin", self.lot_branch)
        _git(self.origin, "update-ref", "refs/pull/7/head", self.head)
        with self.assertRaisesRegex(merge_pr.MergePrError, "not at its freeze"):
            self.merge(self.gh())
        self.assertEqual(self.tip(), self.base)

    def test_a_protected_target_is_refused(self):
        project = radar.Project(name="p", repo=self.repo, github="o/r",
                                protected=("main",))
        with self.assertRaisesRegex(merge_pr.MergePrError, "protected target"):
            self.merge(self.gh(), project=project)
        self.assertEqual(self.tip(), self.base)

    def test_a_target_the_lot_is_not_queued_for_is_refused(self):
        self.lot_target = "develop"
        with self.assertRaisesRegex(merge_pr.MergePrError, "queued for"):
            self.merge(self.gh())
        self.assertEqual(self.tip(), self.base)

    def test_ci_required_red_on_the_frozen_commit_is_refused(self):
        project = radar.Project(name="p", repo=self.repo, github="o/r", ci_required=True)
        red = {"runs": 1, "green": False, "pending": [], "failed": ["tests"]}
        with mock.patch.object(merge_pr.radar, "commit_ci", return_value=red):
            with self.assertRaisesRegex(merge_pr.MergePrError, "is red"):
                self.merge(self.gh(), project=project)
        self.assertEqual(self.tip(), self.base)


class FreshGuardTest(_Base):
    """#1003 B2 : la garde fraîche juste avant l'écriture — toute mutation
    depuis la construction du commit de fusion arrête sans pousser."""

    def test_a_head_that_moves_after_the_merge_commit_is_refused_before_the_push(self):
        gh = self.gh(fresh=view(headRefOid="b" * 40))
        with self.assertRaisesRegex(merge_pr.MergePrError, "head moved"):
            self.merge(gh)
        self.assertEqual(self.tip(), self.base)

    def test_a_check_that_turns_red_is_refused_before_the_push(self):
        fresh = view(headRefOid=self.head, statusCheckRollup=[
            {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED",
             "conclusion": "FAILURE"}])
        with self.assertRaisesRegex(merge_pr.MergePrError, "not all concluded and green"):
            self.merge(self.gh(fresh=fresh))
        self.assertEqual(self.tip(), self.base)

    def test_a_pr_closed_after_the_merge_commit_is_refused_before_the_push(self):
        with self.assertRaisesRegex(merge_pr.MergePrError, "not open"):
            self.merge(self.gh(fresh=view(state="CLOSED")))
        self.assertEqual(self.tip(), self.base)

    def test_a_verdict_withdrawn_after_the_merge_commit_is_refused_before_the_push(self):
        gh = self.gh()
        original = merge_pr.message

        def side_effect(*a, **kw):
            self.ready_marks(verdict="blocked")
            return original(*a, **kw)

        with mock.patch.object(merge_pr, "message", side_effect=side_effect):
            with self.assertRaisesRegex(merge_pr.MergePrError, "no review verdict ok"):
                self.merge(gh)
        self.assertEqual(self.tip(), self.base)


class RunTest(unittest.TestCase):
    """La ligne de commande : constat, enregistrement du lot, codes de sortie."""

    def args(self, **over):
        import argparse
        data = dict(repo="o/r", fetch_url="", push_url="git@github.com:o/r.git",
                    target=None, pr=7, head="a" * 40, dry_run=False, lot=1003, json=True,
                    actor="impl-03")
        data.update(over)
        return argparse.Namespace(**data)

    def run_with(self, out, args=None, **patches):
        import contextlib
        import io
        args = args or self.args()
        buf = io.StringIO()
        with mock.patch.object(merge_pr, "direct_merge", return_value=dict(out)), \
             mock.patch.object(merge_pr.plan_git, "_out", return_value=""), \
             mock.patch.object(merge_pr.migrations, "origin_repo", return_value="o/r"), \
             contextlib.redirect_stdout(buf):
            code = merge_pr.run(None, None, args)
        return code, buf.getvalue()

    def test_a_verified_merge_is_declared_on_the_lot(self):
        out = {"state": "merged", "pr": 7, "merge": "b" * 40, "frozen": "a" * 40,
               "tree": "c" * 40, "frozen_tree": "c" * 40, "target": "main",
               "tip": "d" * 40}
        with mock.patch.object(merge_pr.work, "merged",
                               return_value={"result": "merged"}) as recorded:
            code, text = self.run_with(out, args=self.args(lot=1003, json=False))
        self.assertEqual(code, 0)
        self.assertIn("declared merged", text)
        self.assertEqual(recorded.call_args.kwargs["sha"], "b" * 40)
        self.assertEqual(recorded.call_args.kwargs["actor"], "impl-03")

    def test_a_lot_that_cannot_be_declared_does_not_undo_the_merge(self):
        out = {"state": "merged", "pr": 7, "merge": "b" * 40, "frozen": "a" * 40,
               "tree": "c" * 40, "frozen_tree": "c" * 40, "target": "main",
               "tip": "d" * 40}
        patcher = mock.patch.object(merge_pr.work, "merged",
                                    side_effect=RuntimeError("lot closed"))
        patcher.start()
        self.addCleanup(patcher.stop)
        code, text = self.run_with(out, args=self.args(lot=1003, json=False))
        self.assertEqual(code, 0)
        self.assertIn("WARNING lot not declared", text)

    def test_exit_codes_name_the_uncertainty(self):
        for state, expected in (("pushed-unverified", 2), ("already-unverified", 1)):
            with self.subTest(state=state):
                code, _ = self.run_with({"state": state, "pr": 7, "merge": "b" * 40,
                                         "frozen": "a" * 40, "tip": "d" * 40,
                                         "target": "main", "why": "x"})
                self.assertEqual(code, expected)

    def test_a_refusal_exits_one(self):
        patcher = mock.patch.object(merge_pr, "direct_merge",
                                    side_effect=merge_pr.MergePrError("refusé"))
        patcher.start()
        self.addCleanup(patcher.stop)
        import contextlib
        import io
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = merge_pr.run(None, None, self.args())
        self.assertEqual(code, 1)
        self.assertIn("refusé", err.getvalue())

    def test_credentials_are_never_printed(self):
        self.assertEqual(merge_pr._redact("https://user:tok3n@github.com/o/r.git"),
                         "https://***@github.com/o/r.git")
        self.assertEqual(merge_pr._redact("git@github.com:o/r.git"),
                         "git@github.com:o/r.git")
