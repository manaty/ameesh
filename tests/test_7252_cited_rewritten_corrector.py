# SPDX-License-Identifier: AGPL-3.0-only
"""#7252 — un correctif retiré, cité par la réécriture de l'auteur, exclut son acteur.

Cas réel #7248 (gel #40563, 0e8f046c6f95) : le relecteur auditeur avait poussé
son correctif 5e2c911 après le gel #40555, hors du hook de provenance (agent de
gouvernance sans tour lié). L'auteur a retiré ce commit et l'a réécrit depuis
le B ; le message de réécriture cite le SHA retiré et le B. Sans reçu exact,
`_rewritten_after_freeze` (#4457) ne voyait rien et la désignation asseyait le
correcteur.

Témoins : le commit retiré cité par l'auteur est exclu **par son nom**, sa
famille reste éligible (§2) ; un correctif qui reste dans la tête garde la
règle existante (famille du correcteur interdite) ; une citation d'un commit
encore dans la tête n'exclut personne ; un commit cité absent du clone est
résolu par la forge puis rapatrié (B/P2 auditeur) ; une forge muette trace un
doute au gel, sans exclure.
"""
from __future__ import annotations

import os
import subprocess

from unittest import mock

from ameesh import registry, review_authors, review_designation as rd, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

FAMILIES = ("claude", "codex", "deepseek")


