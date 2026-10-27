# SPDX-License-Identifier: AGPL-3.0-only
"""Qualification du pont d'approbation sur les vrais enregistrements IMA #704.

Ce test ne tourne que sur bureau-goal, apres le geste #4005 (jeton GitHub
existant consigne au lot, nom et portees seulement). Il est **saute** sans la
fixture, conformement au lot #4006 : le code et les tests sur fixtures ne
dependent pas de #4005, la qualification reelle si.

Capturer la fixture depuis bureau-goal (lecture seule, jamais dans Git) ::

    gh api repos/thepackengers/issue-manager-agent/issues/704 > /tmp/ima-704-issue.json
    gh api 'repos/thepackengers/issue-manager-agent/issues/704/comments?per_page=100' \
        > /tmp/ima-704-comments.json
    python3 - <<'PY'
    import json
    issue = json.load(open('/tmp/ima-704-issue.json'))
    comments = json.load(open('/tmp/ima-704-comments.json'))
    json.dump({'issue': issue, 'comments': comments},
              open('/tmp/ima-704-fixture.json', 'w'))
    PY
    AMEESH_APPROVALS_704_FIXTURE=/tmp/ima-704-fixture.json \
        python3 -m unittest tests.test_process_approvals_704 -v

Le fichier de fixture contient les commentaires reels : il reste dans le
coffre du bureau, jamais dans le depot.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import unittest

from ameesh import process_approvals as pa

UTC = dt.timezone.utc
#: Horloge fixe : la qualification lit des enregistrements datés, elle ne
#: dépend pas du jour où la fixture a été capturée.
QUALIFICATION_NOW = dt.datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)
REPO = "thepackengers/issue-manager-agent"
ISSUE = 704
FULL_A = "d918149d9d301f0ba7a2aff80e196e8e3b1e30ac"
FULL_B = "6c771e2e16bc04e8ce3b04a55df67ad4aca77cb8"
PATH_A = "knowledge/architecture/ima-ameesh-work-contract.md"
PATH_B = "knowledge/architecture/ima-ameesh-process-contract.md"
APPROVAL_A = 6206271894
APPROVAL_B = 6206927953


class Real704Qualification(unittest.TestCase):
    def setUp(self) -> None:
        path = os.environ.get("AMEESH_APPROVALS_704_FIXTURE")
        if not path:
            self.skipTest("set AMEESH_APPROVALS_704_FIXTURE on bureau-goal")
        with open(path, encoding="utf-8") as handle:
            fixture = json.load(handle)
        if not isinstance(fixture, dict) or not isinstance(
                fixture.get("comments"), list):
            self.fail("fixture: {'issue': ..., 'comments': [...]} expected")
        self.issue = fixture.get("issue") or {}
        self.comments = fixture["comments"]
        self.source = pa.Source(
            repo=REPO, issue=ISSUE, approvers=("smichea",),
            token_file=os.environ.get("AMEESH_APPROVALS_704_TOKEN_FILE",
                                      "/nonexistent/read-only-qualification"),
            projection="/nonexistent/read-only-projection.json")
        self.projection = pa.build_projection(
            self.source, self.issue, self.comments,
            now=QUALIFICATION_NOW)

    def _record(self, comment_id: int) -> pa.Approval:
        matches = [item for item in self.projection.approvals
                   if item.comment_id == comment_id]
        self.assertEqual(len(matches), 1,
                         "approval #%d not normalised; refusals: %r"
                         % (comment_id, self.projection.refusals))
        return matches[0]

    def test_the_two_owner_approvals_are_exact_and_current(self):
        first = self._record(APPROVAL_A)
        self.assertEqual(first.artifact.repo, REPO)
        self.assertEqual(first.artifact.revision, FULL_A)
        self.assertEqual(first.artifact.path, PATH_A)
        self.assertEqual(first.state, "current")
        self.assertEqual(first.author, "smichea")
        second = self._record(APPROVAL_B)
        self.assertEqual(second.artifact.repo, REPO)
        self.assertEqual(second.artifact.revision, FULL_B)
        self.assertEqual(second.artifact.path, PATH_B)
        self.assertEqual(second.state, "current")
        self.assertEqual(second.author, "smichea")
        for record in (first, second):
            got = pa.require_approval(
                self.projection, repo=record.artifact.repo,
                revision=record.artifact.revision,
                path=record.artifact.path,
                now=QUALIFICATION_NOW)
            self.assertEqual(got.id, record.id)

    def test_bot_status_comments_are_not_approvals(self):
        for record in self.projection.approvals:
            self.assertEqual(record.author, "smichea")
            self.assertIsInstance(record.comment_id, int)

    def test_the_projection_has_no_secret(self):
        token = os.environ.get("AMEESH_APPROVALS_704_TOKEN")
        if not token:
            self.skipTest("no AMEESH_APPROVALS_704_TOKEN to look for")
        raw = json.dumps(self.projection.as_dict())
        self.assertNotIn(token, raw)


if __name__ == "__main__":
    unittest.main()
