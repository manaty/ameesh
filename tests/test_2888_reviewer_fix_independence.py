# SPDX-License-Identifier: AGPL-3.0-only
"""#2888 — un correctif de relecteur, même petit, fait de lui un auteur.

Canon §2 (home#127 `c5197a00`, « Le relecteur corrige lui-même… ») : une
correction qui laisserait moins de familles indépendantes que d'avis requis
n'est pas recevable. Il n'y a pas d'exception de taille : le seuil de ~50
lignes ne décide que du retour à l'auteur, jamais de l'éligibilité d'une
famille.

Témoin obligatoire du lot : auteur Claude, correcteur Codex (44 lignes),
avis Codex + DeepSeek, deux avis requis -> la porte refuse (seul DeepSeek est
indépendant). Les courriers disent la règle avant la poussée.
"""
from __future__ import annotations

import os
from unittest import TestCase

from ameesh import merge_queue, registry, review_authors, work
from ameesh import review_designation as rd

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

FAMILIES = ("claude", "codex", "deepseek")
LOT = {"id": 2888, "title": "Indépendance des avis : correctif #2670"}
SHA = "a" * 40


class SelectCorrectorFamilyTest(TestCase):
    """La famille du correcteur est une famille d'auteur, pas une exception."""

    def test_the_corrector_family_cannot_cover_the_second_opinion(self):
        # Auteur Claude, correcteur Codex : il ne reste que DeepSeek pour deux
        # avis. Codex (correcteur ou un autre agent de sa famille) est exclu ;
        # #3596 pourvoit tout de suite le siège DeepSeek disponible.
        out = rd.select(
            required=2,
            authors=[{"name": "author", "family": "claude"},
                     {"name": "cx2", "family": "codex"}],
            candidates=[rd.Candidate("cx2", "codex", True, 0),
                        rd.Candidate("cx3", "codex", True, 0),
                        rd.Candidate("dev1", "deepseek", True, 0)],
            families=FAMILIES,
            fixers=[{"name": "cx2", "family": "codex"}],
        )
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["dev1"])
        self.assertEqual(out["missing"], 1)
        self.assertIn("forbidden claude, codex", out["why"])
        self.assertIn("1 eligible family", out["why"])

    def test_a_small_correction_is_receivable_when_a_family_remains(self):
        # Un avis requis, auteur Claude, correcteur Codex : DeepSeek reste une
        # famille indépendante, la correction est donc recevable (canon §2).
        out = rd.select(
            required=1,
            authors=[{"name": "author", "family": "claude"},
                     {"name": "cx2", "family": "codex"}],
            candidates=[rd.Candidate("cx2", "codex", True, 0),
                        rd.Candidate("dev1", "deepseek", True, 0)],
            families=FAMILIES,
            fixers=[{"name": "cx2", "family": "codex"}],
        )
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual([row["name"] for row in out["reviewers"]], ["dev1"])


class ReaderCorrectorTest(PgTestCase):
    """La lecture partagée rend le correcteur auteur ET correcteur."""

    def receipt(self, actor, harness, commits):
        for sha in commits:
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject) DO NOTHING",
                ("commit:" + sha, sha, actor, "s-" + harness, "t", harness,
                 self.lot))

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        for name, harness in (("author", "claude"), ("cx2", "codex"),
                              ("dev1", "deepseek")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        self.item = work.add(self.db, title="preuve #2888", app="ameesh",
                             assignee="author")
        self.lot = int(self.item["id"])
        work.move(self.db, self.lot, "build")

    def build_fix(self, lines=44):
        self.repo.write("src/ameesh/work.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/work.py", "lot\n")
        self.author = self.repo.commit("author: lot")
        self.repo.write("src/ameesh/work.py",
                        "lot\n" + "".join("fix %02d\n" % i for i in range(lines)))
        self.frozen = self.repo.commit("cx2: fix")
        self.receipt("author", "claude", [self.author])
        self.receipt("cx2", "codex", [self.frozen])
        self.db.execute("UPDATE work_items SET branch_target = %s,"
                        " reviewers = ARRAY['cx2'] WHERE id = %s",
                        (self.base, self.lot))
        self.db.execute("UPDATE work_items SET assignee = 'author' WHERE id = %s",
                        (self.lot,))
        work.milestone(self.db, self.lot, "frozen", sha=self.frozen, actor="author")

    def test_a_forty_four_line_fix_is_an_author_and_a_corrector(self):
        self.build_fix()
        authors, unknown, correctors = review_authors.reading(
            self.db, work.get(self.db, self.lot), None,
            [self.author, self.frozen], self.repo.root, FAMILIES)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"author": "claude", "cx2": "codex"})
        self.assertEqual([row["name"] for row in correctors], ["cx2"])


