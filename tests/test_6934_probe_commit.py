# SPDX-License-Identifier: AGPL-3.0-only
"""#6934 — une sonde locale jamais poussée n'exclut pas son relecteur.

Cas réel nexlink #6912 (gel #39075) : mesh-design fait des commits de sonde
dans un worktree détaché, sur la tête gelée, sans jamais les pousser. Le hook
post-commit en écrit le reçu ; son tour est rattaché au lot. La forge refuse
l'objet, et `_rewritten_after_freeze` en faisait un doute #4679 : le
relecteur était exclu comme correcteur.

Témoins : sonde refusée par la forge, aucune poussée de l'acteur sur la
branche du lot → aucune exclusion, le gel la dit ; correctif rebasé avant son
push (#4679) dont la poussée est vue sur la branche → exclusion inchangée.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import (plan_git, review_authors, review_designation as rd,
                    review_designation_activation as act, work)

from . import test_4679_rebased_corrector as rebased

_REAL_FETCH = review_authors._forge_fetch


def _refusing(*shas):
    """`_forge_fetch` dont la forge refuse net les SHA donnés."""
    refused = {str(sha).lower() for sha in shas}

    def fetch(repo, sha, *, timeout=None):
        if str(sha).lower() in refused:
            return False
        return _REAL_FETCH(repo, sha, timeout=timeout)
    return fetch


class ProbeCommitTest(rebased.RebasedCorrectorDoubtTest):
    """Les témoins #4679 restent verts ; s'y ajoutent la sonde et sa garde."""

    def prefetched_freeze(self):
        """Le gel de `freeze_gate` : préchargement hors transaction, puis gel."""
        item = work.get(self.db, self.lot)
        target = act.prepare_target(self.cfg, self.db, item, self.frozen)
        act.prefetch_freeze_objects(self.db, item, target, self.frozen)
        return self.freeze()

    def _prepare_probe(self, *, forget=True):
        head_a = self.frozen
        self.freeze()                       # F1
        # La sonde de cx3 sur la tête gelée, jamais poussée.
        self.repo.write("README", "sonde\n")
        probe = self.repo.commit("p3", agent="cx3")
        self.commit_receipt("cx3", "codex", probe, turn="t")
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, session, recorded_at,"
            " work_item_id, turn_id)"
            " VALUES ('cx3', 'codex', 's-codex', now(), %s, 't')", (self.lot,))
        self.repo.git("reset", "--hard", head_a)
        if forget:
            self._forget(probe)
        self.db.execute(
            "UPDATE provenance_receipts SET at = now() - interval '10 minutes'"
            " WHERE subject = %s", ("commit:" + probe,))
        work.milestone(self.db, self.lot, "verdict", sha=head_a, actor="cx3",
                       verdict="blocked")
        # L'auteur corrige lui-même depuis le B.
        self.repo.write("src/ameesh/mail.py", "lot\nauteur : correctif\n")
        head_b = self.repo.commit("author: correctif", agent="author")
        self.receipts(commits=[head_b])
        self.frozen = head_b
        return probe

    def test_a_never_pushed_probe_excludes_nobody(self):
        probe = self._prepare_probe()
        with mock.patch.object(review_authors, "_forge_fetch",
                               side_effect=_refusing(probe)):
            _mark, out = self.prefetched_freeze()      # F2
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertNotIn("cx3", out.get("fixers") or [])
        self.assertNotIn("excluded by doubt", out["why"])
        self.assertIn("never pushed", out["why"])
        self.assertIn(probe[:12], out["why"])

    def test_a_forge_without_answer_keeps_the_doubt(self):
        probe = self._prepare_probe()
        with mock.patch.object(review_authors, "_forge_fetch",
                               return_value=None):
            _mark, out = self.prefetched_freeze()      # F2
        self.assertIn("excluded by doubt", out["why"])
        self.assertIn(probe[:12], out["why"])

    def test_a_rebased_correction_seen_on_the_branch_stays_excluded(self):
        _head_a, local, pushed, _head_b = self._prepare_doubt()
        self.assertIsNotNone(plan_git.local_commit(self.repo.root, pushed))
        self.db.execute(
            "INSERT INTO work_item_branch_heads (work_item_id, branch, sha)"
            " VALUES (%s, 'lot-branch', %s)", (self.lot, pushed))
        with mock.patch.object(review_authors, "_forge_fetch",
                               side_effect=_refusing(local)):
            _mark, out = self.prefetched_freeze()      # F2
        names = [row["name"] for row in out["reviewers"]]
        self.assertNotIn("cx3", names)
        self.assertIn("excluded by doubt", out["why"])
        self.assertIn(local[:12], out["why"])

    def test_a_correction_pushed_on_a_new_base_stays_excluded(self):
        # B/P2 mty-amh-impl-06 : le correctif poussé (C) est rebasé sur une
        # base neuve, il ne descend pas de F1 ; le relevé l'a vu, puis l'auteur
        # réécrit. Le reçu local L est refusé par la forge : cx3 reste exclu.
        _head_a, local, _pushed, head_b = self._prepare_doubt()
        branch = self.repo.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        self.repo.git("checkout", "-q", "-B", "nouvelle-base", self.base)
        self.repo.write("docs/avance.md", "la cible avance\n")
        self.repo.commit("cible : avance", agent="author")
        self.repo.write("src/ameesh/mail.py", "lot\ncx3 petit correctif\n")
        rebased_fix = self.repo.commit("cx3: correctif rebasé", agent="cx3")
        self.repo.git("checkout", "-q", branch)
        self.repo.git("reset", "--hard", head_b)
        self.assertIs(False, review_authors._is_ancestor(
            self.repo.root, _head_a, rebased_fix))
        self.db.execute(
            "INSERT INTO work_item_branch_heads (work_item_id, branch, sha)"
            " VALUES (%s, 'lot-branch', %s)", (self.lot, rebased_fix))
        with mock.patch.object(review_authors, "_forge_fetch",
                               side_effect=_refusing(local)):
            _mark, out = self.prefetched_freeze()   # F2
        names = [row["name"] for row in out["reviewers"]]
        self.assertNotIn("cx3", names)
        self.assertIn("excluded by doubt", out["why"])
        self.assertNotIn("never pushed", out["why"])

    def test_a_refused_rebased_correction_without_push_trace_is_a_probe(self):
        _head_a, local, _pushed, _head_b = self._prepare_doubt()
        with mock.patch.object(review_authors, "_forge_fetch",
                               side_effect=_refusing(local)):
            _mark, out = self.prefetched_freeze()      # F2
        self.assertNotIn("excluded by doubt", out["why"])
        self.assertIn("never pushed", out["why"])

    def test_a_probe_still_readable_in_the_clone_excludes_nobody(self):
        # B/P2 cx2 : la sonde reste dans le clone de lecture ; la forge seule
        # dit qu'elle n'a jamais été publiée.
        probe = self._prepare_probe(forget=False)
        self.assertIsNotNone(plan_git.local_commit(self.repo.root, probe))
        served = lambda repo, sha, *, timeout=None: (
            False if plan_git.same_commit(sha, probe) else None)
        with mock.patch.object(review_authors, "_forge_serves",
                               side_effect=served):
            _mark, out = self.prefetched_freeze()   # F2
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertNotIn("cx3", out.get("fixers") or [])
        self.assertNotIn("corrector(s) excluded: cx3", out["why"])
        self.assertIn("never pushed", out["why"])
        self.assertIn(probe[:12], out["why"])

    def test_a_readable_correction_the_forge_serves_stays_excluded(self):
        # #4457 : publiée puis réécrite, la correction exclut toujours.
        self._prepare_probe(forget=False)
        with mock.patch.object(review_authors, "_forge_serves",
                               return_value=True):
            _mark, out = self.prefetched_freeze()   # F2
        names = [row["name"] for row in out["reviewers"]]
        self.assertNotIn("cx3", names)
        self.assertNotIn("never pushed", out["why"])


class ForgeServesTest(unittest.TestCase):
    """`_forge_serves` interroge la forge seule, jamais l'objet local."""

    def git(self, cwd, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@x.invalid",
                   GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@x.invalid")
        return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_served_published_refused_local_and_none_without_forge(self):
        with tempfile.TemporaryDirectory() as root:
            forge, clone = os.path.join(root, "forge.git"), os.path.join(root, "clone")
            self.git(root, "init", "-q", "--bare", forge)
            self.git(root, "init", "-q", clone)
            self.git(clone, "remote", "add", "origin", "../forge.git")
            self.git(clone, "commit", "-q", "--allow-empty", "-m", "publié")
            published = self.git(clone, "rev-parse", "HEAD")
            self.git(clone, "push", "-q", "origin", "HEAD:refs/heads/lot")
            self.git(clone, "commit", "-q", "--allow-empty", "-m", "sonde")
            probe = self.git(clone, "rev-parse", "HEAD")
            self.assertIs(review_authors._forge_serves(clone, published), True)
            self.assertIs(review_authors._forge_serves(clone, probe), False)
            self.git(clone, "remote", "remove", "origin")
            self.assertIsNone(review_authors._forge_serves(clone, probe))
