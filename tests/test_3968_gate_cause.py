# SPDX-License-Identifier: AGPL-3.0-only
"""#3968 — le refus de la porte nomme sa cause : famille d'auteur ou siège.

nexlink #3882 : la tête gelée e6a03e848 n'a que des commits de claude2 et
deepseek3, tous deux de la famille deepseek. La désignation au gel affichait
pourtant « forbidden codex, deepseek » : codex n'était pas une famille
d'auteur, mais la famille du siège **manuel** codex1, désigné sur la tête
précédente et toujours compté. Le même mot « forbidden » faisait conclure à un
défaut d'attribution (#3584) et à un avis codex perdu par la porte.

Témoins :

- la sélection #3882 distingue `author_families` (deepseek) des familles des
  sièges déjà pourvus (codex1, codex) et le dit dans son refus ;
- la porte accepte les OK codex quand aucun commit codex n'est auteur de la
  tête (elle ne les exclut donc pas « comme auteurs ») ;
- quand un avis est écarté parce que sa famille est celle d'un auteur, le
  refus nomme l'acteur, sa famille et la cause ;
- quand la porte se ferme assez tôt pour ne pas relire Git, elle nomme la
  lecture d'auteurs que l'appelant lui a déjà fournie, au lieu d'affirmer
  « no author reading » (ce que la file #3928 affichait).
"""
from __future__ import annotations

import os
from unittest import TestCase

from ameesh import merge_queue, registry, review_designation as rd, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

FAMILIES = ("claude", "codex", "deepseek")


class SelectionCauseTest(TestCase):
    """La désignation ne confond plus famille d'auteur et famille de siège."""

    def select_3882(self):
        return rd.select(
            required=2, families=FAMILIES,
            authors=[{"name": "claude2", "family": "deepseek"},
                     {"name": "deepseek3", "family": "deepseek"}],
            candidates=[rd.Candidate("codex-one", "codex", True, 0),
                        rd.Candidate("deepseek-one", "deepseek", True, 0)],
            already=[{"name": "codex1", "family": "codex"}])

    def test_3882_a_manual_seat_is_not_an_author_family(self):
        out = self.select_3882()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertEqual(out["missing"], 1, out)
        # La preuve structurée est déjà bonne : seule la phrase du refus
        # mélangeait les deux ensembles.
        self.assertEqual(out["forbidden"], ["deepseek"], out)
        self.assertEqual(out["author_families"], ["deepseek"], out)
        self.assertEqual(out["seat_families"], ["codex"], out)
        self.assertEqual(out["seats"], [{"name": "codex1", "family": "codex"}], out)
        self.assertIn("forbidden author families: deepseek", out["why"])
        self.assertIn("already covered by seat(s): codex1 (codex)", out["why"])
        self.assertNotIn("forbidden codex", out["why"])

    def test_3882_the_forbidden_list_keeps_every_proven_author_family(self):
        """#3584 n'est pas contourné : une famille d'auteur reste interdite."""
        out = rd.select(
            required=2, families=FAMILIES,
            authors=[{"name": "claude2", "family": "deepseek"},
                     {"name": "codex2", "family": "codex"}],
            candidates=[rd.Candidate("codex-one", "codex", True, 0),
                        rd.Candidate("dev-one", "claude", True, 0)],
            already=[{"name": "dev1", "family": "claude"}])
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertEqual(out["author_families"], ["codex", "deepseek"], out)
        self.assertEqual(out["seat_families"], ["claude"], out)
        self.assertIn("forbidden author families: codex, deepseek", out["why"])
        self.assertIn("already covered by seat(s): dev1 (claude)", out["why"])


