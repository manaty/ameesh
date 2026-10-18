# SPDX-License-Identifier: AGPL-3.0-only
"""#1400 : la déclaration de fusion reconnaît la branche ENREGISTRÉE du lot
dans un vrai commit de fusion (forme GitHub comprise, origine canonique) et
montre le constat (lot/app/titre + sujet du commit) avant toute écriture."""
from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tests import support  # noqa: F401 - isole la configuration de l'hôte avant work
from ameesh import mesh_cli, work


class DeclarationFusionTest(unittest.TestCase):
    """N1 : branche enregistrée reconnue sans échappatoire ; N2 : constat."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="declaration-1400-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", "git@github.com:acme/nexlink.git")
        self.item = {"id": 1400, "app": "nexlink", "title": "Déclaration de fusion",
                     "state": "build", "issue_ref": "acme/nexlink#12", "branch": None,
                     "branch_target": None}
        self.commit("Base")

    def git(self, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.invalid",
                   GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.invalid")
        proc = subprocess.run(["git", "-C", self.repo, "-c", "core.hooksPath=" + os.devnull,
                               *args], capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def commit(self, message):
        self.git("commit", "--allow-empty", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def merged_branch(self, branch, title, name="delivered.txt"):
        """Une VRAIE fusion à deux parents, titrée comme GitHub ou en local."""
        self.git("checkout", "-qb", branch)
        Path(self.repo, name).write_text("work\n")
        self.git("add", name)
        self.commit("Work")
        self.git("checkout", "-q", "main")
        self.git("merge", "--no-ff", branch, "-qm", title)
        return self.git("rev-parse", "HEAD")

    def declare(self, sha, **over):
        item = dict(self.item, **over)
        constats = []
        with mock.patch.object(work, "get", return_value=item), \
                mock.patch.object(work, "close_merged", return_value={
                    "result": "merged", "item": item}) as close:
            done = work.merged(object(), item["id"], sha=sha, repo=self.repo,
                               actor="dev3", announce=constats.append)
        return done, close.call_args.kwargs, constats

    def refused(self, sha, message, **over):
        item = dict(self.item, **over)
        constats = []
        with mock.patch.object(work, "get", return_value=item), \
                mock.patch.object(work, "close_merged") as close:
            with self.assertRaisesRegex(work.WorkError, message) as error:
                work.merged(object(), item["id"], sha=sha, repo=self.repo,
                            announce=constats.append)
            self.assertIn("nexlink: Déclaration de fusion", str(error.exception))
            self.assertEqual(constats, [])
            close.assert_not_called()

    # -- N1 : la branche enregistrée dans une vraie fusion -------------------

    def test_github_merge_of_the_recorded_branch_is_accepted(self):
        self.item["branch"] = "agent/cx3-declaration"
        sha = self.merged_branch(
            "agent/cx3-declaration",
            "Merge pull request #12 from acme/agent/cx3-declaration")
        _, args, constats = self.declare(sha[:9].upper())
        self.assertEqual(args["sha"], sha)
        self.assertTrue(args["declared"])
        self.assertEqual(constats[0]["how"], "recorded branch")

    def test_github_merge_from_a_foreign_fork_is_refused(self):
        # Même nom de branche, propriétaire étranger : la preuve d'origine
        # canonique manque, rien n'est écrit.
        self.item["branch"] = "agent/cx3-declaration"
        sha = self.merged_branch(
            "agent/cx3-declaration",
            "Merge pull request #12 from other/agent/cx3-declaration")
        self.refused(sha, "does not designate", branch="agent/cx3-declaration")

    def test_homonymous_recorded_branch_is_refused(self):
        self.item["branch"] = "agent/cx3-declaration"
        sha = self.merged_branch(
            "agent/cx3-declaration-2",
            "Merge pull request #12 from acme/agent/cx3-declaration-2")
        self.refused(sha, "does not designate", branch="agent/cx3-declaration")

    def test_ordinary_commit_carrying_the_merge_title_proves_nothing(self):
        # Un commit ordinaire (un seul parent) qui recopie le titre d'une
        # fusion GitHub n'est pas une fusion.
        self.item["branch"] = "agent/cx3-declaration"
        sha = self.commit("Merge pull request #12 from acme/agent/cx3-declaration")
        self.refused(sha, "does not designate", branch="agent/cx3-declaration")

    def test_remote_tracking_merge_of_the_recorded_branch_is_accepted(self):
        self.item["branch"] = "agent/cx3-declaration"
        sha = self.merged_branch(
            "agent/cx3-declaration",
            "Merge remote-tracking branch 'origin/agent/cx3-declaration'")
        _, args, _ = self.declare(sha)
        self.assertEqual(args["sha"], sha)

    def test_a_foreign_remote_does_not_prove_the_recorded_branch(self):
        self.item["branch"] = "agent/cx3-declaration"
        sha = self.merged_branch(
            "agent/cx3-declaration",
            "Merge remote-tracking branch 'upstream/agent/cx3-declaration'")
        self.refused(sha, "does not designate", branch="agent/cx3-declaration")

    def test_a_foreign_clone_is_refused(self):
        self.item["branch"] = "agent/cx3-declaration"
        sha = self.merged_branch(
            "agent/cx3-declaration",
            "Merge pull request #12 from acme/agent/cx3-declaration")
        self.git("remote", "set-url", "origin", "git@github.com:acme/ameesh.git")
        self.refused(sha, "not of acme/nexlink", branch="agent/cx3-declaration")

    def test_missing_sha_is_refused(self):
        self.refused("f" * 40, "not found")

    def test_a_branch_mention_in_a_review_body_proves_nothing(self):
        self.item["branch"] = "agent/cx3-declaration"
        sha = self.commit("Review\n\nRead agent/cx3-declaration")
        self.refused(sha, "does not designate", branch="agent/cx3-declaration")

    def test_the_declaration_has_no_force_escape_hatch(self):
        # « sans échappatoire force » : la ligne de commande ne connaît aucun
        # `--force` qui ferait passer une preuve refusée.
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                mesh_cli.build_parser().parse_args(
                    ["work", "merged", "1400", "--sha", "a" * 40, "--force", "x"])

    # -- N2 : le constat avant toute écriture --------------------------------

    def test_the_constat_is_shown_before_the_write(self):
        self.item["branch"] = "agent/cx3-declaration"
        sha = self.merged_branch(
            "agent/cx3-declaration",
            "Merge pull request #12 from acme/agent/cx3-declaration")
        events = []
        constats = []

        def announce(constat):
            events.append("constat")
            constats.append(constat)

        def write(*args, **kwargs):
            events.append("write")
            return {"result": "merged", "item": dict(self.item, state="merged")}

        with mock.patch.object(work, "get", return_value=self.item), \
                mock.patch.object(work, "close_merged", side_effect=write):
            done = work.merged(object(), 1400, sha=sha, repo=self.repo, actor="dev3",
                               announce=announce)
        self.assertEqual(events, ["constat", "write"])
        constat = constats[0]
        self.assertEqual((constat["id"], constat["app"], constat["title"]),
                         (1400, "nexlink", "Déclaration de fusion"))
        self.assertEqual(constat["sha"], sha)
        self.assertEqual(constat["subject"],
                         "Merge pull request #12 from acme/agent/cx3-declaration")
        self.assertEqual(constat["how"], "recorded branch")
        self.assertEqual(done["result"], "merged")

    def test_the_constat_names_a_trailer_merge_too(self):
        sha = self.merged_branch("agent/libre", "Deliver\n\nameesh-work: 1400")
        _, args, constats = self.declare(sha)
        self.assertEqual(args["sha"], sha)
        self.assertEqual(constats[0]["how"], "ameesh-work")

    def test_a_refused_evidence_neither_announces_nor_writes(self):
        self.item["branch"] = "agent/cx3-declaration"
        constats = []
        with mock.patch.object(work, "get", return_value=self.item), \
                mock.patch.object(work, "close_merged") as close:
            with self.assertRaises(work.WorkError):
                work.merged(object(), 1400, sha="f" * 40, repo=self.repo,
                            announce=constats.append)
        self.assertEqual(constats, [])
        close.assert_not_called()

    def test_a_write_refused_after_the_constat_leaves_the_lot_unchanged(self):
        # Le gel a changé entre la lecture et l'écriture : le constat a été
        # montré, l'écriture est refusée, l'état du lot ne bouge pas.
        self.item["branch"] = "agent/cx3-declaration"
        sha = self.merged_branch(
            "agent/cx3-declaration",
            "Merge pull request #12 from acme/agent/cx3-declaration")
        constats = []
        refrozen = {"result": "refrozen", "item": self.item,
                    "detail": "new freeze declared in the meantime: not closed, review again"}
        with mock.patch.object(work, "get", return_value=self.item), \
                mock.patch.object(work, "close_merged", return_value=refrozen):
            done = work.merged(object(), 1400, sha=sha, repo=self.repo, actor="dev3",
                               announce=constats.append)
        self.assertEqual(done["result"], "refrozen")
        self.assertEqual(done["item"]["state"], "build")
        self.assertEqual(len(constats), 1)


if __name__ == "__main__":
    unittest.main()
