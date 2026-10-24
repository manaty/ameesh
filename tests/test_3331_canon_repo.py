# SPDX-License-Identifier: AGPL-3.0-only
"""#3331 — un lot du canon se lit dans le clone du canon, pas dans celui du projet.

Constat (#2987, home#148) : le gel d8b4047e vit dans manaty/home. La
résolution du dépôt suivait l'`app` du lot (`radar.projects`), jamais le dépôt
de sa PR : report de verdict refusé (« no-repository »), relecture après fusion
en doute (« aucun dépôt candidat ne contient le commit »), et le relevé des
branches relisait un clone ameesh qui ne porte ni la branche `canon/…` ni son
gel.

Témoins : `repository` rend le clone du canon pour un lot `canon/…` (branche ou
PR `manaty/home#N`), le dépôt du projet sinon ; `class_at_merge` prouve le
classement dans le clone du canon ; le report de verdict passe sans `--repo` ;
sans clone du canon, un doute explicite (jamais le dépôt du projet) ; le relevé
des branches résout le clone du canon par la PR.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import lot_class, plan_git, registry, review_lot, verdict_carry, work

from .support import PgTestCase, patch_families

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
FAM = {"author": "deepseek", "rev1": "claude"}


def _git(root, *args) -> str:
    proc = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                          env=ENV)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _repo(path, origin="") -> str:
    os.makedirs(path, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    if origin:
        _git(path, "remote", "add", "origin", origin)
    return path


def _commit(root, name, text) -> str:
    with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
        fh.write(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", text)
    return _git(root, "rev-parse", "HEAD")


def _cfg(canon: str, state_dir: str, project: str = "") -> types.SimpleNamespace:
    radar = {"projects": {"ameesh": {"repo": project}}} if project else {}
    return types.SimpleNamespace(canon=canon, canon_ref="", canon_untrusted=False,
                                 extra_canons=(), state_dir=state_dir, radar=radar)


CANON_ITEM = {"id": 1, "app": "ameesh", "branch": "canon/2987-porte-prod-main",
              "branch_target": "main", "pr_ref": "manaty/home#148"}


class RepositoryTest(unittest.TestCase):
    """Le dépôt du lot suit la PR, puis l'app."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3331-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.canon = _repo(os.path.join(self.tmp, "canon"),
                           origin="https://github.com/manaty/home.git")
        self.project = _repo(os.path.join(self.tmp, "ameesh"),
                             origin="https://github.com/manaty/ameesh.git")
        self.cfg = _cfg(self.canon, self.tmp, project=self.project)

    def test_a_canon_branch_reads_the_canon_clone(self):
        self.assertEqual(lot_class.repository(self.cfg, dict(CANON_ITEM)), self.canon)

    def test_the_pr_repository_alone_names_the_canon_clone(self):
        item = dict(CANON_ITEM, branch="lot/2987-x")
        self.assertEqual(lot_class.repository(self.cfg, item), self.canon)

    def test_a_normal_lot_keeps_the_project_repository(self):
        item = {"app": "ameesh", "branch": "lot/9-x", "branch_target": "main",
                "pr_ref": "manaty/ameesh#9"}
        self.assertEqual(lot_class.repository(self.cfg, item), self.project)

    def test_without_a_canon_clone_a_canon_lot_is_a_doubt(self):
        cfg = _cfg("", self.tmp, project=self.project)
        self.assertIsNone(lot_class.repository(cfg, dict(CANON_ITEM)))

    def test_a_configured_canon_that_is_not_a_checkout_is_a_doubt(self):
        cfg = _cfg(os.path.join(self.tmp, "missing"), self.tmp, project=self.project)
        self.assertIsNone(lot_class.repository(cfg, dict(CANON_ITEM)))


