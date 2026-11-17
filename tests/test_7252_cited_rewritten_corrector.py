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
"""
from __future__ import annotations

import io
import json
import os
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
        for token in (blob[:8], tree[:12]):
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

    def test_hex_word_requires_a_definitive_forge_negative(self):
        from ameesh import plan_github

        _fix, head = self._rewrite(cite="ed25519")
        for status, message, accepted in (
                (404, "No commit found for SHA: ed25519", True),
                (422, "No commit found for SHA: ed25519", True),
                (404, "Not Found", False),
                (422, "Ambiguous SHA", False),
                (403, "API rate limit exceeded", False),
                (503, "Service unavailable", False)):
            with self.subTest(status=status, message=message):
                def unavailable(*args, **kwargs):
                    raise urllib.error.HTTPError(
                        "https://api.invalid", status, "failure", {},
                        io.BytesIO(json.dumps({"message": message}).encode()))

                api = plan_github.Api(token="test", opener=unavailable)
                with mock.patch.object(review_authors, "_repository_slug",
                                       return_value="owner/repo"), \
                        mock.patch.object(plan_github, "client", return_value=api), \
                        mock.patch.object(review_authors, "_forge_fetch") as fetch:
                    if accepted:
                        (_authors, unknown, correctors), _freeze = self._reading(
                            head, strict=True)
                        self.assertEqual(unknown, [])
                        self.assertEqual(correctors, [])
                    else:
                        with self.assertRaisesRegex(
                                review_provenance.ProvenanceError,
                                "cited commit ed25519 .*forge-unreadable"):
                            self._reading(head, strict=True)
                    fetch.assert_not_called()

    def test_numeric_citation_requires_a_definitive_forge_negative(self):
        _fix, head = self._rewrite(cite="43969930787")
        with mock.patch.object(review_authors, "_forge_commit", return_value=None):
            self._reading(head, strict=True)
        for fetch in (False, True):
            with self.subTest(fetch=fetch):
                with mock.patch.object(review_authors, "_forge_commit", return_value=""):
                    with self.assertRaises(review_provenance.ProvenanceError):
                        self._reading(head, strict=True, fetch=fetch)

    def test_numeric_abbreviation_resolved_by_forge_excludes_corrector(self):
        fix, head = self._remote_rewrite()
        body = self.repo.git("log", "-1", "--format=%B").replace(fix[:12], "1234567")
        self.repo.git("commit", "--amend", "-m", body)
        head = self.repo.git("rev-parse", "HEAD")
        self._receipt("author", "deepseek", [head])
        with mock.patch.object(review_authors, "_forge_commit", return_value=fix) as forge:
            (_authors, unknown, correctors), _freeze = self._reading(head, strict=True)
        self.assertEqual(unknown, [])
        self.assertEqual([row["name"] for row in correctors], ["cx3"])
        self.assertEqual(forge.call_args.args[1], "1234567")

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
            "author: correctif-43969930787 ; paquet h-20261117-05",
            agent="author")
        self._receipt("author", "deepseek", [head])
        with mock.patch.object(review_authors, "_forge_commit",
                               return_value="") as forge:
            with self.assertRaisesRegex(
                    review_provenance.ProvenanceError,
                    "cited commit 43969930787 .*forge-unreadable"):
                self._reading(head, strict=True)
        self.assertEqual([call.args[1] for call in forge.call_args_list],
                         ["43969930787"])


class ForgeCitationResolutionTest(unittest.TestCase):
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