class _Fixture:
    """Un gel F1, son relecteur cx3, puis une tête réécrite par l'auteur."""

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        self.repo.write("src/ameesh/mail.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/mail.py", "lot\n")
        self.head_a = self.repo.commit("lot")
        for name, harness in (("author", "deepseek"), ("r1", "claude"),
                              ("r2", "codex"), ("cx3", "codex")):
            registry.upsert(self.db, name, harness=harness,
                            host=self.cfg.host, cwd=self.repo.root,
                            status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        self.item = work.add(self.db, title="citation #7252", app="ameesh",
                             assignee="author")
        self.lot = int(self.item["id"])
        work.move(self.db, self.lot, "build")
        self.db.execute(
            "UPDATE work_items SET branch_target = %s, reviewers = ARRAY['cx3']"
            " WHERE id = %s", (self.base, self.lot))
        self._milestone("frozen", self.head_a)
        self._milestone("verdict", self.head_a, actor="cx3", verdict="blocked")
        self._receipt("author", "deepseek", [self.head_a])

    # -- helpers ----------------------------------------------------------
    def _milestone(self, kind, sha, *, actor="author", verdict=None):
        rows = self.db.query(
            "INSERT INTO work_item_milestones"
            " (work_item_id, kind, sha, actor, verdict)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (self.lot, kind, sha, actor, verdict))
        return {"id": int(rows[0]["id"]), "sha": sha, "at_ts": None,
                "actor": actor}

    def _receipt(self, actor, harness, commits):
        for sha in commits:
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject) DO NOTHING",
                ("commit:" + sha, sha, actor, "s-" + harness, "t", harness,
                 self.lot))

    def _rewrite(self, *, keep=False, cite=None):
        """Le correctif de cx3 après F1, puis la tête B de l'auteur.

        `keep` garde le correctif dans la tête (règle existante). `cite` vaut
        None (citer le correctif retiré), False (ne rien citer) ou un SHA.
        """
        self.repo.write("src/ameesh/mail.py", "lot\ncx3 petit correctif\n")
        fix = self.repo.commit("cx3: correctif de relecture", agent="cx3")
        if keep:
            self._receipt("cx3", "codex", [fix])
            self.repo.write("src/ameesh/mail.py",
                            "lot\ncx3 petit correctif\nauteur : garde\n")
            head_b = self.repo.commit("author: garde le correctif",
                                      agent="author")
        else:
            # L'auteur réécrit depuis F1 : le commit du relecteur n'est plus
            # ancêtre de la tête. Le contenu diffère, aucune reprise (#2924).
            self.repo.git("reset", "--hard", self.head_a)
            self.repo.write("src/ameesh/mail.py",
                            "lot\ncx3 petit correctif\nauteur : reprise\n")
            message = "author: reprise du correctif"
            if cite is not False:
                token = fix[:12] if cite is None else str(cite)[:12]
                message += ("\n\nB #40556 : le correctif du relecteur %s est"
                            " retiré de l'historique et remplacé par cette"
                            " réécriture d'auteur." % token)
            head_b = self.repo.commit(message, agent="author")
        self._receipt("author", "deepseek", [head_b])
        return fix, head_b

    def _remote_rewrite(self):
        """Le correctif de cx3 n'existe que sur la forge, pas dans le clone lu.

        Le clone lu pousse F1 sur une forge nue, un second clone y ajoute le
        correctif, puis l'auteur réécrit depuis F1 : le SHA cité est absent du
        clone lu (push forcé), comme sur un hôte qui n'a pas fetché entre le
        correctif et la réécriture.
        """
        forge = os.path.join(self.tmp, "forge.git")
        fixer = os.path.join(self.tmp, "fixer")
        self.repo.git("init", "--bare", "-q", forge)
        self.repo.git("remote", "add", "origin", forge)
        self.repo.git("push", "-q", "origin",
                      "%s:refs/heads/main" % self.head_a)
        self.repo.git("--git-dir", forge, "symbolic-ref", "HEAD",
                      "refs/heads/main")
        subprocess.run(["git", "clone", "-q", forge, fixer], check=True,
                       capture_output=True, text=True)
        with open(os.path.join(fixer, "src/ameesh/mail.py"), "w",
                  encoding="utf-8") as handle:
            handle.write("lot\ncx3 petit correctif\n")
        env = dict(os.environ,
                   GIT_AUTHOR_NAME="cx3",
                   GIT_AUTHOR_EMAIL="cx3@ameesh.invalid",
                   GIT_COMMITTER_NAME="cx3",
                   GIT_COMMITTER_EMAIL="cx3@ameesh.invalid")
        for argv in (["add", "-A"], ["commit", "-qm",
                                     "cx3: correctif de relecture"]):
            subprocess.run(["git", "-C", fixer, *argv], check=True,
                           capture_output=True, text=True, env=env)
        fix = subprocess.run(["git", "-C", fixer, "rev-parse", "HEAD"],
                             check=True, capture_output=True, text=True,
                             env=env).stdout.strip()
        subprocess.run(["git", "-C", fixer, "push", "-q", "origin",
                        "HEAD:refs/heads/main"], check=True,
                       capture_output=True, text=True, env=env)
        self.repo.git("reset", "--hard", self.head_a)
        self.repo.write("src/ameesh/mail.py",
                        "lot\ncx3 petit correctif\nauteur : reprise\n")
        head_b = self.repo.commit(
            "author: reprise du correctif\n\nB #40556 : le correctif du"
            " relecteur %s est retiré de l'historique et remplacé par cette"
            " réécriture d'auteur." % fix[:12], agent="author")
        self._receipt("author", "deepseek", [head_b])
        return fix, head_b

    def _reading(self, head_b, doubts=None):
        commits = self.repo.git("rev-list", "--no-merges",
                                "%s..%s" % (self.base, head_b)).split()
        freeze = self._milestone("frozen", head_b)
        return review_authors.reading(
            self.db, work.get(self.db, self.lot), freeze, commits,
            self.repo.root, FAMILIES, doubts_out=doubts), freeze

    def _select(self, head_b, doubts=None):
        (authors, unknown, correctors), _freeze = self._reading(head_b, doubts)
        self.assertEqual(unknown, [])
        out = rd.select(
            required=2, authors=authors,
            candidates=[rd.Candidate("r1", "claude", True, 0),
                        rd.Candidate("r2", "codex", True, 0),
                        rd.Candidate("cx3", "codex", True, 0)],
            families=FAMILIES, fixers=correctors)
        return authors, correctors, out

    # -- witnesses --------------------------------------------------------
    def test_a_cited_removed_correction_excludes_its_reviewer(self):
        fix, head_b = self._rewrite()
        authors, correctors, out = self._select(head_b)
        self.assertEqual([row["name"] for row in correctors], ["cx3"])
        self.assertEqual(correctors[0]["family"], "")
        self.assertEqual(correctors[0]["rewritten"], fix)
        self.assertEqual(correctors[0]["cited"], head_b)
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        names = [row["name"] for row in out["reviewers"]]
        self.assertNotIn("cx3", names)
        # §2 : la famille codex reste éligible, un autre agent relit.
        self.assertIn("r1", names)
        self.assertIn("r2", names)
        self.assertIn("cx3", out["why"])
        self.assertIn(fix[:12], out["why"])
        self.assertIn("rewritten correction", out["why"])
        self.assertIn("cited by %s" % head_b[:12], out["why"])

    def test_a_kept_correction_keeps_the_family_exclusion(self):
        _fix, head_b = self._rewrite(keep=True)
        authors, correctors, out = self._select(head_b)
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"author": "deepseek", "cx3": "codex"})
        self.assertEqual([row["name"] for row in correctors], ["cx3"])
        self.assertEqual(correctors[0]["family"], "codex")
        # Le correctif est resté dans la tête : cx3 est auteur de la tête et
        # sa famille codex est interdite comme celle de tout auteur (#2888).
        self.assertEqual(out["state"], rd.WAITING, out)
        names = [row["name"] for row in out["reviewers"]]
        self.assertIn("r1", names)
        self.assertNotIn("r2", names)
        self.assertNotIn("cx3", names)
        self.assertIn("codex", out["why"])

    def test_an_uncited_removal_does_not_exclude(self):
        _fix, head_b = self._rewrite(cite=False)
        _authors, correctors, _out = self._select(head_b)
        self.assertEqual(correctors, [])

    def test_a_citation_of_a_commit_still_in_the_head_does_not_exclude(self):
        _fix, head_b = self._rewrite(cite=self.head_a)
        _authors, correctors, _out = self._select(head_b)
        self.assertEqual(correctors, [])

    def test_a_cited_commit_outside_the_lot_does_not_exclude(self):
        _fix, head_b = self._rewrite(cite=self.base)
        _authors, correctors, _out = self._select(head_b)
        self.assertEqual(correctors, [])

    def test_a_cited_commit_absent_from_the_clone_is_resolved_at_the_forge(self):
        fix, head_b = self._remote_rewrite()
        self.assertNotEqual(subprocess.run(
            ["git", "-C", self.repo.root, "cat-file", "-e", fix],
            capture_output=True).returncode, 0)
        with mock.patch.object(review_authors, "_forge_commit",
                               lambda repo, token, **kw: fix):
            _authors, correctors, out = self._select(head_b)
        # B/P2 auditeur : le clone de lecture n'avait pas l'objet ; la forge
        # a résolu l'abrégé, l'objet exact a été rapatrié, l'exclusion tient.
        self.assertEqual([row["name"] for row in correctors], ["cx3"])
        self.assertEqual(correctors[0]["rewritten"], fix)
        self.assertEqual(correctors[0]["cited"], head_b)
        self.assertEqual(self.repo.git("rev-parse", fix + "^{commit}"), fix)
        names = [row["name"] for row in out["reviewers"]]
        self.assertNotIn("cx3", names)
        self.assertIn("r2", names)      # §2 : la famille codex reste éligible

    def test_an_unreadable_forge_traces_a_doubt_without_excluding(self):
        fix, head_b = self._remote_rewrite()
        doubts: list = []
        with mock.patch.object(review_authors, "_forge_commit",
                               lambda repo, token, **kw: ""):
            _authors, correctors, _out = self._select(head_b, doubts)
        self.assertEqual(correctors, [])
        self.assertEqual([row["name"] for row in doubts], [""])
        self.assertEqual(doubts[0]["sha"], fix[:12])
        self.assertEqual(doubts[0]["reason"], "forge-unreadable")
        self.assertEqual(doubts[0]["cited"], head_b)
        note = rd.doubts_note(doubts)
        self.assertIn("1 cited commit(s) unreadable at the forge", note)
        self.assertIn(fix[:12], note)
        self.assertIn("cited by %s" % head_b[:12], note)


class CitedRewrittenCorrectorTest(_Fixture, PgTestCase):
    """Les témoins #7252, sur la lecture et la sélection du gel."""
