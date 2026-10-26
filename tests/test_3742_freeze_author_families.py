# SPDX-License-Identifier: AGPL-3.0-only
"""#3742 — le gel refuse une tête que les familles de ses auteurs rendent non relisible.

Le canon §2 exclut la famille de tout auteur de la tête, correcteurs compris,
et exige deux familles distinctes pour deux avis. Cas de référence : Nexlink
#3725, trois familles d'auteurs, deux avis requis, « 0 eligible families ».
Le gel doit refuser avant d'écrire le jalon, en nommant les commits à renvoyer
à l'assigné.
"""
from __future__ import annotations

import types
import unittest

from ameesh import freeze_families as ff
from ameesh import review_designation_activation as act
from ameesh import work

from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture

FAMILIES = ("claude", "codex", "deepseek")


def _inputs(*, authors=(), correctors=(), required=2, families=FAMILIES,
            exclude=True, distinct=True, seats=(), commits=None, unknown=(),
            assignee="author", risk="sensitive"):
    return types.SimpleNamespace(
        policy=types.SimpleNamespace(
            required=required, families=families, risk=risk,
            exclude_author_family=exclude, distinct_families=distinct),
        authors=list(authors), correctors=list(correctors),
        manual_seats=list(seats), unknown_authors=list(unknown),
        author_commits=dict(commits or {}),
        item={"assignee": assignee})


