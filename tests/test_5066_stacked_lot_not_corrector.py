# SPDX-License-Identifier: AGPL-3.0-only
"""#5066 — le relecteur dont un autre lot est empilé sur le gel n'est pas « corrector ».

Cas réel #5029 (gel #28737, b43e3a7fe227) : la porte rejetait l'OK de
ameesh-cx3 (verdict #28741) comme celui d'un « reviewer who corrected the
head », alors que les quatre commits de la plage sont de ameesh-dev3. cx3
travaillait le lot #5038 sur une branche empilée sur le premier gel de #5029
(fe75128d3). Son commit 60438e212, rattaché au lot 5038 par son tour, descend
de ce gel et n'est pas dans la tête : `_rewritten_after_freeze` (#4457) le
lisait comme un correctif de relecteur réécrit par l'auteur.

La descendance du gel ne suffit plus. Le reçu est écarté sur trois preuves :
il est rattaché à un autre lot, le commit est contenu dans une tête de cet
autre lot, et son contenu n'est pas réémis dans la tête (B cx2, #173774 : un
correctif écrit sur le lot empilé puis repris par l'auteur). Quand l'une des
trois manque, l'acteur reste exclu par son nom (canon §2 : en cas de doute, la
porte reste fermée).
"""
from __future__ import annotations

from ameesh import merge_queue, registry, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture

FAMILIES = ("claude", "codex", "deepseek")


