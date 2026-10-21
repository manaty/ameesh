# SPDX-License-Identifier: AGPL-3.0-only
"""#2961 — la famille de l'auteur de référence se lit à la trace du gel.

#2892 (gel 81e20300) a été écrit par mesh-design en session codex ; le registre
courant le donne aujourd'hui claude. La désignation (review_authors.
reference_author) et le train lisent codex ; la porte du composeur prenait la
famille au registre et excluait donc l'avis claude d'ameesh-dev1, refusant #2892
à tort. La famille de la référence est celle de la trace du gel (reçu du jalon
de gel, sinon journal des tours au gel), la même que la désignation.

Témoins du lot :
(1) auteur codex au gel, claude au registre, avis deepseek + claude : accepté ;
(2) auteur claude au gel et au registre, avis claude : refusé comme avant ;
(3) reçu du gel absent : repli sur le journal des tours au gel, jamais le
    registre courant ;
(4) reçu contradictoire en mode strict : refus.
"""
from __future__ import annotations

import os

from ameesh import merge_queue, registry, review_authors, review_provenance, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

FAMILIES = ("claude", "codex", "deepseek")
CRITICAL = "src/ameesh/merge_queue.py"


class _Fixture(PgTestCase):
    """Un lot gelé sur un vrai commit, un registre dont le harnais a changé."""

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        self.repo.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.repo.write("src/ameesh/merge_queue.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.git("checkout", "-q", "-b", "lot/2961-reference")
        self.repo.write("src/ameesh/merge_queue.py", "lot\n")
        self.frozen = self.repo.commit("lot", agent="mesh-design")
        # mesh-design est aujourd'hui claude au registre : c'est le piège de
        # #2892. La trace du gel, elle, dira codex.
        for name, harness in (("mesh-design", "claude"), ("qa", "deepseek"),
                              ("dev1", "claude"), ("other", "codex")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        self.item = work.add(self.db, title="famille de référence au gel",
                             app="ameesh", assignee="mesh-design",
                             branch="lot/2961-reference",
                             branch_target=self.base)
        self.lot = int(self.item["id"])
        work.move(self.db, self.lot, "build")
        work.move(self.db, self.lot, "qa")

    # -- helpers ---------------------------------------------------------
    def freeze(self, actor="mesh-design"):
        return work.milestone(self.db, self.lot, "frozen", sha=self.frozen,
                              actor=actor)

    def receipt(self, subject, *, actor="mesh-design", harness="codex"):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, 't', %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            (subject, self.frozen, actor, "s-" + harness, harness, self.lot))

    def turn(self, harness, agent="mesh-design", seconds=1):
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, session, recorded_at)"
            " VALUES (%s, %s, %s, now() + make_interval(secs => %s))",
            (agent, harness, "s-" + harness, float(seconds)))

    def verdict(self, actor):
        work.milestone(self.db, self.lot, "verdict", sha=self.frozen,
                       actor=actor, verdict="ok")

    def marks(self):
        return work.milestones(self.db, self.lot)

    def klass(self, path=CRITICAL):
        cls = "sensitive"
        return {"class": cls, "project": "ameesh", "repo": self.repo.root,
                "target": self.base, "base_sha": self.base,
                "files": [{"path": path, "class": cls, "rule": [path]}]}

    def gate(self, klass=None):
        return merge_queue.review_gate(
            self.db, self.marks(), self.frozen, repo=self.repo.root,
            klass=klass or self.klass(), item=work.get(self.db, self.lot),
            families=FAMILIES)


class ComposerGateTest(_Fixture):
    """Témoin (1) : le harnais a changé depuis le gel, la porte lit le gel."""

    def setUp(self):
        super().setUp()
        mark = self.freeze()
        self.receipt("commit:" + self.frozen)
        self.receipt("milestone:%d" % mark["id"])
        self.turn("codex")
        self.verdict("qa")
        self.verdict("dev1")

    def test_the_composer_gate_reads_the_freeze_family(self):
        out = self.gate()
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual(out["author_family"], "codex")
        self.assertEqual(out["authors"], [{"name": "mesh-design", "family": "codex"}])
        self.assertEqual([row["actor"] for row in out["accepted"]], ["qa", "dev1"])

    def test_review_gate_for_item_reads_the_same_trace(self):
        gate = merge_queue.review_gate_for_item(
            self.db, self.cfg, work.get(self.db, self.lot), self.marks())
        self.assertIsNotNone(gate)
        self.assertTrue(gate["ok"], gate)
        self.assertEqual(gate["author_family"], "codex")
        self.assertEqual([row["actor"] for row in gate["accepted"]], ["qa", "dev1"])


class SameFamilyGateTest(_Fixture):
    """Témoin (2) : un auteur claude au gel et au registre exclut claude."""

    def setUp(self):
        super().setUp()
        mark = self.freeze()
        self.receipt("commit:" + self.frozen, harness="claude")
        self.receipt("milestone:%d" % mark["id"], harness="claude")
        self.turn("claude")
        self.verdict("dev1")

    def test_a_verdict_of_the_author_family_is_still_refused(self):
        out = self.gate(self.klass(path="src/ameesh/mail.py"))
        self.assertFalse(out["ok"], out["why"])
        self.assertEqual(out["author_family"], "claude")
        self.assertEqual(out["accepted"], [])


class TurnJournalFallbackTest(_Fixture):
    """Témoin (3) : sans reçu du gel, le journal des tours au gel fait foi."""

    def setUp(self):
        super().setUp()
        self.freeze()
        # Aucun reçu (commit ou jalon) : seule la trace du tour au gel existe.
        self.turn("codex")
        self.verdict("qa")
        self.verdict("dev1")

    def test_the_freeze_turn_beats_the_current_registry(self):
        out = self.gate()
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual(out["author_family"], "codex")
        self.assertNotEqual(out["author_family"], "claude")
        self.assertEqual([row["actor"] for row in out["accepted"]], ["qa", "dev1"])


class FreezeReceiptFallbackTest(_Fixture):
    """Le reçu du jalon de gel donne la famille quand le journal est muet."""

    def setUp(self):
        super().setUp()
        mark = self.freeze()
        self.receipt("milestone:%d" % mark["id"])
        self.verdict("qa")
        self.verdict("dev1")

    def test_the_freeze_receipt_is_the_family_of_reference(self):
        out = self.gate()
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual(out["author_family"], "codex")
        self.assertEqual([row["actor"] for row in out["accepted"]], ["qa", "dev1"])


class ContradictoryReceiptTest(_Fixture):
    """Témoin (4) : un reçu contradictoire refuse en mode strict."""

    def setUp(self):
        super().setUp()
        self.mark = self.freeze()
        self.receipt("milestone:%d" % self.mark["id"], actor="other")
        self.verdict("qa")
        self.verdict("dev1")

    def test_reference_author_strict_refuses_the_contradiction(self):
        item = work.get(self.db, self.lot)
        with self.assertRaises(review_provenance.ProvenanceError):
            review_authors.reference_author(
                self.db, item, self.mark, FAMILIES, strict=True)
        # La désignation, non stricte, attend : elle ne suppose pas la famille.
        loose = review_authors.reference_author(
            self.db, item, self.mark, FAMILIES)
        self.assertEqual(loose["name"], "mesh-design")
        self.assertEqual(loose["family"], "")

    def test_the_gate_refuses_the_contradictory_freeze_receipt(self):
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertIn("contradictory", (out.get("error") or "")
                      + (out.get("why") or ""))


if __name__ == "__main__":
    import unittest

    unittest.main()
