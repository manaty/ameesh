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
doute au gel, sans exclure ; la lecture de la transaction (`fetch=False`,
#3836) n'interroge jamais la forge et dit la citation non préparée ; le
prefetch hors transaction, sur la plage gelée, la prépare et l'exclusion tient
alors sans réseau.

#8224/#8228 : quand le correctif du relecteur avait lui-même été gelé comme
tête, `_carried_from_previous` le reportait auteur (#2924) et sa famille
restait interdite malgré la réécriture citée. Un acteur prouvé réécrit, sans
autre commit dans la plage courante, sort des auteurs et reste correcteur ;
sans preuve de réécriture, le report d'auteur demeure (pas de blanchiment).
#8230 : la preuve vaut par commit : un correctif ancien reporté, non prouvé
réécrit, garde son acteur auteur même quand un autre correctif est cité.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
import subprocess

from unittest import mock

from ameesh import (registry, review_authors, review_designation as rd,
                    review_provenance, work)

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

    def _freeze_fix(self, fix):
        """Le correctif du relecteur est lui-même une tête gelée (#8224).

        Cas vivant #8224 : le gel #46196 porte le commit f42d2abce du
        correcteur, puis l'auteur le réécrit et le cite en #46201. La poussée
        est relevée sur la branche du lot (`branch_heads`) : la citation est
        alors une correction publiée, pas une sonde locale (#6934).
        """
        self.db.execute(
            "INSERT INTO work_item_branch_heads (work_item_id, branch, sha)"
            " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (self.lot, "lot/8224", fix))
        return self._milestone("frozen", fix)

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

    def _reading(self, head_b, doubts=None, *, fetch=True, strict=False):
        commits = self.repo.git("rev-list", "--no-merges",
                                "%s..%s" % (self.base, head_b)).split()
        freeze = self._milestone("frozen", head_b)
        return review_authors.reading(
            self.db, work.get(self.db, self.lot), freeze, commits,
            self.repo.root, FAMILIES, doubts_out=doubts,
            fetch=fetch, strict=strict), freeze

    def _select(self, head_b, doubts=None, *, fetch=True):
        (authors, unknown, correctors), _freeze = self._reading(
            head_b, doubts, fetch=fetch)
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

    def test_strict_reading_refuses_an_unresolved_citation_without_a_trace_collector(self):
        fix, head_b = self._remote_rewrite()
        for fetch, resolved, fetched, reason in (
                (False, "", False, "not-prefetched"),
                (True, "", False, "forge-unreadable"),
                (True, fix, False, "forge-unreadable"),
                (True, fix, True, "forge-unreadable")):
            with self.subTest(fetch=fetch, resolved=resolved, fetched=fetched):
                with mock.patch.object(review_authors, "_forge_commit",
                                       return_value=resolved) as resolve, \
                        mock.patch.object(review_authors, "_forge_fetch",
                                          return_value=fetched) as download:
                    with self.assertRaisesRegex(
                            review_provenance.ProvenanceError,
                            "cited commit %s .*%s" % (fix[:12], reason)):
                        self._reading(head_b, fetch=fetch, strict=True)
                if not fetch:
                    resolve.assert_not_called()
                    download.assert_not_called()

    def test_strict_reading_accepts_a_citation_recovered_from_the_forge(self):
        fix, head_b = self._remote_rewrite()
        with mock.patch.object(review_authors, "_forge_commit", return_value=fix):
            (_authors, unknown, correctors), _freeze = self._reading(
                head_b, strict=True)
        self.assertEqual(unknown, [])
        self.assertEqual([row["name"] for row in correctors], ["cx3"])
        self.assertEqual(correctors[0]["rewritten"], fix)

    def test_a_transaction_reading_never_queries_the_forge(self):
        # #3836 (B/P2 cx2) : `fetch=False` est la lecture de la transaction du
        # gel ; elle ne doit tenir aucun verrou pendant un accès réseau. Une
        # citation absente localement est un doute, jamais un `_forge_commit`
        # ni un `git fetch`.
        fix, head_b = self._remote_rewrite()
        doubts: list = []
        calls: list = []

        def forbidden(*args, **kwargs):
            calls.append(args)
            raise AssertionError("fetch=False must not reach the forge")

        with mock.patch.object(review_authors, "_forge_commit", forbidden), \
                mock.patch.object(review_authors, "_forge_fetch", forbidden), \
                mock.patch.object(review_authors, "_forge_serves", forbidden):
            _authors, correctors, _out = self._select(head_b, doubts,
                                                      fetch=False)
        self.assertEqual(calls, [])
        self.assertEqual(correctors, [])
        self.assertEqual([row["name"] for row in doubts], [""])
        self.assertEqual(doubts[0]["sha"], fix[:12])
        self.assertEqual(doubts[0]["reason"], "not-prefetched")
        self.assertEqual(doubts[0]["cited"], head_b)
        note = rd.doubts_note(doubts)
        self.assertIn("1 cited commit(s) not prepared before the freeze", note)
        self.assertIn(fix[:12], note)
        self.assertIn("cited by %s" % head_b[:12], note)

    def test_prefetch_prepares_the_cited_commit_before_the_transaction(self):
        # #3836 (B/P2 cx2) : le rapatriement hors transaction reçoit la plage
        # gelée et prépare les commits cités ; la lecture `fetch=False` les
        # trouve alors localement et exclut le correcteur, sans réseau.
        fix, head_b = self._remote_rewrite()
        self.assertNotEqual(subprocess.run(
            ["git", "-C", self.repo.root, "cat-file", "-e", fix],
            capture_output=True).returncode, 0)
        commits = self.repo.git("rev-list", "--no-merges",
                                "%s..%s" % (self.base, head_b)).split()
        with mock.patch.object(review_authors, "_forge_commit",
                               lambda repo, token, **kw: fix):
            review_authors.prefetch_prior_freezes(
                self.db, work.get(self.db, self.lot), self.repo.root, head_b,
                commits=commits)
        self.assertEqual(self.repo.git("rev-parse", fix + "^{commit}"), fix)
        doubts: list = []

        def forbidden(*args, **kwargs):
            raise AssertionError("the prefetch must have prepared the object")

        with mock.patch.object(review_authors, "_forge_commit", forbidden), \
                mock.patch.object(review_authors, "_forge_fetch", forbidden), \
                mock.patch.object(review_authors, "_forge_serves", forbidden):
            _authors, correctors, out = self._select(head_b, doubts,
                                                     fetch=False)
        self.assertEqual(doubts, [])
        self.assertEqual([row["name"] for row in correctors], ["cx3"])
        self.assertEqual(correctors[0]["rewritten"], fix)
        self.assertEqual(correctors[0]["cited"], head_b)
        self.assertNotIn("cx3", [row["name"] for row in out["reviewers"]])


class CitedRewrittenCorrectorTest(_Fixture, PgTestCase):
    """Les témoins #7252, sur la lecture et la sélection du gel."""

    def test_strict_reading_ignores_non_commit_objects_and_run_numbers(self):
        blob = self.repo.git("rev-parse", "HEAD:src/ameesh/mail.py")
        tree = self.repo.git("rev-parse", "HEAD^{tree}")
        for token in (blob[:12], tree[:12]):
            for fetch in (False, True):
                with self.subTest(token=token, fetch=fetch):
                    self.repo.git("reset", "--hard", self.head_a)
                    self.repo.write("src/ameesh/mail.py", token + "\n")
                    head = self.repo.commit("author: reference (%s)" % token,
                                            agent="author")
                    self._receipt("author", "deepseek", [head])
                    with mock.patch.object(review_authors, "_forge_commit") as forge:
                        (_authors, unknown, correctors), _freeze = self._reading(
                            head, strict=True, fetch=fetch)
                    self.assertEqual(unknown, [])
                    self.assertEqual(correctors, [])
                    forge.assert_not_called()

    def test_hex_word_is_not_a_citation_and_never_asks_the_forge(self):
        # #7721 (B auditeur sur 474fe7c9) : `ed25519` (7 hex) et un abrégé de
        # 9 caractères absent du clone ne sont pas des citations. La présence
        # locale est lue sans réseau : une forge inaccessible ou ambiguë ne
        # peut plus les refuser.
        for token in ("ed25519", "deadbeef1"):
            with self.subTest(token=token):
                self.repo.git("reset", "--hard", self.head_a)
                _fix, head = self._rewrite(cite=token)
                with mock.patch.object(review_authors, "_forge_commit") as forge, \
                        mock.patch.object(review_authors, "_forge_fetch") as fetch:
                    (_authors, unknown, correctors), _freeze = self._reading(
                        head, strict=True)
                self.assertEqual(unknown, [])
                self.assertEqual(correctors, [])
                forge.assert_not_called()
                fetch.assert_not_called()

    def test_short_local_citation_still_excludes_its_reviewer(self):
        # B auditeur sur 474fe7c9 : l'abrégé usuel de Git (9 caractères ici)
        # d'un correctif encore présent dans le clone reste une citation ; le
        # correcteur est exclu par son nom et la forge n'est pas interrogée.
        fix, _head = self._rewrite()
        short = fix[:9]
        self.repo.git("commit", "--amend", "-m",
                      "author: reprise ; correctif %s retiré" % short)
        head = self.repo.git("rev-parse", "HEAD")
        self._receipt("author", "deepseek", [head])
        with mock.patch.object(review_authors, "_forge_commit") as forge:
            (_authors, unknown, correctors), _freeze = self._reading(
                head, strict=True)
        self.assertEqual(unknown, [])
        self.assertEqual([row["name"] for row in correctors], ["cx3"])
        self.assertEqual(correctors[0]["rewritten"], fix)
        self.assertEqual(correctors[0]["cited"], head)
        # La citation courte est qualifiée par la présence locale : elle
        # n'appelle jamais le résolveur d'abrégé de la forge.
        forge.assert_not_called()

    def test_unreadable_local_presence_refuses_instead_of_opening_provenance(self):
        # B2 auditeur/cx2 sur 3a7e2272 : quand la lecture groupée de présence
        # rend None (git en échec, coupé ou délai), l'abrégé court n'est pas
        # écarté en silence ; en lecture stricte, le doute refuse la porte.
        _fix, head = self._rewrite(cite="deadbeef1")
        real = review_authors._present_commits

        def unreadable(repo, shas, **kwargs):
            if "deadbeef1" in {str(sha).lower() for sha in shas}:
                return None
            return real(repo, shas, **kwargs)

        with mock.patch.object(review_authors, "_present_commits", unreadable), \
                mock.patch.object(review_authors, "_forge_commit",
                                  return_value=""):
            with self.assertRaisesRegex(
                    review_provenance.ProvenanceError,
                    "cited commit deadbeef1 .*forge-unreadable"):
                self._reading(head, strict=True)

    def test_a_locally_ambiguous_citation_is_a_doubt_whatever_the_forge_says(self):
        # B cx2 sur 0c407b5f3f8c : le statut ambigu de la lecture locale ne
        # doit pas être perdu dans _rewritten_after_freeze ; « No commit
        # found » ne l'écarte pas et une résolution unique ne le choisit pas.
        fix, head = self._rewrite(cite="deadbeef1")
        real = review_authors._present_commits

        def ambiguous(repo, shas, *, ambiguous=None, **kwargs):
            words = {str(sha).lower() for sha in shas}
            if ambiguous is not None and "deadbeef1" in words:
                ambiguous.update(words)
                return {}
            return real(repo, shas, **kwargs)

        for answer in (None, fix):
            with self.subTest(forge=answer):
                with mock.patch.object(review_authors, "_present_commits",
                                       ambiguous), \
                        mock.patch.object(review_authors, "_forge_commit",
                                          return_value=answer) as forge, \
                        mock.patch.object(review_authors, "_forge_fetch") as fetch:
                    with self.assertRaisesRegex(
                            review_provenance.ProvenanceError,
                            "cited commit deadbeef1 .*ambiguous"):
                        self._reading(head, strict=True)
                forge.assert_not_called()
                fetch.assert_not_called()

    def test_ambiguous_forge_still_refuses_a_qualified_citation(self):
        # La garantie de provenance reste : une vraie citation (12 hex) que
        # la forge ne résout pas explicitement est un doute refusé.
        _fix, head = self._rewrite(cite="deadbeef1234")
        for resolved in (None, ""):
            with self.subTest(resolved=resolved):
                with mock.patch.object(review_authors, "_forge_commit",
                                       return_value=resolved):
                    if resolved is None:
                        (_authors, unknown, correctors), _freeze = self._reading(
                            head, strict=True)
                        self.assertEqual(unknown, [])
                        self.assertEqual(correctors, [])
                    else:
                        with self.assertRaisesRegex(
                                review_provenance.ProvenanceError,
                                "cited commit deadbeef1234 .*forge-unreadable"):
                            self._reading(head, strict=True)

    def test_foreign_repository_sha_is_ignored_on_a_definitive_forge_negative(self):
        """#7721 : un SHA d'un autre dépôt, cité en entier dans le message,
        n'est pas un commit de ce clone. La forge qui répond explicitement
        « No commit found » l'écarte sans refus ni exclusion."""
        from ameesh import plan_github

        foreign = "0123456789abcdef0123456789abcdef01234567"
        _fix, _head = self._rewrite(cite=False)
        self.repo.git("commit", "--amend", "-m",
                      "author: reprise\n\nSHA d'un autre dépôt %s cité pour"
                      " mémoire." % foreign)
        head = self.repo.git("rev-parse", "HEAD")
        self._receipt("author", "deepseek", [head])

        def unavailable(*args, **kwargs):
            raise urllib.error.HTTPError(
                "https://api.invalid", 422, "failure", {},
                io.BytesIO(json.dumps(
                    {"message": "No commit found for SHA: " + foreign}).encode()))

        api = plan_github.Api(token="test", opener=unavailable)
        with mock.patch.object(review_authors, "_repository_slug",
                               return_value="owner/repo"), \
                mock.patch.object(plan_github, "client", return_value=api), \
                mock.patch.object(review_authors, "_forge_fetch") as fetch:
            (_authors, unknown, correctors), _freeze = self._reading(
                head, strict=True)
        self.assertEqual(unknown, [])
        self.assertEqual(correctors, [])
        fetch.assert_not_called()

    def test_foreign_qualified_repository_is_ignored_without_the_forge(self):
        """#7721 : `dépôt@sha` nommant un autre dépôt n'est pas une citation
        de cette tête ; aucune forge n'est interrogée pour lui."""
        foreign = "0123456789abcdef0123456789abcdef01234567"
        _fix, _head = self._rewrite(cite=False)
        self.repo.git("commit", "--amend", "-m",
                      "author: reprise\n\nVoir le dépôt other/foreign@%s." % foreign)
        head = self.repo.git("rev-parse", "HEAD")
        self._receipt("author", "deepseek", [head])
        with mock.patch.object(review_authors, "_repository_slug",
                               return_value="owner/repo"), \
                mock.patch.object(review_authors, "_forge_commit") as forge, \
                mock.patch.object(review_authors, "_forge_fetch") as fetch:
            (_authors, unknown, correctors), _freeze = self._reading(
                head, strict=True)
        self.assertEqual(unknown, [])
        self.assertEqual(correctors, [])
        forge.assert_not_called()
        fetch.assert_not_called()

    def test_numeric_citation_requires_a_definitive_forge_negative(self):
        _fix, head = self._rewrite(cite="439699307871")
        with mock.patch.object(review_authors, "_forge_commit", return_value=None):
            self._reading(head, strict=True)
        for fetch in (False, True):
            with self.subTest(fetch=fetch):
                with mock.patch.object(review_authors, "_forge_commit", return_value=""):
                    with self.assertRaises(review_provenance.ProvenanceError):
                        self._reading(head, strict=True, fetch=fetch)

    def test_numeric_abbreviation_resolved_by_forge_excludes_corrector(self):
        fix, head = self._remote_rewrite()
        body = self.repo.git("log", "-1", "--format=%B").replace(fix[:12], "123456789012")
        self.repo.git("commit", "--amend", "-m", body)
        head = self.repo.git("rev-parse", "HEAD")
        self._receipt("author", "deepseek", [head])
        with mock.patch.object(review_authors, "_forge_commit", return_value=fix) as forge:
            (_authors, unknown, correctors), _freeze = self._reading(head, strict=True)
        self.assertEqual(unknown, [])
        self.assertEqual([row["name"] for row in correctors], ["cx3"])
        self.assertEqual(forge.call_args.args[1], "123456789012")

    def test_strict_reading_ignores_explicit_checksums(self):
        for label in ("checksum", "sha256", "SHA256"):
            with self.subTest(label=label):
                self.repo.write("src/ameesh/mail.py", label + "\n")
                head = self.repo.commit(
                    "author: SQL %s deadbeef1234" % label, agent="author")
                self._receipt("author", "deepseek", [head])
                with mock.patch.object(review_authors, "_forge_commit") as forge:
                    (_authors, unknown, correctors), _freeze = self._reading(
                        head, strict=True)
                self.assertEqual(unknown, [])
                self.assertEqual(correctors, [])
                forge.assert_not_called()

    def test_checksum_does_not_hide_a_real_commit_citation(self):
        fix, head = self._rewrite()
        self.repo.git("commit", "--amend", "-m",
                      "author: checksum deadbeef1234; correctif %s" % fix[:12])
        head = self.repo.git("rev-parse", "HEAD")
        self._receipt("author", "deepseek", [head])
        (_authors, unknown, correctors), _freeze = self._reading(head, strict=True)
        self.assertEqual(unknown, [])
        self.assertEqual([row["name"] for row in correctors], ["cx3"])

    def test_strict_reading_ignores_a_package_name_date(self):
        # #7709 : la date d'un nom complet de paquet horaire (h-AAAAMMJJ-NN)
        # est consommée avec l'identifiant ; ce n'est pas une citation Git.
        # Le cas se répète dans les messages réels (#7692, #7707) et refusait
        # la porte en lecture stricte, faute de résolution à la forge.
        for text in ("paquet h-20261117-05",
                     "h-20261117-05",
                     "paquet-h-20261117-05"):
            with self.subTest(text=text):
                self.repo.git("reset", "--hard", self.head_a)
                self.repo.write("src/ameesh/mail.py", text + "\n")
                head = self.repo.commit("author: " + text, agent="author")
                self._receipt("author", "deepseek", [head])
                with mock.patch.object(review_authors, "_forge_commit") as forge:
                    (_authors, unknown, correctors), _freeze = self._reading(
                        head, strict=True)
                self.assertEqual(unknown, [])
                self.assertEqual(correctors, [])
                forge.assert_not_called()

    def test_package_name_does_not_hide_a_real_commit_citation(self):
        # #7709 : écarter la date du nom de paquet ne doit pas écarter la
        # vraie citation du correctif retiré présente dans le même message.
        fix, head = self._rewrite()
        self.repo.git("commit", "--amend", "-m",
                      "author: paquet h-20261117-05 ; correctif %s" % fix[:12])
        head = self.repo.git("rev-parse", "HEAD")
        self._receipt("author", "deepseek", [head])
        (_authors, unknown, correctors), _freeze = self._reading(head, strict=True)
        self.assertEqual(unknown, [])
        self.assertEqual([row["name"] for row in correctors], ["cx3"])
        self.assertEqual(correctors[0]["rewritten"], fix)
        self.assertEqual(correctors[0]["cited"], head)

    def test_hyphenated_real_citation_is_still_resolved(self):
        # B/P2 auditeur : un mot suivi d'un tiret n'est pas un identifiant
        # reconnu. La citation réelle `correctif-<sha>` reste lue et résolue ;
        # l'exclusion ne vaut que pour le nom complet d'un paquet.
        fix, head = self._rewrite()
        self.repo.git("commit", "--amend", "-m",
                      "author: correctif-%s retiré par la réécriture" % fix[:12])
        head = self.repo.git("rev-parse", "HEAD")
        self._receipt("author", "deepseek", [head])
        (_authors, unknown, correctors), _freeze = self._reading(head, strict=True)
        self.assertEqual(unknown, [])
        self.assertEqual([row["name"] for row in correctors], ["cx3"])
        self.assertEqual(correctors[0]["rewritten"], fix)
        self.assertEqual(correctors[0]["cited"], head)

    def test_package_name_does_not_excuse_an_unreadable_short_sha(self):
        # #7709 : un vrai jeton court avec tiret, numérique comme la date du
        # paquet, reste une citation à résoudre ; introuvable, il refuse.
        self.repo.git("reset", "--hard", self.head_a)
        self.repo.write("src/ameesh/mail.py", "package\n")
        head = self.repo.commit(
            "author: correctif-439699307871 ; paquet h-20261117-05",
            agent="author")
        self._receipt("author", "deepseek", [head])
        with mock.patch.object(review_authors, "_forge_commit",
                               return_value="") as forge:
            with self.assertRaisesRegex(
                    review_provenance.ProvenanceError,
                    "cited commit 439699307871 .*forge-unreadable"):
                self._reading(head, strict=True)
        self.assertEqual([call.args[1] for call in forge.call_args_list],
                         ["439699307871"])

    def test_a_rewritten_correction_frozen_as_a_head_leaves_the_authors(self):
        # #8224/#8228 : le correctif de cx3 avait lui-même été gelé (#46196).
        # `_carried_from_previous` le portait auteur de la tête (#2924) avec sa
        # famille codex ; la désignation rendait donc « forbidden author
        # families: claude, deepseek ; eligible: none ». La preuve de
        # réécriture (#7252, citation de l'auteur) doit le sortir des auteurs.
        fix, head_b = self._rewrite()
        self._freeze_fix(fix)
        authors, correctors, out = self._select(head_b)
        self.assertEqual([row["name"] for row in authors], ["author"])
        self.assertEqual([row["name"] for row in correctors], ["cx3"])
        self.assertEqual(correctors[0]["family"], "")
        self.assertEqual(correctors[0]["rewritten"], fix)
        self.assertEqual(correctors[0]["cited"], head_b)
        # §2 : la famille codex redevient éligible, un autre agent relit.
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        names = [row["name"] for row in out["reviewers"]]
        self.assertIn("r2", names)
        self.assertNotIn("cx3", names)

    def test_without_rewrite_proof_the_carried_author_is_kept(self):
        # Témoin négatif : le reçu exact du correctif donne sa famille, mais
        # sans citation ni verdict qui porte la preuve de réécriture, le report
        # d'auteur de #2924 reste, le blanchiment de la famille est impossible
        # (canon §2).
        fix, head_b = self._rewrite(cite=False)
        self._receipt("cx3", "codex", [fix])
        self.db.execute(
            "DELETE FROM work_item_milestones"
            " WHERE work_item_id = %s AND kind = 'verdict'", (self.lot,))
        self._freeze_fix(fix)
        authors, correctors, out = self._select(head_b)
        self.assertIn("cx3", [row["name"] for row in authors])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"author": "deepseek", "cx3": "codex"})
        self.assertIn("codex", [row["family"] for row in correctors])
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertIn("codex", out["why"])

    def test_a_second_commit_of_the_actor_keeps_it_author(self):
        # Un acteur qui a aussi un commit non réécrit dans la plage courante
        # reste auteur : la preuve de réécriture ne vise que le commit retiré.
        fix, _head_b = self._rewrite()
        self._freeze_fix(fix)
        self.repo.write("src/ameesh/mail.py", "lot\nautre correction cx3\n")
        head_c = self.repo.commit("cx3: autre correction", agent="cx3")
        self._receipt("cx3", "codex", [head_c])
        authors, _correctors, out = self._select(head_c)
        self.assertIn("cx3", [row["name"] for row in authors])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"author": "deepseek", "cx3": "codex"})
        self.assertIn("codex", out["why"])

    def _two_corrections(self, *, cite_old):
        """#8230 : cx3 corrige deux fois (F0 gelé, puis F1), l'auteur reprend
        les deux contenus dans un seul commit qui cite F1, et F0 si `cite_old`."""
        self.repo.write("src/ameesh/other.py", "cx3 ancien correctif\n")
        old_fix = self.repo.commit("cx3: correctif ancien", agent="cx3")
        # Le reçu de F0 date de deux jours : hors de la fenêtre des verdicts,
        # aucune preuve topologique ne le nomme (seule la citation compte).
        self._receipt("cx3", "codex", [old_fix])
        self.db.execute(
            "UPDATE provenance_receipts SET at = now() - interval '2 days'"
            " WHERE sha = %s", (old_fix,))
        self._freeze_fix(old_fix)
        self._milestone("verdict", old_fix, actor="cx3", verdict="blocked")
        # F1 sans reçu (poussé hors du hook, #7252) : sa ligne est la citation.
        self.repo.write("src/ameesh/mail.py", "lot\ncx3 petit correctif\n")
        fix = self.repo.commit("cx3: correctif de relecture", agent="cx3")
        self._freeze_fix(fix)
        self.repo.git("reset", "--hard", self.head_a)
        self.repo.write("src/ameesh/other.py", "cx3 ancien correctif\n")
        self.repo.write("src/ameesh/mail.py",
                        "lot\ncx3 petit correctif\nauteur : reprise\n")
        cited = fix[:12] + (" et %s" % old_fix[:12] if cite_old else "")
        head_b = self.repo.commit(
            "author: reprise du correctif\n\nB #40556 : le correctif du"
            " relecteur %s est retiré de l'historique et remplacé par cette"
            " réécriture d'auteur." % cited, agent="author")
        self._receipt("author", "deepseek", [head_b])
        return head_b

    def test_an_uncited_older_correction_keeps_the_corrector_author(self):
        # #8230 : la preuve de réécriture vaut par commit. Le correctif ancien
        # F0 de cx3, gelé puis repris sans citation ni reçu qui le prouve
        # réécrit, survit : F1 cité ne blanchit pas la famille codex.
        authors, _correctors, out = self._select(
            self._two_corrections(cite_old=False))
        self.assertIn("cx3", [row["name"] for row in authors], out)
        self.assertNotEqual(out["state"], rd.DESIGNATED, out)

    def test_every_carried_correction_proven_leaves_the_authors(self):
        # #8230 : F0 et F1 tous deux cités : chaque commit reporté est prouvé
        # réécrit, cx3 sort des auteurs et sa famille redevient éligible (§2).
        authors, correctors, out = self._select(
            self._two_corrections(cite_old=True))
        self.assertEqual([row["name"] for row in authors], ["author"])
        self.assertIn("cx3", [row["name"] for row in correctors])
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertNotIn("cx3", [row["name"] for row in out["reviewers"]])