class StackedLotNotCorrectorTest(_ActivationFixture, PgTestCase):
    """Un lot gelé, un autre lot de cx3 empilé sur le gel, puis un re-gel."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "cx3", harness="codex", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                        " WHERE name = %s", ("cx3",))
        self.other = int(work.add(self.db, title="Lot empilé de cx3",
                                  app="ameesh", assignee="cx3")["id"])

    STACKED_PATH = "src/ameesh/autre.py"
    FIX = ("def corrige_le_defaut(valeur):\n"
           "    resultat = valeur.strip().lower()\n"
           "    return journaliser_le_resultat(resultat)\n")

    def commit_receipt(self, actor, harness, sha, *, work_item_id=None):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id, at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, now() - interval '10 minutes')"
            " ON CONFLICT (subject) DO NOTHING",
            ("commit:" + sha, sha, actor, "s-" + harness, "t", harness,
             work_item_id))

    def verdict_receipt(self, mark, harness):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("milestone:%d" % int(mark["id"]), self.frozen, mark["actor"],
             "s-" + harness, "t", harness, self.lot))

    def _stack(self, *, receipt_lot, other_head=True,
               stacked_text="lot empilé de cx3\n",
               author_text="lot\nauteur : suite\n",
               author_path="src/ameesh/mail.py"):
        """F1, le commit de cx3 empilé sur F1, puis F2 de l'auteur sans lui."""
        head_a = self.frozen
        self.freeze()                       # F1
        work.milestone(self.db, self.lot, "verdict", sha=head_a, actor="cx3",
                       verdict="ok")
        # cx3 travaille son propre lot sur une branche partie de F1.
        if stacked_text is None:
            self.repo.write("src/ameesh/mail.py", "remplacement de cx3\n")
        else:
            self.repo.write(self.STACKED_PATH, stacked_text)
        stacked = self.repo.commit("cx3: travail du lot empilé", agent="cx3")
        self.commit_receipt("cx3", "codex", stacked,
                            work_item_id=self.lot if receipt_lot == "this"
                            else self.other if receipt_lot == "other" else None)
        if other_head:
            self.db.execute("UPDATE work_items SET branch_head = %s WHERE id = %s",
                            (stacked, self.other))
        # L'auteur continue depuis F1 : le commit de cx3 n'est pas dans F2.
        self.repo.git("reset", "--hard", head_a)
        self.repo.write(author_path, author_text)
        head_b = self.repo.commit("author: suite du lot", agent="author")
        self.receipts(commits=[head_b])
        self.frozen = head_b
        self.freeze()                       # F2
        return head_b

    def gate(self, head):
        for actor, harness in (("cx3", "codex"), ("r1", "claude")):
            mark = work.milestone(self.db, self.lot, "verdict", sha=head,
                                  actor=actor, verdict="ok")
            self.verdict_receipt(mark, harness)
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/mail.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/*"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        out = merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), head,
            repo=self.repo.root, klass=klass, item=work.get(self.db, self.lot),
            required=2, families=FAMILIES)
        self.assertFalse(out.get("error"), out)
        return out

    def test_a_commit_of_a_stacked_lot_does_not_make_a_corrector(self):
        out = self.gate(self._stack(receipt_lot="other"))
        self.assertNotIn("cx3", out["corrections"])
        self.assertNotIn("cx3", [row["actor"] for row in out["rejected"]])
        self.assertEqual(sorted(row["actor"] for row in out["accepted"]),
                         ["cx3", "r1"])
        self.assertTrue(out["ok"], out)

    def test_the_stacked_lot_is_also_proven_by_its_freeze(self):
        head = self._stack(receipt_lot="other", other_head=False)
        stacked = self.db.query(
            "SELECT sha FROM provenance_receipts WHERE actor = 'cx3'"
            " AND subject LIKE 'commit:%%'")[0]["sha"]
        work.milestone(self.db, self.other, "frozen", sha=stacked, actor="cx3")
        out = self.gate(head)
        self.assertNotIn("cx3", out["corrections"])

    def test_another_lot_without_a_head_holding_the_commit_stays_closed(self):
        # Le reçu nomme un autre lot, mais aucune tête de ce lot ne contient
        # le commit : pas de seconde preuve, cx3 reste exclu par son nom.
        out = self.gate(self._stack(receipt_lot="other", other_head=False))
        self.assertIn("cx3", out["corrections"])
        self.assertFalse(out["ok"], out)

    def test_a_receipt_without_a_lot_stays_closed(self):
        out = self.gate(self._stack(receipt_lot=None))
        self.assertIn("cx3", out["corrections"])
        self.assertFalse(out["ok"], out)

    def test_a_receipt_of_this_lot_stays_closed(self):
        # Le correctif réécrit de #4457 : rattaché à ce lot, toujours exclu,
        # même si une tête d'un autre lot le contient.
        out = self.gate(self._stack(receipt_lot="this"))
        self.assertIn("cx3", out["corrections"])
        self.assertFalse(out["ok"], out)

    def test_a_cherry_pick_by_the_author_stays_closed(self):
        # B cx2 : le correctif est écrit sur le lot empilé, puis réémis tel
        # quel par l'auteur dans ce lot, sous son identité.
        out = self.gate(self._stack(receipt_lot="other", stacked_text=self.FIX,
                                    author_text=self.FIX,
                                    author_path=self.STACKED_PATH))
        self.assertIn("cx3", out["corrections"])
        self.assertFalse(out["ok"], out)

    def test_a_partial_rewrite_at_another_path_stays_closed(self):
        # Réécriture à la main ailleurs : une seule ligne reprise suffit.
        rewritten = ("def autre_nom(valeur):\n"
                     "    resultat = valeur.strip().lower()\n"
                     "    return resultat or None\n")
        out = self.gate(self._stack(receipt_lot="other", stacked_text=self.FIX,
                                    author_text=rewritten,
                                    author_path="src/ameesh/ailleurs.py"))
        self.assertIn("cx3", out["corrections"])
        self.assertFalse(out["ok"], out)

    def test_a_rewrite_of_the_same_replaced_lines_stays_closed(self):
        # Les ajouts diffèrent, mais les deux commits retirent la même ligne
        # du fichier gelé : la réécriture se lit dans le retrait.
        path = "src/ameesh/mail.py"
        base = "ligne defectueuse du fichier\n"
        self.repo.write(path, base)
        self.frozen = self.repo.commit("author: base du correctif", agent="author")
        self.receipts(commits=[self.frozen])
        out = self.gate(self._stack(
            receipt_lot="other", stacked_text=None,
            author_text="version de l'auteur du remplacement\n",
            author_path=path))
        self.assertIn("cx3", out["corrections"])
        self.assertFalse(out["ok"], out)

    def test_a_binary_stacked_commit_stays_closed(self):
        # Un diff sans preuve par ligne ne se compare pas : la porte reste fermée.
        with open(self.repo.root + "/src/ameesh/image.bin", "wb") as handle:
            handle.write(b"\x00\x01binaire\x00")
        out = self.gate(self._stack(receipt_lot="other"))
        self.assertIn("cx3", out["corrections"])
        self.assertFalse(out["ok"], out)