class GateCauseTest(PgTestCase):
    """La porte de fusion : aucun codex auteur sur #3882, avis compté."""

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        self.repo.write("src/ameesh/merge_queue.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/merge_queue.py", "base\na\n")
        self.first = self.repo.commit("claude2: a", agent="claude2")
        self.repo.write("src/ameesh/merge_queue.py", "base\na\nb\n")
        self.frozen = self.repo.commit("deepseek3: b", agent="deepseek3")
        for name, harness in (("claude2", "deepseek"), ("deepseek3", "deepseek"),
                              ("codex1", "codex"), ("codex2", "codex"),
                              ("orchestrateur", "claude")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        self.item = work.add(self.db, title="Porte #3968", app="ameesh",
                             assignee="claude2")
        self.lot = int(self.item["id"])
        work.move(self.db, self.lot, "build")
        self.receipt("claude2", "deepseek", [self.first])
        self.receipt("deepseek3", "deepseek", [self.frozen])
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        work.milestone(self.db, self.lot, "frozen", sha=self.frozen, actor="claude2")

    # -- helpers -----------------------------------------------------------

    def receipt(self, actor, harness, commits):
        for sha in commits:
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject) DO NOTHING",
                ("commit:" + sha, sha, actor, "s-" + actor, "t-" + sha[:8],
                 harness, self.lot))

    def verdict_receipt(self, mark, harness):
        """#2928 : la porte sensible ne compte un verdict que sur son reçu."""
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("milestone:%d" % int(mark["id"]), self.frozen, mark["actor"],
             "s-" + harness, "t-v", harness, self.lot))

    def verdict(self, actor, harness, *, sha=None):
        mark = work.milestone(self.db, self.lot, "verdict",
                              sha=sha or self.frozen, actor=actor, verdict="ok")
        self.verdict_receipt(mark, harness)
        return mark

    def klass(self):
        return {"class": "sensitive", "doubt": False,
                "files": [{"path": "src/ameesh/merge_queue.py", "class": "sensitive",
                           "rule": ["src/ameesh/merge_queue.py"]}],
                "repo": self.repo.root, "target": self.base,
                "base_sha": self.base}

    def gate(self, **kwargs):
        return merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), self.frozen,
            repo=self.repo.root, klass=self.klass(),
            item=work.get(self.db, self.lot), **kwargs)

    # -- témoins -----------------------------------------------------------

    def test_3882_a_codex_ok_counts_when_no_codex_commit_authors_the_head(self):
        """Le défaut d'attribution supposé n'existe pas à la porte."""
        self.verdict("orchestrateur", "claude")
        self.verdict("codex1", "codex")
        out = self.gate()
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual(out["author_families"], ["deepseek"], out)
        self.assertEqual({row["name"] for row in out["authors"]},
                         {"claude2", "deepseek3"}, out)
        self.assertEqual({row["actor"] for row in out["accepted"]},
                         {"orchestrateur", "codex1"}, out)

    def test_a_verdict_excluded_by_an_author_family_is_named(self):
        """Un vrai auteur codex exclut l'avis codex, et le refus le dit."""
        self.repo.write("src/ameesh/merge_queue.py", "base\na\nb\nc\n")
        self.frozen = self.repo.commit("codex2: c", agent="codex2")
        self.receipt("codex2", "codex", [self.frozen])
        work.milestone(self.db, self.lot, "frozen", sha=self.frozen, actor="claude2")
        self.verdict("orchestrateur", "claude")
        self.verdict("codex1", "codex")
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["author_families"], ["codex", "deepseek"], out)
        self.assertEqual([row["actor"] for row in out["accepted"]],
                         ["orchestrateur"], out)
        self.assertIn("author families of this head: codex, deepseek", out["why"])
        self.assertIn("codex1 (codex): family of an author of the head", out["why"])

    def test_the_early_refusal_names_the_author_reading_it_was_given(self):
        """La file #3928 ne lit plus « no author reading » après avoir lu."""
        self.verdict("orchestrateur", "claude")
        out = self.gate(
            authors=[{"name": "claude2", "family": "deepseek"},
                     {"name": "deepseek3", "family": "deepseek"}],
            authors_complete=True)
        self.assertFalse(out["ok"], out)
        self.assertIn("author reading of this head: claude2 (deepseek),"
                      " deepseek3 (deepseek)", out["why"])
        self.assertIn("not on this head", out["why"])
        self.assertNotIn("no author reading", out["why"])
        self.assertEqual(out["author_families"], ["deepseek"], out)

    def test_the_early_refusal_without_a_reading_keeps_its_old_cause(self):
        self.verdict("orchestrateur", "claude")
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertIn("no author reading", out["why"])
