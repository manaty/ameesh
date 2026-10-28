# SPDX-License-Identifier: AGPL-3.0-only
"""#4179 — un commit tiers hérité puis retiré ne ferme plus sa famille.

Cas réel : la pile A2 → A3. Le correctif du relecteur `r1` (famille claude) a
été retiré du parent A2 par un revert ; A3 empile son propre travail sur la
tête A2, donc la plage `main..A3` contient encore le commit retiré. Le gel A3
était refusé `author-families` : ce commit, tiers pour A3 (r1 n'y est pas
relecteur désigné), était compté comme auteur et la famille claude devenait
inéligible. La porte doit lire la survie du contenu pour un tiers comme pour un
correcteur (#3335) : le contenu retiré ne ferme plus sa famille, une
contribution qui survit la ferme toujours.
"""
from __future__ import annotations

from ameesh import review_authors, review_designation as rd
from ameesh import review_designation_activation as act
from ameesh import work

from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture

FAMILIES = ("claude", "codex", "deepseek")


class _ThirdPartyFixture(_ActivationFixture):
    """Une plage main..tête qui contient le commit d'un tiers r1 (claude)."""

    def setUp(self):
        super().setUp()
        # `self.frozen` initial est le commit de l'auteur, déjà reçu deepseek.
        self.author_commit = self.frozen
        self.repo.write("src/ameesh/mail.py",
                        "lot\nif not admitted:\n    return deferred  # r1\n")
        self.third = self.repo.commit("r1 contribution", agent="r1")
        self.receipts(commits=[self.third], actor="r1", harness="claude")

    def remove_third(self, keep: str = ""):
        self.repo.write("src/ameesh/mail.py", "lot\n" + keep)
        self.revert = self.repo.commit("author removes r1 content", agent="author")
        self.receipts(commits=[self.revert])
        self.frozen = self.revert
        return self.revert

    def inputs(self):
        return act.prepare(self.cfg, self.db, self.lot,
                           freeze={"id": 7, "sha": self.frozen}, fetch=False)

    def read(self, *, head: str, commits=None):
        return review_authors.reading(
            self.db, work.get(self.db, self.lot), None,
            commits or self.repo.git("rev-list", "--no-merges",
                                     "%s..%s" % (self.base, head)).split(),
            self.repo.root, FAMILIES, base=self.base, head=head)


class RemovedThirdPartyTest(_ThirdPartyFixture, PgTestCase):
    """Le contenu tiers retiré : la famille claude reste éligible, le gel passe."""

    def setUp(self):
        super().setUp()
        self.remove_third()

    def test_the_removed_third_party_commit_is_not_an_author(self):
        inputs = self.inputs()
        self.assertIsInstance(inputs, act.Inputs, inputs)
        self.assertEqual({row["name"] for row in inputs.authors}, {"author"})
        self.assertNotIn("r1", inputs.author_commits, inputs.author_commits)

    def test_the_freeze_is_accepted_and_designates_reviewers(self):
        mark, out = self.freeze()
        self.assertIn(mark["id"], [row["id"] for row in self.db.query(
            "SELECT id FROM work_item_milestones WHERE work_item_id = %s"
            " AND kind = 'frozen'", (self.lot,))])
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertTrue(self.db.query(
            "SELECT 1 FROM work_item_milestones WHERE work_item_id = %s"
            " AND kind = 'frozen'", (self.lot,)))

    def test_the_designation_can_reuse_the_claude_family(self):
        inputs = self.inputs()
        self.assertIsInstance(inputs, act.Inputs, inputs)
        out = rd.select(
            required=2, authors=inputs.authors, fixers=inputs.correctors,
            candidates=[rd.Candidate("r1", "claude", True, 0),
                        rd.Candidate("r2", "codex", True, 0)],
            families=FAMILIES)
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertIn("claude", {row["family"] for row in out["reviewers"]}, out)