class ClassAtMergeTest(unittest.TestCase):
    """La relecture après fusion prouve le classement dans le clone du canon."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3331-merge-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.canon = _repo(os.path.join(self.tmp, "canon"))
        self.frozen = _commit(self.canon, "a.txt", "base")
        self.merged = _commit(self.canon, "a.txt", "change")
        self.project = _repo(os.path.join(self.tmp, "ameesh"),
                             origin="https://github.com/manaty/ameesh.git")
        _commit(self.project, "b.txt", "another")
        self.agent = _repo(os.path.join(self.tmp, "agent"),
                           origin="https://github.com/manaty/ameesh.git")
        _commit(self.agent, "c.txt", "encore")

    def _class(self, cfg):
        db = types.SimpleNamespace(cfg=cfg)
        marks = [{"id": 1, "kind": "frozen", "sha": self.frozen, "at_ts": 1}]
        with mock.patch.object(review_lot, "_repo_of_agent", return_value=self.agent), \
                mock.patch.object(review_lot.work, "milestones", return_value=marks), \
                mock.patch.object(review_lot.lot_class, "of_lot") as of_lot:
            of_lot.return_value = {"lot": 1, "class": "light", "files": ["a.txt"]}
            out = review_lot.class_at_merge(
                db, 1, item=dict(CANON_ITEM), sha=self.merged, target="")
        return out, of_lot

    def test_the_canon_clone_proves_the_class(self):
        cfg = _cfg(self.canon, self.tmp, project=self.project)
        out, of_lot = self._class(cfg)
        self.assertFalse(out.get("doubt"), out)
        self.assertEqual(out.get("class"), "light")
        of_lot.assert_called_once()
        self.assertEqual(of_lot.call_args.kwargs.get("repo"), self.canon)

    def test_without_a_canon_clone_the_doubt_names_no_project_repo(self):
        cfg = _cfg("", self.tmp, project=self.project)
        out, of_lot = self._class(cfg)
        self.assertIs(out.get("doubt"), True)
        self.assertFalse(of_lot.called, out)
        why = out.get("why") or ""
        self.assertIn(self.agent, why)
        self.assertIn("ne contient pas le commit", why)
        self.assertNotIn(self.project, why)


class BranchSweepResolvesCanonTest(unittest.TestCase):
    """Le relevé des branches lit le clone du canon, pas celui de l'assigné."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3331-sweep-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.canon = _repo(os.path.join(self.tmp, "canon"),
                           origin="https://github.com/manaty/home.git")
        self.project = _repo(os.path.join(self.tmp, "ameesh"),
                             origin="https://github.com/manaty/ameesh.git")
        self.cfg = _cfg(self.canon, self.tmp, project=self.project)

    def test_the_pr_repository_wins_over_the_issue_repository(self):
        row = {"id": 2987, "app": "ameesh", "issue_ref": "manaty/ameesh#2288",
               "pr_ref": "manaty/home#148", "branch": "canon/2987-x"}
        self.assertEqual(plan_git._expected_repo(row), ("manaty/home", "home"))

    def test_the_issue_still_names_the_repository_without_a_pr(self):
        row = {"id": 9, "app": "ameesh", "issue_ref": "manaty/ameesh#2288"}
        self.assertEqual(plan_git._expected_repo(row), ("manaty/ameesh", "ameesh"))

    def test_the_canon_clone_is_a_local_candidate_and_reads_the_lot(self):
        row = {"id": 2987, "app": "ameesh", "issue_ref": "manaty/ameesh#2288",
               "pr_ref": "manaty/home#148", "branch": "canon/2987-x"}
        db = types.SimpleNamespace(cfg=self.cfg)
        with mock.patch.object(registry, "overview", return_value=[]):
            local = plan_git._local_clones(db, None)
        self.assertIn((self.canon, "manaty/home", {}), local)
        chosen, note = plan_git._lot_clone(db, None, row, self.project, local)
        self.assertEqual(chosen, self.canon)
        self.assertIn("read in %s" % self.canon, note)


class CarryCanonTest(PgTestCase):
    """Le report de verdict d'un lot du canon, sans `--repo` (témoin #3331)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        for name, harness in (("author", "deepseek"), ("rev1", "claude")):
            registry.upsert(cls.db, name, harness=harness, host="pc")

    def setUp(self) -> None:
        patch_families(self)
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3331-carry-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.canon = _repo(os.path.join(self.tmp, "canon"))
        # B0 ; la branche relue (R = B0 + lot) ; la cible avance (T1) ; le gel
        # est le rebasage de R sur T1 (même delta, historique mécanique).
        b0 = _commit(self.canon, "shared.txt", "one\n")
        _git(self.canon, "switch", "-q", "-c", "canon/2987-porte-prod-main")
        self.reviewed = _commit(self.canon, "lot.txt", "lot\n")
        _git(self.canon, "switch", "-q", "main")
        t1 = _commit(self.canon, "other.txt", "other\n")
        _git(self.canon, "switch", "-q", "-c", "rebased", t1)
        _git(self.canon, "cherry-pick", self.reviewed)
        self.frozen = _git(self.canon, "rev-parse", "HEAD")
        _git(self.canon, "switch", "-q", "main")
        self.lot = work.add(self.db, title="un lot du canon", app="ameesh",
                            branch="canon/2987-porte-prod-main", branch_target="main",
                            assignee="author")["id"]
        self.db.execute("UPDATE work_items SET pr_ref=%s WHERE id=%s",
                        ("manaty/home#148", self.lot))
        self.verdict("rev1", self.reviewed)
        work.milestone(self.db, self.lot, "frozen", sha=self.frozen, actor="author",
                       repo=self.canon)
        self.cfg = dataclasses.replace(self.cfg, canon=self.canon)

    def verdict(self, actor, sha, verdict="ok"):
        mark = work.milestone(self.db, self.lot, "verdict", sha=sha, actor=actor,
                              verdict=verdict, repo=self.canon)
        full = _git(self.canon, "rev-parse", sha)
        harness = FAM[actor]
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("milestone:%d" % int(mark["id"]), full, actor, "s-" + harness,
             "t", harness, self.lot))

    def on(self, sha):
        return [m for m in work.milestones(self.db, self.lot)
                if m["kind"] == "verdict" and m["sha"] == sha]

    def test_the_carry_reads_the_canon_clone_without_an_explicit_repo(self):
        result = verdict_carry.carry(self.cfg, self.db, self.lot)
        self.assertEqual(result["result"], "carried", result)
        self.assertEqual([m["actor"] for m in self.on(self.frozen)], ["rev1"])

    def test_without_a_canon_clone_the_carry_is_an_explicit_doubt(self):
        cfg = dataclasses.replace(self.cfg, canon="")
        result = verdict_carry.carry(cfg, self.db, self.lot)
        self.assertEqual(result["result"], "no-repository", result)
        self.assertEqual(self.on(self.frozen), [])
