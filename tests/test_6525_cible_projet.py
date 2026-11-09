# SPDX-License-Identifier: AGPL-3.0-only
"""#6525 — la cible d'un lot vient du projet, jamais d'`origin/HEAD`.

Un lot nexlink dont la branche est posée sans `--target` retombait sur
`origin/main` du clone (la prod) alors que le projet intègre sur `develop`.
Le 09/11, dix lots ont été corrigés à la main ; le refus « do not share a
target » de `work resolve` (#203999) l'a révélé.

Attendu :
- une seule résolution : cible du lot, sinon cible du projet (configuration
  compilée `release.projects`, puis `radar.projects`, puis l'unique
  `github.projects.<p>.base`), sinon `ameesh.target` du clone ; `origin/HEAD`
  n'est jamais la cible d'un projet qui en déclare une ;
- `work add` et `work branch` héritent de la cible du projet, une cible
  explicite prime ;
- l'inférence au gel (`lot_class.default_base`) lit la même cible ;
- un lot existant dont la cible n'est que le défaut du dépôt est signalé,
  sans réécriture silencieuse.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import intake_neighbors, lot_class, plan_git, resolutions, storage, work


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _write(repo: str, path: str, text: str) -> None:
    full = os.path.join(repo, path)
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(text)


class RepoFixture(unittest.TestCase):
    """`origin/HEAD` vise `main` (le piège nexlink), `develop` existe."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-6525-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "nexlink")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.name", "banc")
        _git(self.repo, "config", "user.email", "banc@example.invalid")
        _write(self.repo, "base.txt", "base\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        _git(self.repo, "branch", "develop")
        _git(self.repo, "update-ref", "refs/remotes/origin/main", "main")
        _git(self.repo, "update-ref", "refs/remotes/origin/develop", "develop")
        _git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD",
             "refs/remotes/origin/main")

    def cfg(self, **sources):
        return types.SimpleNamespace(**sources)

    def project_cfg(self, *, release=True, radar=False, github=False):
        values = {}
        if release:
            values["release"] = {"projects": {"nexlink": {
                "repo": self.repo, "target": "develop", "github": "manaty/nexlink"}}}
        if radar:
            values["radar"] = {"projects": {"nexlink": {
                "repo": self.repo, "target": "develop",
                "github": "manaty/nexlink"}}}
        if github:
            values["github"] = {"projects": {"nexlink": {
                "repo": "manaty/nexlink", "base": ["develop"]}}}
        return types.SimpleNamespace(**values)


class ProjectTargetTest(RepoFixture):
    def test_release_configuration_is_the_compiled_project_target(self):
        self.assertEqual(intake_neighbors.project_target(self.project_cfg(), "nexlink"),
                         "develop")

    def test_radar_target_is_read_when_release_does_not_declare_it(self):
        cfg = self.project_cfg(release=False, radar=True)
        self.assertEqual(intake_neighbors.project_target(cfg, "nexlink"), "develop")

    def test_single_declared_base_is_the_project_target(self):
        cfg = self.project_cfg(release=False, github=True)
        self.assertEqual(intake_neighbors.project_target(cfg, "nexlink"), "develop")

    def test_ambiguous_base_is_not_guessed(self):
        cfg = types.SimpleNamespace(github={"projects": {"nexlink": {
            "repo": "manaty/nexlink", "base": ["main", "develop"]}}})
        self.assertEqual(intake_neighbors.project_target(cfg, "nexlink"), "")

    def test_no_declared_project_target(self):
        self.assertEqual(intake_neighbors.project_target(types.SimpleNamespace(), "nexlink"), "")


class DefaultBaseTest(RepoFixture):
    def item(self, **extra):
        row = {"id": 7, "app": "nexlink", "branch": "lot/7", "branch_target": ""}
        row.update(extra)
        return row

    def test_freeze_inference_gets_develop_not_origin_head_main(self):
        cfg = self.project_cfg(github=True)
        self.assertEqual(lot_class.default_base(cfg, self.item(), self.repo), "develop")
        self.assertEqual(plan_git.repository_default_target(self.repo), "origin/main")

    def test_project_without_target_keeps_the_clone_default(self):
        cfg = types.SimpleNamespace()
        self.assertEqual(lot_class.default_base(cfg, self.item(app="ameesh"), self.repo),
                         "origin/main")

    def test_explicit_lot_target_wins(self):
        cfg = self.project_cfg()
        self.assertEqual(lot_class.default_base(cfg, self.item(branch_target="main"),
                                                self.repo), "main")

    def test_declared_only_still_reads_the_project(self):
        cfg = self.project_cfg()
        self.assertEqual(lot_class.default_base(cfg, self.item(), self.repo,
                                                declared_only=True), "develop")


class WorkBranchDefaultTest(RepoFixture):
    def store(self, row):
        store = mock.Mock()
        store.work.set_branch.return_value = dict(row, branch_target="develop")
        return store

    def test_work_branch_inherits_the_project_target(self):
        cfg = self.project_cfg(github=True)
        db = types.SimpleNamespace(cfg=cfg)
        item = {"id": 7, "app": "nexlink", "branch": "lot/7", "branch_target": None,
                "state": "build"}
        store = self.store(item)
        with mock.patch.object(storage, "of", return_value=store), \
                mock.patch.object(work, "get", return_value=item):
            row = work.set_branch(db, 7, "lot/7-b")
        self.assertEqual(row["branch_target"], "develop")
        args, kwargs = store.work.set_branch.call_args
        self.assertEqual((args[0], args[1], args[2]), (7, "lot/7-b", "develop"))

    def test_existing_explicit_target_is_never_rewritten(self):
        cfg = self.project_cfg(github=True)
        db = types.SimpleNamespace(cfg=cfg)
        item = {"id": 7, "app": "nexlink", "branch": "lot/7", "branch_target": "main",
                "state": "build"}
        store = self.store(item)
        with mock.patch.object(storage, "of", return_value=store), \
                mock.patch.object(work, "get", return_value=item):
            row = work.set_branch(db, 7, "lot/7")
        self.assertEqual(row["branch_target"], "main")
        store.work.set_branch.assert_not_called()

    def test_project_without_target_keeps_the_previous_behaviour(self):
        db = types.SimpleNamespace(cfg=types.SimpleNamespace())
        item = {"id": 7, "app": "ameesh", "branch": "lot/7", "branch_target": None,
                "state": "build"}
        store = self.store(item)
        with mock.patch.object(storage, "of", return_value=store), \
                mock.patch.object(work, "get", return_value=item):
            work.set_branch(db, 7, "lot/7-b")
        self.assertIsNone(store.work.set_branch.call_args.args[2])