class SurvivingThirdPartyTest(_ThirdPartyFixture, PgTestCase):
    """La contribution tiers qui survit garde sa famille exclue (porte fermée)."""

    def setUp(self):
        super().setUp()
        # Un commit de l'auteur après r1, sans toucher à sa contribution.
        self.repo.write("src/ameesh/mail.py",
                        "lot\nif not admitted:\n    return deferred  # r1\n"
                        "# A3 work\n")
        self.after = self.repo.commit("author work after r1", agent="author")
        self.receipts(commits=[self.after])
        self.frozen = self.after

    def test_the_surviving_third_party_commit_is_an_author(self):
        inputs = self.inputs()
        self.assertIsInstance(inputs, act.Inputs, inputs)
        self.assertIn("r1", {row["name"] for row in inputs.authors})
        self.assertIn("r1", inputs.author_commits)

    def test_the_freeze_is_still_refused(self):
        with self.assertRaises(work.WorkError) as caught:
            self.freeze()
        text = str(caught.exception)
        self.assertIn("freeze refused (author-families)", text)
        self.assertIn("r1", text)
        self.assertEqual(self.db.query(
            "SELECT 1 FROM work_item_milestones WHERE work_item_id = %s"
            " AND kind = 'frozen'", (self.lot,)), [])


class PartlySurvivingThirdPartyTest(_ThirdPartyFixture, PgTestCase):
    """Une seule ligne significative qui survit suffit à garder l'auteur."""

    def setUp(self):
        super().setUp()
        self.repo.write("src/ameesh/mail.py", "lot\nif not admitted:\n")
        self.remove_third(keep="if not admitted:\n")

    def test_one_surviving_line_keeps_the_third_party_family(self):
        inputs = self.inputs()
        self.assertIsInstance(inputs, act.Inputs, inputs)
        self.assertIn("r1", {row["name"] for row in inputs.authors})


class ReintroducedThirdPartyTest(_ThirdPartyFixture, PgTestCase):
    """Le contenu retiré puis réintroduit survit : la famille reste exclue."""

    def setUp(self):
        super().setUp()
        self.remove_third()
        self.repo.write("src/ameesh/mail.py",
                        "lot\nif not admitted:\n    return deferred  # r1\n")
        self.back = self.repo.commit("author reintroduces r1 content", agent="author")
        self.receipts(commits=[self.back])
        self.frozen = self.back

    def test_the_reintroduced_content_keeps_the_author(self):
        inputs = self.inputs()
        self.assertIsInstance(inputs, act.Inputs, inputs)
        self.assertIn("r1", {row["name"] for row in inputs.authors})


class UnreadableHeadTest(_ThirdPartyFixture, PgTestCase):
    """Une tête illisible est un doute : le tiers reste auteur."""

    def test_an_unreadable_head_keeps_the_third_party_author(self):
        authors, _unknown, _correctors = self.read(
            head="0" * 40, commits=[self.author_commit, self.third])
        self.assertIn("r1", {row["name"] for row in authors})


class StackedParentReadingTest(_ThirdPartyFixture, PgTestCase):
    """La lecture de la pile A3 ne compte pas le commit A2 déjà retiré.

    Même forme que le cas réel : un parent empilé (commits de l'auteur, puis
    r1) dont le contenu tiers est retiré avant la tête enfant. La lecture de
    l'enfant est celle de la plage `main..tête` ; le commit retiré du parent
    reste dans l'intervalle mais n'est plus un contenu de la tête.
    """

    def setUp(self):
        super().setUp()
        self.remove_third()
        self.repo.write("src/ameesh/mail.py", "lot\n# A3 own work\n")
        self.head = self.repo.commit("A3 work on top of parent", agent="author")
        self.receipts(commits=[self.head])

    def test_the_child_head_reads_only_its_own_author(self):
        authors, unknown, _correctors = self.read(head=self.head)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"] for row in authors}, {"author"})
