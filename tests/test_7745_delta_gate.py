# SPDX-License-Identifier: AGPL-3.0-only
"""#7745 — la porte de fusion lit l'indépendance par delta d'auteur.

Témoins du lot, sur une vraie plage gelée et une vraie lecture de provenance :

(1) une tête à deux familles d'auteurs (codex puis deepseek) est validable avec
    les trois familles : claude+deepseek couvrent le delta codex, claude+codex
    couvrent le delta deepseek (#7678) ;
(2) si un delta n'a qu'un avis d'une autre famille, la porte refuse et nomme
    le commit fautif — un avis d'une famille d'auteur d'un **autre** delta est
    retenu, jamais écarté en bloc ;
(3) un commit réattribué (#7324) garde la famille prouvée du gel comme famille
    globale : elle ne couvre **aucun** delta, même avec trois familles.
"""
from __future__ import annotations

import os

from ameesh import merge_queue, registry, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

FAMILIES = ("claude", "codex", "deepseek")
CRITICAL = "src/ameesh/merge_queue.py"


class _Fixture(PgTestCase):
    """Un lot gelé sur un vrai commit, avec des reçus de provenance exacts."""

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        self.repo.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.repo.write(CRITICAL, "base\n")
        self.base = self.repo.commit("base")
        self.repo.git("checkout", "-q", "-b", "lot/7745-delta")

    def agent(self, name, harness):
        registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                        " WHERE name = %s", (name,))

    def receipt(self, subject, *, sha=None, actor, harness):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, 't', %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            (subject, self.frozen if sha is None else sha, actor,
             "s-" + harness, harness, self.lot))

    def open_lot(self, assignee):
        self.item = work.add(self.db, title="porte par delta #7745",
                             app="ameesh", assignee=assignee,
                             branch="lot/7745-delta", branch_target=self.base)
        self.lot = int(self.item["id"])
        work.move(self.db, self.lot, "build")
        work.move(self.db, self.lot, "qa")

    def verdict(self, actor, harness):
        mark = work.milestone(self.db, self.lot, "verdict", sha=self.frozen,
                              actor=actor, verdict="ok")
        self.receipt("milestone:%d" % int(mark["id"]), actor=actor,
                     harness=harness)
        return mark

    def marks(self):
        return work.milestones(self.db, self.lot)

    def klass(self):
        cls = "sensitive"
        return {"class": cls, "project": "ameesh", "repo": self.repo.root,
                "target": self.base, "base_sha": self.base,
                "files": [{"path": CRITICAL, "class": cls, "rule": [CRITICAL]}]}

    def gate(self):
        return merge_queue.review_gate(
            self.db, self.marks(), self.frozen, repo=self.repo.root,
            klass=self.klass(), item=work.get(self.db, self.lot),
            families=FAMILIES)


class TwoAuthorFamiliesTest(_Fixture):
    """Témoin (1) : #7678, la tête codex+deepseek relue par les trois familles."""

    def setUp(self):
        super().setUp()
        self.repo.write(CRITICAL, "codex\n")
        self.first = self.repo.commit("codex delta", agent="codex-author")
        self.repo.write(CRITICAL, "deepseek\n")
        self.frozen = self.repo.commit("deepseek delta", agent="ds-author")
        for name, harness in (("codex-author", "codex"), ("ds-author", "deepseek"),
                              ("claude-r", "claude"), ("codex-r", "codex"),
                              ("deepseek-r", "deepseek")):
            self.agent(name, harness)
        self.open_lot("ds-author")
        self.receipt("commit:" + self.first, sha=self.first,
                     actor="codex-author", harness="codex")
        self.receipt("commit:" + self.frozen, actor="ds-author",
                     harness="deepseek")
        work.milestone(self.db, self.lot, "frozen", sha=self.frozen,
                       actor="ds-author")

    def test_the_head_is_valid_with_the_three_families(self):
        for actor, harness in (("claude-r", "claude"), ("codex-r", "codex"),
                               ("deepseek-r", "deepseek")):
            self.verdict(actor, harness)
        out = self.gate()
        self.assertTrue(out["ok"], out)
        rows = {row["sha"]: row for row in out["deltas"]}
        self.assertEqual(set(rows), {self.first, self.frozen})
        self.assertEqual(rows[self.first]["families"], ["codex"])
        self.assertEqual(rows[self.frozen]["families"], ["deepseek"])
        self.assertEqual({row["actor"] for row in out["accepted"]},
                         {"claude-r", "codex-r", "deepseek-r"})
        # Chaque delta est couvert par les deux familles qu'il n'a pas écrites.
        self.assertEqual(rows[self.first]["eligible"], ["claude", "deepseek"])
        self.assertEqual(rows[self.frozen]["eligible"], ["claude", "codex"])

    def test_one_delta_with_only_one_other_family_is_refused(self):
        # codex+claude : le delta codex n'a que claude, il lui manque deepseek.
        self.verdict("claude-r", "claude")
        self.verdict("codex-r", "codex")
        out = self.gate()
        self.assertFalse(out["ok"], out)
        # L'avis codex n'est plus écarté en bloc : il compte pour le delta
        # deepseek, et la porte le montre.
        self.assertIn("codex-r", {row["actor"] for row in out["accepted"]})
        self.assertIn(self.first[:12], out["why"])
        rows = {row["sha"]: row for row in out["deltas"]}
        self.assertTrue(rows[self.frozen]["covered"])
        self.assertFalse(rows[self.first]["covered"])
        self.assertEqual(rows[self.first]["missing"], 1)
        self.assertEqual(rows[self.first]["eligible"], ["claude"])


class ReassignedCommitTest(_Fixture):
    """Témoin (3) : #7324, la famille du gel reste globale, donc interdite partout."""

    def setUp(self):
        super().setUp()
        self.repo.write(CRITICAL, "signed ds\n")
        self.frozen = self.repo.commit("signed deepseek", agent="ds-author")
        stamp = float(self.repo.git("log", "-1", "--format=%ct", self.frozen))
        for name, harness in (("ds-author", "deepseek"),
                              ("codex-author", "codex"),
                              ("claude-r", "claude"), ("codex-r", "codex"),
                              ("deepseek-r", "deepseek")):
            self.agent(name, harness)
        # Le commit n'a aucun reçu : son identité Git (ds-author) est prouvée
        # par le tour qui le couvre ; le gel, lui, est prouvé de codex-author.
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, session, recorded_at)"
            " VALUES ('ds-author', 'deepseek', 's', to_timestamp(%s))",
            (stamp + 60,))
        self.open_lot("ds-author")
        frozen = work.milestone(self.db, self.lot, "frozen", sha=self.frozen,
                                actor="codex-author")
        self.receipt("milestone:%d" % int(frozen["id"]), actor="codex-author",
                     harness="codex")

    def test_the_freezer_family_covers_no_delta(self):
        for actor, harness in (("claude-r", "claude"), ("codex-r", "codex"),
                               ("deepseek-r", "deepseek")):
            self.verdict(actor, harness)
        out = self.gate()
        self.assertFalse(out["ok"], out)
        # deepseek (identité du commit) + codex (reçu du gel) ferment le
        # delta : seul claude reste recevable, il manque un second avis.
        self.assertEqual(out["deltas"][0]["families"], ["codex", "deepseek"])
        self.assertEqual(out["deltas"][0]["eligible"], ["claude"])
        self.assertFalse(out["deltas"][0]["covered"])
        self.assertIn("codex, deepseek", out["why"])
