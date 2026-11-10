# SPDX-License-Identifier: AGPL-3.0-only
"""#6626 — un enfant parti d'une tête intermédiaire jamais gelée du parent.

Relecture de #6607 (tête 12f2241ec705) : parent A à A0 (jamais gelé), enfant
B = A0+B1, A avance à A1 ; A0 n'est plus `branch_head`, ni un gel, ni un
verdict : `stacked_on(B1)` ne nommait pas A et le paquet pouvait emporter A0
non relu.

Attendu : le relevé des fusions (`set_branch_head`) inscrit chaque tête à
l'historique du lot (0563) ; `open_lot_heads` la compte parmi les têtes du
parent, sur sa branche courante seulement ; le débit lit B
`stacked_undeclared` sur A, A garde son `qa_merge`.
"""
from __future__ import annotations

import os

from ameesh import debit, packet_candidates, registry, storage, work

from .support import PgTestCase
from .test_4572_slo_candidate_cause import git, write_commit

HEURE = 3600.0


class BranchHeadsHistoryTest(PgTestCase):
    GO = 1_792_950_000.0

    def setUp(self):
        super().setUp()
        self.root = root = os.path.join(self.tmp, "repo")
        os.makedirs(root)
        git(root, "init", "-q", "-b", "main")
        base = write_commit(root, "f", "base\n", "base")
        git(root, "checkout", "-q", "-b", "review/a")
        self.a0 = write_commit(root, "a", "a0\n", "lot A, tête jamais gelée")
        git(root, "checkout", "-q", "-b", "lot/b")
        self.b1 = write_commit(root, "b", "b\n", "lot B")
        git(root, "checkout", "-q", "review/a")
        self.a1 = write_commit(root, "a", "a1\n", "lot A, suite")
        git(root, "checkout", "-q", "main")
        git(root, "update-ref", "refs/remotes/origin/main", base)
        git(root, "update-ref", "refs/remotes/origin/review/a", self.a1)
        git(root, "update-ref", "refs/remotes/origin/lot/b", self.b1)
        registry.upsert(self.db, "dev1", harness="claude", host=self.cfg.host,
                        cwd=root)
        registry.upsert(self.db, "rel", harness="codex", host=self.cfg.host)
        self.a = self.lot("lot A", "review/a", self.a1)
        self.b = self.lot("lot B", "lot/b", self.b1)
        self.st = storage.of(self.db)

    def lot(self, title, branch, tip):
        item = work.add(self.db, title=title, assignee="dev1", app="ameesh",
                        branch=branch, branch_target="main", actor="dev1")
        work.move(self.db, item["id"], "build")
        work.move(self.db, item["id"], "qa")
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor, sha)"
            " VALUES (%s, 'frozen', to_timestamp(%s), 'dev1', %s)",
            (item["id"], self.GO - 1, tip))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor,"
            " verdict, sha) VALUES (%s, 'verdict', to_timestamp(%s), 'rel', 'ok', %s)",
            (item["id"], self.GO, tip))
        return item

    def observe_parent(self):
        """Le relevé voit A à A0, puis à A1 (A0 n'est jamais gelé)."""
        self.st.work.set_branch_head(self.a["id"], "review/a", self.a0)
        self.st.work.set_branch_head(self.a["id"], "review/a", self.a1)

    def heads_of(self, lot):
        heads, truncated = packet_candidates.open_lot_heads(self.db, "ameesh")
        self.assertFalse(truncated)
        return next((h["shas"] for h in heads if h["lot"] == lot), [])

    def report(self):
        return debit.read(self.db, cfg=self.cfg, now=self.GO + 3 * HEURE,
                          jobs=False, wait_s=2 * HEURE)

    def rows(self, report, lot):
        return [(row["kind"], row.get("depends_on"))
                for row in report["slow_waits"] + report["human_waits"]
                if row["lot"] == lot]

    def test_le_releve_inscrit_chaque_tete_une_fois(self):
        self.observe_parent()
        self.st.work.set_branch_head(self.a["id"], "review/a", self.a1)
        self.assertEqual([(h["branch"], h["sha"])
                          for h in self.st.work.branch_heads([self.a["id"]])],
                         [("review/a", self.a0), ("review/a", self.a1)])
        self.assertIn(self.a0, self.heads_of(self.a["id"]))

    def test_une_autre_branche_n_inscrit_rien(self):
        self.st.work.set_branch_head(self.a["id"], "autre", self.a0)
        self.assertEqual(self.st.work.branch_heads([self.a["id"]]), [])

    def test_une_tete_d_une_ancienne_branche_n_est_pas_comptee(self):
        self.observe_parent()
        self.db.execute("UPDATE work_items SET branch = 'review/a2' WHERE id = %s",
                        (self.a["id"],))
        self.assertNotIn(self.a0, self.heads_of(self.a["id"]))

    def test_l_enfant_d_une_tete_intermediaire_est_empile(self):
        # témoin : sans relevé de A0, B est lu attente de paquet (le défaut)
        self.assertEqual(self.rows(self.report(), self.b["id"]),
                         [("qa_merge", None)])
        self.observe_parent()
        report = self.report()
        self.assertEqual(self.rows(report, self.b["id"]),
                         [("stacked_undeclared", self.a["id"])])
        # jamais un faux parent : A n'est pas empilé sur son enfant
        self.assertEqual(self.rows(report, self.a["id"]), [("qa_merge", None)])
        stacked = packet_candidates.stacked_on(
            self.root, self.b1, "main", packet_candidates.open_lot_heads(
                self.db, "ameesh")[0], lot=self.b["id"], branch="lot/b")
        self.assertEqual(stacked, [{"lot": self.a["id"], "branch": "review/a",
                                    "sha": self.a0}])