class ForgeCitationResolutionTest(unittest.TestCase):
    def test_hex_suffix_in_a_word_is_not_a_commit_citation(self):
        text = "owner-decision job succeeded; correctif-1234abcd5678 retiré"
        self.assertEqual(review_authors._cited_tokens(text, "owner/repo"),
                         ["1234abcd5678"])

    def test_short_hex_word_is_not_a_commit_citation(self):
        # #7721 : sans dépôt local, un jeton non qualifié de moins de 12
        # caractères n'est jamais une citation, même entier et borné
        # (`ed25519`, `deadbeef`, `correctif-1234abcd`).
        for text in ("ed25519", "deadbeef", "correctif-1234abcd",
                     "paquet h-20261117-05"):
            with self.subTest(text=text):
                self.assertEqual(
                    review_authors._cited_tokens(text, "owner/repo"), [])

    def test_a_short_citation_is_qualified_by_local_presence_only(self):
        # B auditeur sur 474fe7c9 : l'abrégé de 9 caractères d'un commit
        # présent dans le clone est une citation ; le même type de jeton
        # absent localement ne l'est pas, sans interroger la forge.
        with tempfile.TemporaryDirectory() as tmp:
            repo = _Repo(os.path.join(tmp, "depot"))
            repo.write("f.txt", "base\n")
            sha = repo.commit("base")
            self.assertEqual(
                review_authors._cited_tokens(
                    "reprend %s" % sha[:9], "owner/repo", repo=repo.root),
                [sha[:9]])
            self.assertEqual(
                review_authors._cited_tokens(
                    "ed25519 deadbeef1 correctif-1234abcd", "owner/repo",
                    repo=repo.root),
                [])

    def test_unreadable_local_presence_keeps_the_short_citation(self):
        # B2 auditeur/cx2 sur 3a7e2272 : None de _present_commits est une
        # lecture non concluante, pas une absence ; l'abrégé court reste un
        # jeton, et le chemin existant en fait un doute (refus en strict).
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(review_authors, "_present_commits",
                                   return_value=None):
                self.assertEqual(
                    review_authors._cited_tokens(
                        "reprend 4c39e880a", "owner/repo", repo=tmp),
                    ["4c39e880a"])

    def test_ambiguous_local_abbreviation_keeps_the_short_citation(self):
        # B2 cx2 : une abréviation ambiguë ne prouve ni présence ni absence ;
        # elle reste un jeton, dit au gel ou refusé en lecture stricte.
        with tempfile.TemporaryDirectory() as tmp:
            def probe(repo, shas, *, ambiguous=None, timeout=None):
                if ambiguous is not None:
                    ambiguous.update(str(sha).lower() for sha in shas)
                return {}

            with mock.patch.object(review_authors, "_present_commits",
                                   side_effect=probe):
                self.assertEqual(
                    review_authors._cited_tokens(
                        "reprend deadbeef1", "owner/repo", repo=tmp),
                    ["deadbeef1"])

    def test_present_commits_reports_an_ambiguous_abbreviation(self):
        # `cat-file --batch-check` répond « <jeton> ambiguous » pour une
        # abréviation ambiguë ; ce n'est pas une absence.
        with tempfile.TemporaryDirectory() as tmp:
            repo = _Repo(os.path.join(tmp, "depot"))
            repo.write("f.txt", "base\n")
            repo.commit("base")
            proc = mock.Mock(returncode=0, stdout=b"deadbeef1 ambiguous\n")
            ambiguous: set[str] = set()
            with mock.patch.object(review_authors, "_read_git",
                                   return_value=proc):
                self.assertEqual(
                    review_authors._present_commits(
                        repo.root, ["deadbeef1"], ambiguous=ambiguous), {})
            self.assertEqual(ambiguous, {"deadbeef1"})

    def test_qualified_repository_citation_keeps_a_short_sha(self):
        # La forme `dépôt@sha` qualifie un SHA court ; un autre dépôt nommé
        # n'est pas une citation de cette tête.
        for text in ("manaty/ameesh@1234abcd", "ameesh@1234abcd"):
            with self.subTest(text=text):
                self.assertEqual(
                    review_authors._cited_tokens(text, "manaty/ameesh"),
                    ["1234abcd"])
        self.assertEqual(
            review_authors._cited_tokens("other/repo@1234abcd", "manaty/ameesh"),
            [])
        # Un fork qui ne diffère que par le propriétaire n'est pas le dépôt lu.
        self.assertEqual(
            review_authors._cited_tokens("other/ameesh@1234abcd", "manaty/ameesh"),
            [])

    def test_gh_without_shared_reads_preserves_commit_absence(self):
        from ameesh import plan_github

        sha = "a" * 40
        for status, payload, expected in (
                (422, {"message": "No commit found for SHA: 43969930787"}, None),
                (404, {"message": "Not Found"}, ""),
                (403, {"message": "No commit found for SHA: 43969930787"}, ""),
                (200, {"sha": sha}, sha)):
            with self.subTest(status=status):
                gh = mock.Mock()
                gh.run.return_value = (
                    1 if status >= 400 else 0,
                    "HTTP/2.0 %d Failure\r\nContent-Type: application/json\r\n\r\n%s"
                    % (status, json.dumps(payload)),
                    "gh: request failed" if status >= 400 else "")
                api = plan_github.GhApi(gh, reads=None)
                with mock.patch.object(review_authors, "_repository_slug",
                                       return_value="owner/repo"), \
                        mock.patch.object(plan_github, "client", return_value=api):
                    self.assertEqual(review_authors._forge_commit(
                        "repo", "43969930787"), expected)
                gh.run.assert_called_once_with(
                    ["api", "repos/owner/repo/commits/43969930787", "-i"])

    def test_only_explicit_commit_absence_is_definitive(self):
        from ameesh import plan_github

        for status, message, expected in (
                (404, "No commit found for SHA: 43969930787", None),
                (422, "No commit found for SHA: 43969930787", None),
                (404, "Not Found", ""),
                (422, "Ambiguous SHA", ""),
                (403, "No commit found for SHA: 43969930787", ""),
                (503, "Service unavailable", "")):
            with self.subTest(status=status, message=message):
                def unavailable(*args, **kwargs):
                    raise urllib.error.HTTPError(
                        "https://api.invalid", status, "failure", {},
                        io.BytesIO(json.dumps({"message": message}).encode()))

                api = plan_github.Api(token="test", opener=unavailable)
                with mock.patch.object(review_authors, "_repository_slug",
                                       return_value="owner/repo"), \
                        mock.patch.object(plan_github, "client", return_value=api):
                    self.assertEqual(review_authors._forge_commit(
                        "repo", "43969930787"), expected)