class AssessTest(unittest.TestCase):
    """La décision se prend sur les familles prouvées et la politique compilée."""

    def test_two_author_families_leave_one_for_two_reviews(self):
        verdict = ff.assess(_inputs(authors=[
            {"name": "author", "family": "claude"},
            {"name": "r1", "family": "codex"}]))
        self.assertEqual(verdict["state"], "short")
        self.assertEqual(verdict["required"], 2)
        self.assertEqual(verdict["need"], 2)
        self.assertEqual(verdict["eligible"], ["deepseek"])
        self.assertEqual(verdict["forbidden"], ["claude", "codex"])

    def test_three_author_families_leave_none(self):
        verdict = ff.assess(_inputs(authors=[
            {"name": "author", "family": "claude"},
            {"name": "r1", "family": "codex"},
            {"name": "r2", "family": "deepseek"}]))
        self.assertEqual(verdict["state"], "short")
        self.assertEqual(verdict["eligible"], [])
        self.assertEqual(verdict["forbidden"], ["claude", "codex", "deepseek"])

    def test_one_author_family_leaves_enough(self):
        verdict = ff.assess(_inputs(authors=[
            {"name": "author", "family": "claude"}]))
        self.assertEqual(verdict["state"], "ok")
        self.assertEqual(verdict["eligible"], ["codex", "deepseek"])

    def test_an_ambiguous_author_excludes_each_proven_family(self):
        verdict = ff.assess(_inputs(authors=[
            {"name": "author", "family": "",
             "families": ["claude", "codex"]}]))
        self.assertEqual(verdict["forbidden"], ["claude", "codex"])
        self.assertEqual(verdict["state"], "short")

    def test_a_reviewer_correction_is_an_author(self):
        verdict = ff.assess(_inputs(
            authors=[{"name": "author", "family": "deepseek"}],
            correctors=[{"name": "r1", "family": "claude"}]))
        self.assertEqual(verdict["state"], "short")
        self.assertEqual(verdict["forbidden"], ["claude", "deepseek"])
        self.assertTrue(next(row for row in verdict["authors"]
                             if row["name"] == "r1")["corrector"])

    def test_a_test_only_correction_forbids_no_family(self):
        verdict = ff.assess(_inputs(
            authors=[{"name": "author", "family": "deepseek"}],
            correctors=[{"name": "r1", "family": "claude", "test_only": True}]))
        self.assertEqual(verdict["state"], "ok")
        self.assertNotIn("claude", verdict["forbidden"])

    def test_an_unknown_author_does_not_make_the_head_look_reviewable(self):
        # La borne haute reste la même : une famille inconnue ne peut qu'en
        # retirer, jamais en ajouter. Trois familles interdites prouvées :
        # aucune lecture d'inconnu ne peut rendre la tête relisible.
        verdict = ff.assess(_inputs(
            authors=[{"name": "author", "family": "claude"},
                     {"name": "r1", "family": "codex"},
                     {"name": "r2", "family": "deepseek"}],
            unknown=("someone",)))
        self.assertEqual(verdict["state"], "short")
        self.assertEqual(verdict["unknown_authors"], ["someone"])

    def test_an_unknown_author_alone_does_not_refuse(self):
        verdict = ff.assess(_inputs(
            authors=[{"name": "author", "family": "claude"}],
            unknown=("someone",)))
        self.assertEqual(verdict["state"], "ok")

    def test_the_flag_extends_strictness_to_a_normal_single_review(self):
        # Hors lot léger/sensible et hors deux avis, le drapeau compilé est la
        # seule source de la stricte : faux, la famille de l'auteur compte.
        verdict = ff.assess(_inputs(
            required=1, risk="normal",
            authors=[{"name": "author", "family": "claude"},
                     {"name": "r1", "family": "codex"}],
            exclude=False))
        self.assertEqual(verdict["state"], "ok")
        self.assertEqual(verdict["forbidden"], [])
        strict = ff.assess(_inputs(
            required=1, risk="normal",
            authors=[{"name": "author", "family": "claude"},
                     {"name": "r1", "family": "codex"},
                     {"name": "r2", "family": "deepseek"}],
            exclude=False))
        self.assertEqual(strict["state"], "ok")
        # La même politique à drapeau vrai exclut les trois familles.
        self.assertEqual(ff.assess(_inputs(
            required=1, risk="normal", exclude=True,
            authors=[{"name": "author", "family": "claude"},
                     {"name": "r1", "family": "codex"},
                     {"name": "r2", "family": "deepseek"}],
        ))["state"], "short")

    def test_two_reviews_exclude_author_families_even_without_the_flag(self):
        # `merge_queue.review_gate` est strict dès deux avis, drapeau ou non.
        verdict = ff.assess(_inputs(
            required=2, risk="normal",
            authors=[{"name": "author", "family": "claude"},
                     {"name": "r1", "family": "codex"}],
            exclude=False))
        self.assertEqual(verdict["state"], "short")
        self.assertEqual(verdict["forbidden"], ["claude", "codex"])

    def test_a_light_single_review_excludes_author_families(self):
        verdict = ff.assess(_inputs(
            required=1, risk="light", exclude=False,
            authors=[{"name": "author", "family": "claude"},
                     {"name": "r1", "family": "codex"},
                     {"name": "r2", "family": "deepseek"}]))
        self.assertEqual(verdict["state"], "short")
        self.assertEqual(verdict["forbidden"], ["claude", "codex", "deepseek"])

    def test_one_family_may_cover_two_reviews_when_distinct_is_lifted(self):
        verdict = ff.assess(_inputs(
            authors=[{"name": "author", "family": "claude"},
                     {"name": "r1", "family": "codex"}],
            distinct=False))
        self.assertEqual(verdict["state"], "ok")
        self.assertEqual(verdict["need"], 1)

    def test_a_manual_seat_covers_one_opinion(self):
        verdict = ff.assess(_inputs(
            authors=[{"name": "author", "family": "claude"},
                     {"name": "r1", "family": "codex"}],
            seats=[{"name": "manual", "family": "deepseek"}]))
        # deepseek couvre un avis ; il reste un avis à désigner, mais la
        # famille deepseek est déjà le siège manuel : plus aucune famille.
        self.assertEqual(verdict["state"], "short")
        self.assertEqual(verdict["need"], 1)
        self.assertEqual(verdict["eligible"], [])

    def test_no_required_review_is_never_refused(self):
        verdict = ff.assess(_inputs(required=0, authors=[
            {"name": "author", "family": "claude"},
            {"name": "r1", "family": "codex"},
            {"name": "r2", "family": "deepseek"}]))
        self.assertEqual(verdict["state"], "ok")


class MessageTest(unittest.TestCase):
    """Le refus nomme les commits, les familles et l'assigné (§2)."""

    def test_the_message_names_the_commits_and_the_assignee(self):
        inputs = _inputs(
            authors=[{"name": "author", "family": "claude"},
                     {"name": "r1", "family": "codex"}],
            commits={"author": ["a" * 40],
                     "r1": ["b" * 40, "c" * 40]})
        text = ff.message(inputs)
        self.assertIn("freeze refused (author-families)", text)
        self.assertIn("assignee author", text)
        self.assertIn("aaaaaaaaaaaa", text)
        self.assertIn("bbbbbbbbbbbb", text)
        self.assertIn("cccccccccccc", text)
        self.assertIn("claude", text)
        self.assertIn("codex", text)
        self.assertIn("§2: retour à l'auteur", text)

    def test_a_corrector_is_named_as_such(self):
        text = ff.message(_inputs(
            authors=[{"name": "author", "family": "deepseek"}],
            correctors=[{"name": "r1", "family": "claude"}],
            commits={"author": ["a" * 40], "r1": ["b" * 40]}))
        self.assertIn("r1", text)
        self.assertIn("reviewer correction", text)

    def test_a_policy_with_too_few_families_names_the_policy_not_an_author(self):
        text = ff.message(_inputs(
            required=2, risk="normal", families=("claude",),
            authors=[{"name": "author", "family": "deepseek"}],
            commits={"author": ["a" * 40]}))
        self.assertIn("policy itself declares fewer families", text)
        self.assertNotIn("aaaa", text)

    def test_the_list_is_truncated(self):
        commits = {"author": ["%040x" % value for value in range(ff.MAX_COMMITS + 2)]}
        text = ff.message(_inputs(authors=[
            {"name": "author", "family": "claude"},
            {"name": "r1", "family": "codex"}], commits=commits))
        self.assertIn("list truncated", text)
        self.assertEqual(text.count("\n  - "), ff.MAX_COMMITS + 1)


