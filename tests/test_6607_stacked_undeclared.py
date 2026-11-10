# SPDX-License-Identifier: AGPL-3.0-only
"""#6607 — branche empilée sur un lot non fusionné sans dépendance déclarée.

Constat (auditeur, analyse profonde 10/11, angle 3) : nexlink #6542 (validé
14:51Z) lu « qa_merge 9 h 37, rappeler au pilote » alors qu'il était empilé
sur #6501 non fusionné, sans dépendance déclarée ; même motif sur #6548 et
#6502. Le débit ne voyait une dépendance que par `branch_target` (#3437) ou
par un prérequis déclaré (#6214) ; `paquet-candidats` trouvait une fusion
propre et pouvait emporter les commits non relus du parent dans le paquet.

Attendu : cause `stacked-undeclared` (auteur ou pilote, geste `ameesh work
dependencies <lot> --add <parent>`) quand la tête gelée porte le gel ou le
`branch_head` d'un autre lot ouvert du projet absent de la cible ; le débit
la lit comme attente de dépendance ; `paquet-candidats` refuse le lot.

Témoin : dépôt jetable, A sur `review/a` non fusionné, B branché sur A, gelé
et relu, sans dépendance : pas de `qa_merge`, cause `stacked-undeclared`
`depends_on=A`, refus du candidat.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from ameesh import debit, packet_candidates, registry, work, work_dependencies

from .support import PgTestCase
from .test_4572_slo_candidate_cause import git, write_commit

HEURE = 3600.0


def stacked_repo(root):
    """`main` ; `review/a` (lot A) non fusionné ; `lot/b` branché sur A."""
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    base = write_commit(root, "f", "base\n", "base")
    git(root, "checkout", "-q", "-b", "review/a")
    a_tip = write_commit(root, "a", "a\n", "lot A, non relu")
    git(root, "checkout", "-q", "-b", "lot/b")
    b_tip = write_commit(root, "b", "b\n", "lot B")
    git(root, "checkout", "-q", "main")
    git(root, "update-ref", "refs/remotes/origin/main", base)
    git(root, "update-ref", "refs/remotes/origin/review/a", a_tip)
    git(root, "update-ref", "refs/remotes/origin/lot/b", b_tip)
    return base, a_tip, b_tip


class StackedOnTest(unittest.TestCase):
    """Le moteur, sans base : une lecture `rev-list` et les têtes connues."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test-6607-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        self.base, self.a_tip, self.b_tip = stacked_repo(self.repo)
        self.others = [{"lot": 1, "branch": "review/a", "shas": [self.a_tip]},
                       {"lot": 2, "branch": "lot/b", "shas": [self.b_tip]}]

    def stacked(self, **kw):
        kw.setdefault("lot", 2)
        kw.setdefault("branch", "lot/b")
        return packet_candidates.stacked_on(self.repo, self.b_tip, self.base,
                                            self.others, **kw)

    def test_le_parent_ouvert_est_nomme(self):
        self.assertEqual(self.stacked(),
                         [{"lot": 1, "branch": "review/a", "sha": self.a_tip,
                           "shared": 1, "contained": True}])

    def test_le_parent_avance_et_regele_reste_un_parent(self):
        # B1 cx1 : A1 est dans B ; A avance à A2 et ses têtes (observée,
        # gelée, publiée) sont toutes A2, hors de base..B. A1 reste partagé.
        git(self.repo, "checkout", "-q", "review/a")
        a2 = write_commit(self.repo, "a", "a2\n", "lot A, suite")
        git(self.repo, "checkout", "-q", "main")
        self.others[0]["shas"] = [a2]
        found = self.stacked()
        self.assertEqual([(s["lot"], s["sha"]) for s in found], [(1, self.a_tip)])

    def test_un_partage_limite_a_la_cible_n_empile_pas(self):
        # A et B partent tous deux de `main` : seul l'historique de la cible
        # est commun, aucun commit hors cible.
        git(self.repo, "checkout", "-q", "-b", "lot/c", "main")
        c_tip = write_commit(self.repo, "c", "c\n", "lot C")
        git(self.repo, "checkout", "-q", "main")
        self.assertEqual(packet_candidates.stacked_on(
            self.repo, c_tip, self.base, self.others, lot=3, branch="lot/c"), [])

    def test_le_lot_empile_sur_moi_n_est_pas_mon_parent(self):
        # du point de vue de A : B contient A, c'est B qui est empilé
        self.assertEqual(packet_candidates.stacked_on(
            self.repo, self.a_tip, self.base, self.others, lot=1,
            branch="review/a"), [])

    def test_le_vrai_parent_passe_avant_un_frere(self):
        # D est aussi branché sur A : B et D se recoupent par A1, mais A est
        # le parent entièrement contenu dans B.
        git(self.repo, "checkout", "-q", "-b", "lot/d", "review/a")
        d_tip = write_commit(self.repo, "d", "d\n", "lot D")
        git(self.repo, "checkout", "-q", "main")
        self.others.append({"lot": 0, "branch": "lot/d", "shas": [d_tip]})
        found = self.stacked()
        self.assertEqual([s["lot"] for s in found], [1, 0])

    def test_un_prefixe_de_gel_suffit(self):
        self.others[0]["shas"] = [self.a_tip[:12]]
        self.assertEqual([s["lot"] for s in self.stacked()], [1])

    def test_une_dependance_declaree_ou_une_cible_de_lot_n_empile_pas(self):
        self.assertEqual(self.stacked(declared=[1]), [])
        # #3437 : la cible du lot est la branche du parent, il entre avec lui
        self.assertEqual(self.stacked(branch_target="origin/review/a"), [])

    def test_un_parent_dans_la_cible_n_empile_pas(self):
        self.assertEqual(packet_candidates.stacked_on(
            self.repo, self.b_tip, self.a_tip, self.others, lot=2,
            branch="lot/b"), [])

    def test_candidate_refusal_rend_stacked_undeclared(self):
        row = {"id": 2, "branch": "lot/b", "branch_target": "main",
               "assignee": "dev1"}
        cause = packet_candidates.candidate_refusal(
            None, None, row, [{"kind": "frozen", "sha": self.b_tip, "at_ts": 1.0}],
            repo=self.repo, open_heads=self.others)
        self.assertEqual(cause["code"], "stacked-undeclared")
        self.assertEqual(cause["owner"], "author")
        self.assertEqual(cause["depends_on"], 1)
        self.assertFalse(cause["ready"])
        self.assertIn("dependencies 2 --add 1", cause["detail"])
        self.assertIn("declare the dependency", cause["gesture"])

    def test_un_gel_perime_garde_freeze_not_head(self):
        # le regel passe avant : la tête publiée a bougé depuis le gel
        git(self.repo, "checkout", "-q", "lot/b")
        moved = write_commit(self.repo, "c", "c\n", "après gel")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "update-ref", "refs/remotes/origin/lot/b", moved)
        row = {"id": 2, "branch": "lot/b", "branch_target": "main"}
        cause = packet_candidates.candidate_refusal(
            None, None, row, [{"kind": "frozen", "sha": self.b_tip, "at_ts": 1.0}],
            repo=self.repo, open_heads=self.others)
        self.assertEqual(cause["code"], "freeze-not-head")