class GateMandatoryWitnessTest(PgTestCase):
    """Auteur Claude + correcteur Codex 44 lignes + avis Codex/DeepSeek."""

    def receipt(self, actor, harness, commits):
        for sha in commits:
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject) DO NOTHING",
                ("commit:" + sha, sha, actor, "s-" + harness, "t", harness,
                 self.lot))

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        self.repo.write("src/ameesh/work.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/work.py", "lot\n")
        self.author = self.repo.commit("author: lot")
        self.repo.write("src/ameesh/work.py",
                        "lot\n" + "".join("fix %02d\n" % i for i in range(44)))
        self.frozen = self.repo.commit("cx2: 44-line fix")
        for name, harness in (("author", "claude"), ("cx2", "codex"),
                              ("cx3", "codex"), ("dev1", "deepseek")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        self.item = work.add(self.db, title="indépendance #2888", app="ameesh",
                             assignee="author")
        self.lot = int(self.item["id"])
        work.move(self.db, self.lot, "build")
        self.db.execute(
            "UPDATE work_items SET branch_target = %s, reviewers = ARRAY['cx2']"
            " WHERE id = %s", (self.base, self.lot))
        self.receipt("author", "claude", [self.author])
        self.receipt("cx2", "codex", [self.frozen])
        work.milestone(self.db, self.lot, "frozen", sha=self.frozen,
                       actor="author")
        for actor in ("cx2", "cx3", "dev1"):
            mark = work.milestone(self.db, self.lot, "verdict", sha=self.frozen,
                                  actor=actor, verdict="ok")
            # #2928 : la porte sensible ne compte un avis que sur son reçu
            # exact ; ces avis sont réels, ils portent leur preuve.
            harness = {"cx2": "codex", "cx3": "codex", "dev1": "deepseek"}[actor]
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject) DO NOTHING",
                ("milestone:%d" % int(mark["id"]), self.frozen, actor,
                 "s-" + harness, "t", harness, self.lot))

    def gate(self):
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/merge_queue.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/merge_queue.py"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        return merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), self.frozen,
            repo=self.repo.root, klass=klass, item=work.get(self.db, self.lot),
            required=2)

    def test_codex_and_deepseek_do_not_make_two_opinions(self):
        out = self.gate()
        self.assertFalse(out.get("error"), out)
        self.assertFalse(out["ok"], out["why"])
        self.assertEqual(out["corrections"], ["cx2"])
        self.assertIn("cx2", {entry["name"] for entry in out["authors"]})
        # cx2 (author of the fix) and cx3 (same codex family) are excluded;
        # only dev1/DeepSeek remains for two required opinions.
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["dev1"])
        self.assertIn("2 independent review verdict(s) required", out["why"])


class MessageCorrectorRuleTest(TestCase):
    """Les courriers disent la règle sans l'ancienne exception de taille."""

    def test_two_opinions_tell_the_reviewer_not_to_push(self):
        text = rd.reviewer_message(
            LOT, freeze={"id": 1, "sha": SHA}, required=2, rank=1,
            reviewer={"name": "cx2", "family": "codex"},
            authors=[{"name": "author", "family": "claude"}],
            eligible_families=2)
        self.assertIn("do not push this correction", text)
        self.assertIn("1 independent family", text)
        self.assertIn("2 required opinion", text)
        self.assertIn("#2888", text)
        self.assertNotIn("keeps your family eligible", text)

    def test_one_opinion_can_leave_a_family(self):
        text = rd.reviewer_message(
            LOT, freeze={"id": 1, "sha": SHA}, required=1, rank=1,
            reviewer={"name": "cx2", "family": "codex"},
            authors=[{"name": "author", "family": "claude"}],
            eligible_families=2)
        self.assertIn("is receivable", text)
        self.assertIn("1 independent family", text)
        self.assertIn("1 required opinion", text)

    def test_the_manual_mail_carries_the_same_rule(self):
        text = rd.manual_reviewer_message(LOT)
        self.assertIn("Any correction makes you an author", text)
        self.assertIn("fewer independent families", text)
        self.assertIn("#2888", text)
