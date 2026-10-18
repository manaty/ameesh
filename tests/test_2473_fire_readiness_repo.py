# SPDX-License-Identifier: AGPL-3.0-only
"""#2473 : le feu lit la branche du lot dans le dépôt du lot, pas dans le
dossier enregistré de l'assigné.

Constat du 18/10 (paquets Nexlink 68 à 71) : la branche
`integration/paquet-NN-nexlink` est poussée sur `origin` (ls-remote OK), une
copie locale existe, et pourtant `ameesh work fire` répond « branch … is
neither local nor on origin ». Cause : le passage du feu prend le `cwd`
enregistré de l'assigné (`orchestrateur` → `/home/smichea/development/manaty`,
le **parent** des clones) ; `fetch_fresh` n'y trouve pas d'`origin`, ne fait
donc aucun `ls-remote`/`fetch` et ne lit que `refs/heads/<branche>` de ce
dossier — qui n'est pas un dépôt. Ni la copie locale de la branche ni le
résultat d'un `--repo` explicite n'entrent dans cette lecture. Le retrait
d'un worktree n'y change rien : le dossier lu n'est pas celui du worktree.

Le témoin monte un vrai `origin`, pousse la branche depuis un worktree, puis
retire le worktree et efface la branche locale et sa copie
`refs/remotes/origin/*`. La lecture de préparation du feu doit retrouver la
branche par un `ls-remote` + `fetch` frais, sans aucun geste manuel.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import config, fire, merge_queue, plan_git, radar, storage, work

from .test_l160b_radar import _git, _write, git_env
from .test_l160e_merge_queue import FakeQueue

NEXLINK = {"nexlink": {"repo": "", "github": "manaty/nexlink", "land": "local",
                       "gates": ["true"]}}
BRANCH = "integration/paquet-68-nexlink"
TARGET = "develop"


class _RepoCase(unittest.TestCase):
    """Un `origin` réel, un clone, et la branche du paquet poussée depuis un
    worktree retiré ensuite."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2473-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = os.path.join(self.tmp, "origin.git")
        self.repo = os.path.join(self.tmp, "nexlink")
        _git(self.tmp, "init", "-q", "--bare", self.origin)
        _git(self.tmp, "clone", "-q", self.origin, self.repo)
        _write(self.repo, "base.txt", "base\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        _git(self.repo, "branch", "-m", "main")
        _git(self.repo, "push", "-q", "origin", "main")
        _git(self.repo, "checkout", "-q", "-b", TARGET, "main")
        _write(self.repo, "target.txt", "target\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "target")
        _git(self.repo, "push", "-q", "origin", TARGET)
        _git(self.repo, "checkout", "-q", "-b", BRANCH, TARGET)
        _write(self.repo, "packet.txt", "packet 68\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "paquet 68")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "push", "-q", "origin", BRANCH)
        # La branche n'était portée que par un worktree, retiré ensuite ; sa
        # copie locale et sa copie `origin/<branche>` disparaissent : seule
        # `origin` la garde.
        _git(self.repo, "checkout", "-q", TARGET)
        worktree = os.path.join(self.tmp, "paquet-68")
        _git(self.repo, "worktree", "add", "-q", worktree, BRANCH)
        _git(self.repo, "worktree", "remove", worktree)
        _git(self.repo, "branch", "-D", BRANCH)
        _git(self.repo, "update-ref", "-d", "refs/remotes/origin/" + BRANCH)
        self.assertFalse(plan_git._commit(self.repo, "refs/heads/" + BRANCH))
        self.assertFalse(plan_git._commit(self.repo, "refs/remotes/origin/" + BRANCH))
        self.assertIn(self.head, _git(self.repo, "ls-remote", "origin",
                                      "refs/heads/" + BRANCH))

    def config(self, repo=None):
        return dataclasses.replace(
            config.Config(), state_dir=os.path.join(self.tmp, "state"),
            radar={"projects": {"nexlink": dict(NEXLINK["nexlink"],
                                                repo=repo or self.repo)}})


class RepoChoiceTest(_RepoCase):
    def test_a_parent_of_clones_is_not_a_checkout(self):
        """Le `cwd` de l'assigné peut exister sans être un dépôt : il ne doit
        pas être rendu comme dépôt du lot."""
        self.assertIsNone(fire._checkout(self.tmp))
        self.assertEqual(fire._checkout(self.repo), os.path.realpath(self.repo))
        with mock.patch("ameesh.registry.get", return_value={"cwd": self.tmp}):
            self.assertIsNone(fire._repo_of(None, {"assignee": "orchestrateur"}))
        with mock.patch("ameesh.registry.get", return_value={"cwd": self.repo}):
            self.assertEqual(fire._repo_of(None, {"assignee": "orchestrateur"}),
                             os.path.realpath(self.repo))

    def test_a_checkout_of_another_repository_is_discarded(self):
        with mock.patch("ameesh.migrations.origin_repo",
                        return_value="manaty/nexlink"):
            self.assertTrue(fire._same_repository(self.repo, "manaty/nexlink"))
        with mock.patch("ameesh.migrations.origin_repo",
                        return_value="manaty/ameesh"):
            self.assertFalse(fire._same_repository(self.repo, "manaty/nexlink"))


class ReadinessWitnessTest(_RepoCase):
    """La préparation du feu retrouve la branche poussée, sans geste manuel."""

    def _act(self, cfg, cwd):
        lot = 42
        row = {"id": lot, "title": "paquet 68", "branch": BRANCH,
               "branch_target": TARGET, "assignee": "orchestrateur",
               "app": "nexlink", "issue_ref": "manaty/nexlink#1617", "state": "qa"}
        fake = FakeQueue([row])
        marks = {"frozen": [{"id": 1, "kind": "frozen", "sha": self.head,
                             "at_ts": 1.0},
                            {"id": 2, "kind": "fire", "sha": self.head,
                             "at_ts": 2.0, "actor": "orchestrateur"}]}
        store = mock.Mock(work=fake)
        green = {"number": 7, "green": True, "head": self.head, "draft": False}
        patches = [
            mock.patch.object(storage, "of", return_value=store),
            mock.patch.object(work, "get", side_effect=lambda db, n: fake.lots.get(n)),
            mock.patch.object(work, "milestones",
                              side_effect=lambda db, n, *a: marks["frozen"]),
            mock.patch("ameesh.registry.get", return_value={"cwd": cwd}),
            mock.patch.object(fire, "_pr_status", return_value=None),
            mock.patch.object(fire, "_repo_info", return_value={}),
            mock.patch.object(fire, "_target", return_value=TARGET),
            mock.patch.object(fire, "ready_verdicts",
                              return_value=(True, "", {"class": "sensitive"})),
            mock.patch.object(fire, "_passive"),
            mock.patch.object(merge_queue, "_class_of",
                              return_value={"class": "sensitive", "why": "test"}),
            mock.patch.object(merge_queue, "review_gate",
                              return_value={"ok": True, "why": "", "required": 0,
                                            "waived": False}),
            mock.patch.object(radar, "github_ci",
                              return_value=lambda branch: dict(green)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        out = fire._act(cfg, None, row, marks["frozen"], gh=lambda *a, **k: None,
                        dry_run=False, repo=None, cache={})
        return out, fake

    def test_the_branch_pushed_from_a_removed_worktree_is_queued(self):
        cfg = self.config()
        out, fake = self._act(cfg, self.tmp)
        self.assertEqual(out["result"], "queued", out)
        self.assertEqual(out["repo"], os.path.realpath(self.repo))
        self.assertEqual(fake.queue_rows[42]["frozen_sha"], self.head)
        # La preuve du moment de fetch : la tête gelée est arrivée dans le
        # clone du radar sous QUEUE_REF, par ls-remote + fetch, alors qu'aucune
        # ref locale du dépôt des agents ne la portait.
        clone = radar.clone_dir(cfg, "nexlink")
        self.assertEqual(plan_git._commit(clone, merge_queue.QUEUE_REF + BRANCH),
                         self.head)

    def test_the_declared_project_checkout_is_used_when_the_cwd_is_a_parent(self):
        """Le parent de clones ne porte pas la branche : le clone déclaré du
        projet (`radar.projects.nexlink.repo`) est le dépôt lu."""
        cfg = self.config()
        out, _ = self._act(cfg, self.tmp)
        self.assertEqual(out["repo"], os.path.realpath(self.repo))
        self.assertNotEqual(out["repo"], os.path.realpath(self.tmp))


if __name__ == "__main__":
    unittest.main()
