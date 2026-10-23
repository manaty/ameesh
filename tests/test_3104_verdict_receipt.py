# SPDX-License-Identifier: AGPL-3.0-only
"""#3104 — un verdict managé s'écrit avec son reçu, ou est refusé."""
from __future__ import annotations

import dataclasses
import io
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from ameesh import mesh_cli, review_provenance, storage, work

from .support import PgTestCase
from .test_2962_freeze_unresolved import git, repo_with_own_commit


class VerdictReceiptTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.repo, self.head = repo_with_own_commit(os.path.join(self.tmp, "nexlink"))
        self.cwd = os.path.join(self.tmp, "ameesh")
        os.makedirs(self.cwd)
        git(self.cwd, "init", "-q")
        git(self.cwd, "commit", "--allow-empty", "-qm", "cwd ameesh")
        self.assertIsNone(review_provenance.resolve_commit(self.head, cwd=self.cwd))
        self.actor = "reviewer"
        storage.of(self.db).agents.upsert_unleased(
            self.actor, chantier=None, harness="deepseek", host=self.cfg.host,
            cwd=self.cwd, session_id="review-session")
        self.lot = work.add(self.db, title="tête nexlink", app="nexlink")["id"]
        # La tête gelée suffit à reviewed_head, mais ne prouve pas que le
        # commit est lisible pour écrire le reçu de provenance.
        storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha=self.head, actor="author", verdict=None, note="")
        self.turn = {"AGENT_MAIL_NAME": self.actor, "AMEESH_TURN_ID": "t" * 32,
                     "AMEESH_HARNESS": "deepseek"}
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.cwd)

    def verdicts(self):
        return [m for m in work.milestones(self.db, self.lot) if m["kind"] == "verdict"]

    def verdict(self, sha=None, **kwargs):
        return work.milestone(self.db, self.lot, "verdict", sha=sha or self.head,
                              actor=self.actor, verdict="ok", **kwargs)

    def receipt(self, mark):
        receipt = review_provenance.receipt_for_milestone(self.db, mark["id"])
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["commit"], self.head)
        self.assertEqual(receipt["actor"], self.actor)
        self.assertEqual(receipt["session"], "review-session")
        self.assertEqual(receipt["turn"], self.turn["AMEESH_TURN_ID"])

    def test_unresolved_frozen_head_is_refused_without_a_verdict(self):
        for sha in (self.head, self.head[:12]):
            for verdict in ("ok", "blocked"):
                with self.subTest(sha=sha, verdict=verdict), \
                        mock.patch.dict(os.environ, self.turn):
                    with self.assertRaises(work.WorkError) as caught:
                        work.milestone(self.db, self.lot, "verdict", sha=sha,
                                       actor=self.actor, verdict=verdict)
                    self.assertIn("provenance-missing", str(caught.exception))
                    self.assertIn("does not resolve", str(caught.exception))
                    self.assertIn(sha, str(caught.exception))
                    self.assertIn("--repo", str(caught.exception))
        self.assertEqual(self.verdicts(), [])

    def cli_verdict(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env(**self.turn)), \
                mock.patch("ameesh.config.load", return_value=self.cfg), \
                redirect_stdout(out), redirect_stderr(err):
            code = mesh_cli.main(["work", "milestone", str(self.lot), "verdict",
                                  "ok", "--sha", self.head, *extra])
        return code, out.getvalue(), err.getvalue()

    def test_cli_returns_failure_and_names_the_repository_option(self):
        code, out, err = self.cli_verdict()
        self.assertEqual(code, 1, out + err)
        self.assertIn("provenance-missing", err)
        self.assertIn(self.head, err)
        self.assertIn("--repo", err)
        self.assertNotIn("milestone verdict added", out)
        self.assertEqual(self.verdicts(), [])

    def test_cli_repository_option_repairs_the_unresolved_head(self):
        code, out, err = self.cli_verdict("--repo", self.repo)
        self.assertEqual(code, 0, out + err)
        self.receipt(self.verdicts()[0])

    def test_project_repository_supplies_receipts_from_another_project_cwd(self):
        cfg = dataclasses.replace(self.cfg, radar={"projects": {
            "nexlink": {"repo": self.repo}}})
        with mock.patch.object(self.db, "cfg", cfg), \
                mock.patch.dict(os.environ, self.turn):
            for sha in (self.head, self.head[:12]):
                with self.subTest(sha=sha):
                    self.receipt(self.verdict(sha))

    def test_explicit_repository_supplies_the_receipt(self):
        with mock.patch.dict(os.environ, self.turn):
            self.receipt(self.verdict(repo=self.repo))

    def test_missing_live_session_is_refused_with_its_cause(self):
        self.db.execute("UPDATE agent_registry SET session_id = NULL WHERE name = %s",
                        (self.actor,))
        with mock.patch.dict(os.environ, self.turn):
            with self.assertRaises(work.WorkError) as caught:
                self.verdict(repo=self.repo)
        self.assertIn("provenance-missing", str(caught.exception))
        self.assertIn("no live session", str(caught.exception))
        self.assertEqual(self.verdicts(), [])

    def test_outside_a_managed_turn_the_frozen_head_remains_accepted(self):
        with mock.patch.dict(os.environ, {"AMEESH_TURN_ID": ""}):
            mark = self.verdict()
        self.assertIsNone(review_provenance.receipt_for_milestone(self.db, mark["id"]))