class WorkAddDefaultTest(RepoFixture):
    """`work add --branch` écrit la cible du projet, sans toucher au reste."""

    def _create(self, cfg, **kwargs):
        store = mock.Mock()
        store.work.add.return_value = {"id": 7, "branch": kwargs.get("branch"),
                                       "branch_target": kwargs.get("branch_target"),
                                       "type": "evolution", "state": "intake"}
        with mock.patch.object(storage, "of", return_value=store), \
                mock.patch.object(work, "_is_vehicle_branch", return_value=False), \
                mock.patch("ameesh.deep_analysis.on_lot_created", return_value=None), \
                mock.patch("ameesh.backlog.followup_hint", return_value=""):
            work._create(types.SimpleNamespace(), title="t", type="evolution",
                         app="nexlink", cfg=cfg, team_queue=False,
                         **kwargs)
        return store.work.add.call_args.kwargs

    def test_work_add_inherits_develop(self):
        cfg = self.project_cfg(release=True)
        written = self._create(cfg, branch="lot/7")
        self.assertEqual(written["branch_target"], "develop")

    def test_explicit_target_wins(self):
        cfg = self.project_cfg(release=True)
        written = self._create(cfg, branch="lot/7", branch_target="main")
        self.assertEqual(written["branch_target"], "main")

    def test_no_project_target_is_unchanged(self):
        written = self._create(types.SimpleNamespace(), branch="lot/7")
        self.assertIsNone(written["branch_target"])

    def test_no_branch_takes_no_default_target(self):
        cfg = self.project_cfg(release=True)
        written = self._create(cfg)
        self.assertIsNone(written["branch_target"])


class ResolutionPairTest(RepoFixture):
    def _branch(self, name, path):
        _git(self.repo, "switch", "-q", "-c", name, "main")
        _write(self.repo, path, name + "\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", name)
        _git(self.repo, "switch", "-q", "main")

    def test_two_lots_without_target_share_the_project_target(self):
        self._branch("lot/1", "one.txt")
        self._branch("lot/2", "two.txt")
        rows = {1: {"id": 1, "app": "nexlink", "branch": "lot/1", "branch_target": None},
                2: {"id": 2, "app": "nexlink", "branch": "lot/2", "branch_target": None}}
        cfg = self.project_cfg(github=True)
        db = types.SimpleNamespace(cfg=cfg)
        with mock.patch.object(work, "get", side_effect=lambda _db, n: rows.get(n)):
            out = resolutions.pair(db, 1, 2, repo=self.repo, fetch=False)
        self.assertEqual(out["target"], "develop")

    def test_explicit_repository_default_still_conflicts(self):
        self._branch("lot/1", "one.txt")
        self._branch("lot/2", "two.txt")
        rows = {1: {"id": 1, "app": "nexlink", "branch": "lot/1", "branch_target": "main"},
                2: {"id": 2, "app": "nexlink", "branch": "lot/2", "branch_target": None}}
        cfg = self.project_cfg(github=True)
        db = types.SimpleNamespace(cfg=cfg)
        with mock.patch.object(work, "get", side_effect=lambda _db, n: rows.get(n)):
            with self.assertRaises(resolutions.ResolveError) as ctx:
                resolutions.pair(db, 1, 2, repo=self.repo, fetch=False)
        message = str(ctx.exception)
        self.assertIn("do not share a target", message)
        # #6525 : le refus nomme la cible explicite qui n'est que le défaut du
        # dépôt et donne le geste correctif, au lieu de laisser corriger à la main.
        self.assertIn("#1", message)
        self.assertIn("--target develop", message)


class TargetWarningTest(RepoFixture):
    def test_an_existing_default_target_is_named_not_rewritten(self):
        cfg = self.project_cfg(github=True)
        item = {"id": 6455, "app": "nexlink", "branch": "review/6455-x",
                "branch_target": "main"}
        warning = lot_class.target_warning(cfg, item, self.repo)
        self.assertIn("#6455", warning)
        self.assertIn("develop", warning)
        self.assertIn("main", warning)
        self.assertIn("--target develop", warning)
        self.assertEqual(item["branch_target"], "main")

    def test_no_warning_when_the_explicit_target_is_the_project_target(self):
        cfg = self.project_cfg(github=True)
        item = {"id": 6455, "app": "nexlink", "branch": "review/6455-x",
                "branch_target": "develop"}
        self.assertEqual(lot_class.target_warning(cfg, item, self.repo), "")

    def test_no_warning_without_a_declared_project_target(self):
        item = {"id": 6455, "app": "ameesh", "branch": "review/6455-x",
                "branch_target": "main"}
        self.assertEqual(lot_class.target_warning(types.SimpleNamespace(), item,
                                                  self.repo), "")


if __name__ == "__main__":
    unittest.main()