class FreezeRefusalTest(_ActivationFixture, PgTestCase):
    """Le refus est écrit par le gel lui-même, sur la vraie lecture des auteurs."""

    def _receipt(self, sha, actor, harness):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("commit:" + sha, sha, actor, "s-" + harness, "t", harness, self.lot))

    def _head(self, families):
        """Une tête base..frozen dont chaque commit est écrit par une famille."""
        # La tête du fixture devient la base : la plage ne contient que les
        # commits que ce test attribue.
        self.base = self.frozen
        self.head_commits = []
        for index, (actor, harness) in enumerate(families):
            self.repo.write("src/ameesh/mail.py", "lot %d\n" % index)
            sha = self.repo.commit("work by %s" % actor, agent=actor)
            self._receipt(sha, actor, harness)
            self.head_commits.append(sha)
            self.frozen = sha
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))

    def _frozen_marks(self):
        return [row for row in work.milestones(self.db, self.lot)
                if row["kind"] == "frozen"]

    def test_three_families_and_two_reviews_refuse_the_freeze(self):
        self._head([("author", "deepseek"), ("r1", "claude"), ("r2", "codex")])
        with self.assertRaises(work.WorkError) as caught:
            self.freeze()
        text = str(caught.exception)
        self.assertIn("freeze refused (author-families)", text)
        self.assertIn("assignee author", text)
        for sha in self.repo.git("rev-list", "--no-merges",
                                 "%s..%s" % (self.base, self.frozen)).split():
            self.assertIn(sha[:12], text)
        self.assertEqual(self._frozen_marks(), [])
        self.assertEqual(self.mailbox(), [])

    def test_one_author_family_leaves_the_freeze_legal(self):
        self._head([("author", "deepseek"), ("author", "deepseek")])
        mark, out = self.freeze()
        self.assertIn(mark["id"], [row["id"] for row in self._frozen_marks()])
        self.assertEqual(out["state"], "designated")

    def test_a_corrector_that_consumes_the_last_free_family_refuses(self):
        self._head([("author", "deepseek"), ("r1", "claude")])
        # r1 est le relecteur désigné du gel précédent : son commit est un
        # correctif de relecteur, pas un commit d'auteur ordinaire.
        self.db.execute("UPDATE work_items SET reviewers = ARRAY['r1'] WHERE id = %s",
                        (self.lot,))
        with self.assertRaises(work.WorkError) as caught:
            self.freeze()
        text = str(caught.exception)
        self.assertIn("r1", text)
        self.assertIn("reviewer correction", text)
        # codex reste la seule famille : insuffisant pour deux avis.
        self.assertIn("1 eligible family", text)
        self.assertEqual(self._frozen_marks(), [])

    def test_the_default_hook_refuses_too(self):
        self._head([("author", "deepseek"), ("r1", "claude"), ("r2", "codex")])
        db = self.connect(canon=self.canon.root, canon_ref="HEAD")
        try:
            with self.assertRaises(work.WorkError):
                work.milestone(db, self.lot, "frozen", sha=self.frozen,
                               actor="author")
        finally:
            db.close()
        self.assertEqual(self._frozen_marks(), [])

    def test_the_designation_inputs_carry_the_author_commits(self):
        self._head([("author", "deepseek"), ("r1", "claude")])
        freeze = {"id": 7, "sha": self.frozen}
        inputs = act.prepare(self.cfg, self.db, self.lot, freeze=freeze,
                             fetch=False)
        self.assertIsInstance(inputs, act.Inputs)
        self.assertEqual(inputs.author_commits["author"], [self.head_commits[0]])
        self.assertEqual(inputs.author_commits["r1"], [self.head_commits[1]])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