class DebitStackedTest(PgTestCase):
    """Le chemin réel du débit : base, registre, dépôt, aucune doublure."""

    GO = 1_792_950_000.0

    def setUp(self):
        super().setUp()
        root = os.path.join(self.tmp, "repo")
        _base, a_tip, b_tip = stacked_repo(root)
        registry.upsert(self.db, "dev1", harness="claude", host=self.cfg.host,
                        cwd=root)
        registry.upsert(self.db, "rel", harness="codex", host=self.cfg.host)
        self.a = self.lot("lot A", "review/a", a_tip)
        self.b = self.lot("lot B", "lot/b", b_tip)
        self.now = self.GO + 3 * HEURE

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

    def report(self):
        return debit.read(self.db, cfg=self.cfg, now=self.now, jobs=False,
                          wait_s=2 * HEURE)

    def rows(self, report, lot):
        return [row for row in report["slow_waits"] + report["human_waits"]
                if row["lot"] == lot]

    def test_le_lot_empile_n_est_pas_une_attente_de_paquet(self):
        report = self.report()
        rows = self.rows(report, self.b["id"])
        self.assertEqual([row["kind"] for row in rows], ["stacked_undeclared"],
                         report["slow_waits"])
        row = rows[0]
        self.assertEqual(row["depends_on"], self.a["id"])
        self.assertEqual(row["cause"]["code"], "stacked-undeclared")
        self.assertEqual(row["who"], "dev1")
        self.assertIn("ameesh work dependencies %d --add %d"
                      % (self.b["id"], self.a["id"]), row["gesture"])
        # le parent reste l'attente du paquet et porte le lot empilé
        parent = self.rows(report, self.a["id"])
        self.assertEqual([r["kind"] for r in parent], ["qa_merge"])
        self.assertEqual(parent[0]["dependents"], [self.b["id"]])
        self.assertIn("stacked-undeclared", debit.format_text(report))

    def test_la_dependance_declaree_range_le_lot_derriere_son_parent(self):
        work_dependencies.add(self.db, self.b["id"], self.a["id"], actor="dev1")
        report = self.report()
        self.assertEqual(self.rows(report, self.b["id"]), [])
        parent = self.rows(report, self.a["id"])
        self.assertEqual(parent[0]["dependents"], [self.b["id"]])


if __name__ == "__main__":
    unittest.main()
