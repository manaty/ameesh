# SPDX-License-Identifier: AGPL-3.0-only
"""#5197 — une note de relecture sur la branche review/* n'est pas un correctif réécrit.

Constat (nexlink #5172, paquet 311, 31/10) : deepseek5 relit le gel F1, pousse
son rapport `docs/review/…-deepseek5.md` sur sa propre branche
`review/deepseek5-5172` (puis l'amende), et rend ok sur la tête suivante. La
porte le comptait « correcteur de la tête » : son commit descend de F1 et n'est
pas dans la tête, la forme exacte d'un correctif réécrit (#4457). Or rien de ce
commit n'appartient au lot. Les deux preuves sont requises pour ne pas
l'exclure : le commit est sur `review/<relecteur>-<lot>`, et il ne touche que des
fichiers absents de la tête relue et de la tête lue. Un correctif d'un fichier
du lot, ou un commit hors de la branche de relecture, reste exclu (#4457).
"""
from __future__ import annotations

from ameesh import merge_queue, registry, review_authors, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture

FAMILIES = ("claude", "codex", "deepseek")
NOTE = "docs/review/2026-10-31-cx3.md"


class ReviewBranchNoteTest(_ActivationFixture, PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "cx3", harness="codex", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        ("cx3",))

    def commit_receipt(self, actor, harness, sha):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id, at)"
            " VALUES (%s, %s, %s, %s, %s, %s, NULL, now() - interval '10 minutes')"
            " ON CONFLICT (subject) DO NOTHING",
            ("commit:" + sha, sha, actor, "s-" + harness, "t", harness))

    def verdict_receipt(self, mark, harness, sha):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (subject) DO NOTHING",
            ("milestone:%d" % int(mark["id"]), sha, mark["actor"], "s-" + harness,
             "t", harness, self.lot))

    def scenario(self, *, branch=True, path=NOTE, amend=False, kept_by_author=False):
        """F1 ; cx3 pousse sur sa branche (ou non) ; l'auteur repart de F1 ; F2."""
        head_a = self.frozen
        self.freeze()                                     # F1
        self.repo.write(path, "cx3 : rapport du 1er tour\n")
        note = self.repo.commit("cx3: docs(review) 1er tour", agent="cx3")
        self.commit_receipt("cx3", "codex", note)
        if amend:
            self.repo.write(path, "cx3 : rapport du 1er tour, précisé\n")
            note = self.repo.commit("cx3: docs(review) précision", agent="cx3")
            self.commit_receipt("cx3", "codex", note)
        if branch:
            self.repo.git("update-ref", "refs/heads/review/cx3-%d" % self.lot, note)
        work.milestone(self.db, self.lot, "verdict", sha=head_a, actor="cx3",
                       verdict="blocked")
        self.repo.git("reset", "--hard", head_a)
        self.repo.write("src/ameesh/mail.py", "lot\nauteur : correction du B\n")
        if kept_by_author:
            self.repo.write(path, "repris par l'auteur\n")
        head_b = self.repo.commit("author: correction du B", agent="author")
        self.receipts(commits=[head_b])
        self.frozen = head_b
        self.freeze()                                     # F2
        return head_b

    def rewritten(self, head):
        freeze = self.db.query(
            "SELECT id, sha, extract(epoch FROM at)::float8 AS at_ts"
            "  FROM work_item_milestones WHERE work_item_id = %s AND kind = 'frozen'"
            " ORDER BY id DESC LIMIT 1", (self.lot,))[0]
        return review_authors._rewritten_after_freeze(
            self.db, work.get(self.db, self.lot), freeze, self.repo.root,
            head=head, fetch=False)

    def gate(self, head):
        for actor, harness in {"r1": "claude", "cx3": "codex"}.items():
            mark = work.milestone(self.db, self.lot, "verdict", sha=head,
                                  actor=actor, verdict="ok")
            self.verdict_receipt(mark, harness, head)
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/mail.py", "class": "sensitive",
                            "rule": ["src/ameesh/*"]}],
                 "repo": self.repo.root, "target": self.base, "base_sha": self.base}
        return merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), head, repo=self.repo.root,
            klass=klass, item=work.get(self.db, self.lot), required=2,
            families=FAMILIES)

    # -- le constat ------------------------------------------------------

    def test_a_note_on_the_review_branch_does_not_make_a_corrector(self):
        head = self.scenario()
        self.assertEqual(self.rewritten(head), {})
        out = self.gate(head)
        self.assertFalse(out.get("error"), out)
        self.assertNotIn("cx3", out["corrections"])
        self.assertEqual(sorted(entry["actor"] for entry in out["accepted"]),
                         ["cx3", "r1"])

    def test_an_amended_note_on_the_review_branch_does_not_either(self):
        head = self.scenario(amend=True)
        self.assertEqual(self.rewritten(head), {})

    # -- ce qui reste un correcteur (#4457) --------------------------------

    def test_a_lot_file_changed_on_the_review_branch_is_a_correction(self):
        head = self.scenario(path="src/ameesh/mail.py")
        self.assertIn("cx3", self.rewritten(head))

    def test_a_new_file_outside_the_review_branch_is_a_correction(self):
        head = self.scenario(branch=False)
        self.assertIn("cx3", self.rewritten(head))

    def test_a_note_the_author_took_into_the_head_is_a_correction(self):
        head = self.scenario(kept_by_author=True)
        self.assertIn("cx3", self.rewritten(head))

    def test_the_refusal_names_the_corrector_family(self):
        head = self.scenario(path="src/ameesh/mail.py")
        out = self.gate(head)
        self.assertIn("cx3", out["corrections"])
        self.assertIn("cx3 (codex): reviewer who corrected the head", out["why"])
        self.assertNotIn("cx3 (family unknown)", out["why"])


if __name__ == "__main__":
    import unittest
    unittest.main()
