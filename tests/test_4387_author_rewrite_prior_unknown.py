# SPDX-License-Identifier: AGPL-3.0-only
"""#4387 — l'auteur qui réécrit le correctif d'un gel antérieur le reprend.

Cas réel #4164 (gel #24375, tête 5844df4aa44e) : 531d70f, commit de
mesh-design gelé en #23923, n'est plus ancêtre de la tête ; dev2 l'a réécrit
à son nom (canon §2) avec un reçu exact et un patch-id verbatim identique.
La lecture des gels antérieurs le gardait comme inconnu, la désignation
attendait et le lot ne pouvait plus entrer en paquet.

La règle est celle de #3301, bornée à l'auteur de référence : le commit
d'un gel antérieur sans reçu, réémis **par l'auteur** de la tête avec un
reçu exact et le même patch, n'ajoute plus d'inconnu. Un autre auteur ne
peut toujours pas officialiser le même patch (#3301).
"""
from ameesh import registry, review_authors, work
from ameesh import review_designation_activation as act

from .test_2924_history_rewrite_provenance import FAMILIES, _RewriteFixture


class AuthorRewritePriorUnknownTest(_RewriteFixture):
    """Gel A = correctif d'un tiers sans reçu ; gel B = réécriture de l'auteur."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "dev2", harness="claude", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                        " WHERE name = %s", ("dev2",))

    def _build_head_a(self):
        self.repo.write("src/ameesh/runner.py", "base_call()\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/runner.py",
                        "base_call()\noriginal_guard()\n")
        self.original = self.repo.commit("impl: original", agent="impl")
        self.receipt("impl", "deepseek", [self.original])
        # Gel A : le correctif a été poussé hors tour par un tiers (dev1) ;
        # aucun reçu exact ce commit.
        tree = self.repo.git("rev-parse", self.original + "^{tree}")
        self.unreceived = self.repo.git(
            "commit-tree", tree, "-p", self.base,
            "-m", "dev1: correctif sans reçu", env=self.env("dev1"))
        self.head_a = self.unreceived
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        # L'acteur du gel A est l'auteur de sa tête (cas réel : mesh-design
        # gèle 531d70f) : c'est cette provenance que l'auteur réécrit.
        work.milestone(self.db, self.lot, "frozen", sha=self.head_a,
                       actor="dev1", repo=self.repo.root)

    def replace(self, *, actor="impl", receipt=True, extra=""):
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("src/ameesh/runner.py",
                        "base_call()\noriginal_guard()\n" + extra)
        self.head_b = self.repo.commit("received replacement", agent=actor)
        if receipt:
            harness = {"impl": "deepseek", "dev1": "claude",
                       "dev2": "claude"}[actor]
            self.receipt(actor, harness, [self.head_b])
        self.freeze_b = self.freeze(self.head_b)

    def read(self, *, strict=False):
        commits = review_authors._commits_between(self.repo.root, self.base,
                                                  self.head_b)
        return review_authors.reading(
            self.db, self.lot_item(), self.freeze_b, commits, self.repo.root,
            FAMILIES, base=self.base, head=self.head_b, strict=strict)

    def test_author_reemission_clears_the_prior_unknown(self):
        self.replace()
        authors, unknown, _ = self.read(strict=True)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek"})

    def test_designation_accepts_the_author_reemission(self):
        self.replace()
        authors, unknown, correctors = act.attribution_of_head(
            self.db, self.lot_item(), self.freeze_b, self.repo.root,
            self.base, self.head_b, families=FAMILIES)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"] for row in authors}, {"impl"})
        # §2 : le correcteur dont l'auteur reprend le petit correctif reste
        # exclu par son nom ; sa famille (claude) redevient éligible.
        rows = {row["name"]: row for row in correctors}
        self.assertIn("dev1", rows)
        self.assertEqual(rows["dev1"]["family"], "")

    def test_another_author_cannot_launder_the_prior_patch(self):
        self.replace(actor="dev2")
        self.assertIn(self.unreceived[:12], self.read(strict=True)[1])

    def test_author_reemission_without_receipt_keeps_the_unknown(self):
        self.replace(receipt=False)
        self.assertIn(self.unreceived[:12], self.read(strict=True)[1])

    def test_partial_reemission_keeps_the_unknown(self):
        self.replace(extra="    extra_line_not_in_the_old_patch()\n")
        self.assertIn(self.unreceived[:12], self.read(strict=True)[1])

    def test_a_large_prior_head_keeps_its_unknown(self):
        # #4387 (avis auditeur B2) : le §2 ne reprend qu'un petit correctif
        # sans conception ; une tête de gel plus grosse garde son inconnu.
        text = "base_call()\n" + "".join(
            "large_guard_%03d()\n" % index
            for index in range(review_authors.REVIEWER_FIX_LINES + 1))
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("src/ameesh/runner.py", text)
        self.repo.commit("dev1: large correctif", agent="dev1")
        large = self.repo.git("rev-parse", "HEAD")
        work.milestone(self.db, self.lot, "frozen", sha=large,
                       actor="dev1", repo=self.repo.root)
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("src/ameesh/runner.py", text)
        head = self.repo.commit("impl: reprise du large correctif", agent="impl")
        self.receipt("impl", "deepseek", [head])
        freeze_b = self.freeze(head)
        commits = review_authors._commits_between(self.repo.root, self.base, head)
        _authors, unknown, _ = review_authors.reading(
            self.db, self.lot_item(), freeze_b, commits, self.repo.root,
            FAMILIES, base=self.base, head=head)
        self.assertIn(large[:12], unknown)
