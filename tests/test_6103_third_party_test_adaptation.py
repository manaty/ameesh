# SPDX-License-Identifier: AGPL-3.0-only
"""#6103: a third party's test adaptation excludes the person, not the family.

Canon §2: « adaptation de test : aucun avis ». The #3518/#3577 exception of a
reviewer's test-only correction also covers a third party (neither the
reference nor a designated reviewer): its name is excluded, its family stays
eligible. One product path keeps it an author of the head.
"""
from __future__ import annotations

from ameesh import registry, work
from . import test_3518_test_only_corrector as base


class ThirdPartyTestAdaptationTest(base.TestOnlyCorrectorTest):

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "md", harness="claude", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = 'md'")

    def adapt(self, paths):
        for path in paths:
            self.repo.write(path, "adaptation\n")
        commit = self.repo.commit("third-party adaptation")
        self.receipt("md", "claude", [commit])
        return commit

    def freeze_at(self, sha):
        self.frozen = sha
        work.milestone(self.db, self.lot, "frozen", sha=sha, actor="author")
        for name, family in (("cx3", "codex"), ("dev1", "claude"), ("md", "claude")):
            mark = work.milestone(self.db, self.lot, "verdict", sha=sha,
                                  actor=name, verdict="ok")
            self.verdict_receipt(mark, family)

    def test_third_party_test_adaptation_keeps_its_family_eligible(self):
        self.freeze_at(self.adapt(["tests/test_product.py"]))
        out = self.gate()
        self.assertTrue(out["ok"], out)
        self.assertNotIn("md", {row["name"] for row in out["authors"]})
        accepted = {row["actor"] for row in out["accepted"]}
        self.assertIn("dev1", accepted)
        self.assertNotIn("md", accepted)

    def test_third_party_product_change_excludes_its_family(self):
        # #7745 : l'exclusion de famille se lit par delta. md/claude a écrit un
        # delta produit : sa famille (claude) est exclue de **ce** delta, qui
        # reste couvert par le seul codex et fait refuser la porte ; l'avis de
        # dev1/claude reste retenu pour le delta de l'auteur (deepseek). La
        # règle globale « un produit tiers exclut sa famille de toute la tête »
        # est remplacée, pas affaiblie : md n'est jamais relecteur de son
        # propre delta, et la porte refuse tant qu'il y manque un avis.
        self.freeze_at(self.adapt(["tests/test_product.py", "src/ameesh/work.py"]))
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertIn("md", {row["name"] for row in out["authors"]})
        claude_deltas = [row for row in out["deltas"]
                         if "claude" in row["families"]]
        self.assertEqual(len(claude_deltas), 1)
        self.assertFalse(claude_deltas[0]["covered"])
        self.assertEqual(claude_deltas[0]["missing"], 1)
        self.assertNotIn("claude", claude_deltas[0]["eligible"])
        self.assertNotIn("md", {row["actor"] for row in out["accepted"]})

    def test_carried_adaptation_from_a_prior_freeze_keeps_family_eligible(self):
        self.freeze_at(self.adapt(["tests/test_product.py"]))
        tree = self.repo.git("rev-parse", "HEAD^{tree}")
        squashed = self.repo.git("commit-tree", tree, "-p", self.base,
                                 "-m", "consolidated lot and adaptation")
        self.repo.git("reset", "--hard", squashed)
        self.receipt("author", "deepseek", [squashed])
        self.freeze_at(squashed)
        out = self.gate()
        self.assertTrue(out["ok"], out)
        self.assertNotIn("md", {row["name"] for row in out["authors"]})
        accepted = {row["actor"] for row in out["accepted"]}
        self.assertIn("dev1", accepted)
        self.assertNotIn("md", accepted)


# the inherited #3518 cases are run by their own module
for _name in [name for name in vars(base.TestOnlyCorrectorTest)
              if name.startswith("test_")]:
    setattr(ThirdPartyTestAdaptationTest, _name, None)
