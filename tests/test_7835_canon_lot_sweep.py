# SPDX-License-Identifier: AGPL-3.0-only
"""#7835 — le relevé des branches ferme un lot du canon sans PR enregistrée.

Constat (#7830, manaty/home#310) : la PR du lot, fusionnée à 03:20Z, n'était
pas enregistrée (`pr_ref` vide) et son issue miroir vit dans manaty/ameesh. Le
relevé lisait donc le lot dans le clone ameesh de l'assigné, qui ne porte ni
la branche `canon/…` ni le gel : le lot est resté en `qa_merge` jusqu'à un
fetch manuel d'un clone du canon suivi d'un `sync-merges`.

Témoins : le relevé lit le clone du canon configuré, le rafraîchit lui-même
(fetch) et ferme le lot ; sans clone du canon, la lecture d'avant reste.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import tempfile

from ameesh import plan_git, registry, work

from .support import PgTestCase


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


class CanonLotSweepTest(PgTestCase):

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, self.db, "cfg", getattr(self.db, "cfg", None))
        self.tmp = tempfile.mkdtemp(prefix="ameesh-7835-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # deux dépôts distincts : le projet (clone de l'assigné) et le canon
        self.project = self.clone("ameesh")
        self.home_origin = os.path.join(self.tmp, "manaty", "home.git")
        self.canon = self.clone("home")
        # le canon de l'hôte : un clone resté en retard, que personne ne fetch
        self.author = os.path.join(self.tmp, "author-home")
        _git(self.tmp, "clone", "-q", self.home_origin, self.author)
        registry.upsert(self.db, "auditeur", harness="claude", host=self.cfg.host,
                        cwd=self.project)

    def clone(self, name):
        origin = os.path.join(self.tmp, "manaty", name + ".git")
        os.makedirs(os.path.dirname(origin), exist_ok=True)
        _git(self.tmp, "init", "-q", "--bare", "--initial-branch=main", origin)
        path = os.path.join(self.tmp, name)
        _git(self.tmp, "clone", "-q", origin, path)
        self.write(path, "README.md", name + "\n")
        _git(path, "add", "-A")
        _git(path, "commit", "-qm", "base")
        _git(path, "push", "-q", "-u", "origin", "main")
        return path

    def write(self, repo, name, text):
        with open(os.path.join(repo, name), "w") as fh:
            fh.write(text)

    def canon_lot(self, branch):
        """La branche du canon naît chez l'auteur, est gelée puis fusionnée
        (`--no-ff`, comme une PR) dans `main` distante, sans enregistrer la PR."""
        _git(self.author, "checkout", "-q", "-b", branch, "origin/main")
        self.write(self.author, "federation.yaml", "sensitive: credential_gate\n")
        _git(self.author, "add", "-A")
        _git(self.author, "commit", "-qm", branch)
        frozen = _git(self.author, "rev-parse", "HEAD")
        _git(self.author, "push", "-q", "origin", branch)
        lot = work.add(self.db, title="lot du canon", app="ameesh", assignee="auditeur",
                       branch=branch, branch_target="main",
                       issue_ref="manaty/ameesh#6050")["id"]
        work.milestone(self.db, lot, "frozen", sha=frozen, actor="auditeur",
                       repo=self.author)
        _git(self.author, "checkout", "-q", "main")
        _git(self.author, "pull", "-q", "origin", "main")
        _git(self.author, "merge", "-q", "--no-ff", "-m", "Merge pull request #310 from "
             "manaty/" + branch, branch)
        _git(self.author, "push", "-q", "origin", "main")
        return lot, frozen

    def test_a_canon_lot_without_a_pr_is_closed_from_the_canon_clone(self):
        lot, frozen = self.canon_lot("canon/7830-courtier-sensible")
        self.db.cfg = dataclasses.replace(self.cfg, canon=self.canon)
        # le clone du canon de l'hôte ne porte pas encore la fusion
        self.assertNotEqual(_git(self.canon, "rev-parse", "origin/main"),
                            _git(self.author, "rev-parse", "main"))
        # le corps de la PR porteuse (#310) ne se déclare pas partiel
        report = plan_git.sync_branches(self.db, host=self.cfg.host,
                                        pr_body=lambda repo, number: "")
        entry = [e for e in report["results"] if e["work_item"] == lot][0]
        self.assertEqual(entry["result"], "merged", entry)
        self.assertEqual(entry["repo"], self.canon)
        self.assertIn("canon lot", entry["repo_note"])
        self.assertEqual(work.get(self.db, lot)["state"], "delivered")

    def test_without_a_canon_clone_the_project_clone_is_still_read(self):
        lot, _frozen = self.canon_lot("canon/7830-sans-canon")
        self.db.cfg = dataclasses.replace(self.cfg, canon="")
        report = plan_git.sync_branches(self.db, host=self.cfg.host,
                                        pr_body=lambda repo, number: "")
        entry = [e for e in report["results"] if e["work_item"] == lot][0]
        self.assertEqual(entry.get("repo"), self.project, entry)
        self.assertNotEqual(work.get(self.db, lot)["state"], "delivered")

    def test_a_project_lot_keeps_the_project_clone(self):
        row = {"id": 9, "app": "ameesh", "branch": "lot/9-x", "branch_target": "main",
               "issue_ref": "manaty/ameesh#9"}
        self.db.cfg = dataclasses.replace(self.cfg, canon=self.canon)
        local = plan_git._local_clones(self.db, self.cfg.host)
        self.assertEqual(plan_git._lot_clone(self.db, self.cfg.host, row, self.project,
                                             local), (self.project, ""))
