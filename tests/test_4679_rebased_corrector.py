# SPDX-License-Identifier: AGPL-3.0-only
"""#4679 — le correcteur qui rebase ou amende avant de pousser reste exclu.

Cas réel #4353 (gel #26301) : ameesh-dev1 corrige après le gel, rebase son
commit local avant de le pousser (`afd35c55` → `c7b7f50e`). Le reçu exact ne
nomme que le commit local, absent de la forge ; le commit poussé n'a pas de
reçu. `_rewritten_after_freeze` ignorait l'objet irrécupérable et la
désignation suivante redésignait le correcteur, en silence.

Un doute n'est pas une absence : l'acteur reste exclu **par son nom**, sa
famille demeure éligible (un autre agent relit, canon §2), et le gel dit le
doute avec le SHA irrécupérable — pour un reçu **rattaché à ce lot**. Un reçu
sans rattachement de lot ne prouve rien sur cette tête : il est dit au gel,
sans exclure son acteur (B/P2 auditeur). Le reçu sans lot est rattaché au lot
de son tour (`turn_costs.work_item_id`) quand ce rattachement existe : un reçu
d'un autre lot ne peut pas exclure un relecteur de celui-ci.
"""
from __future__ import annotations

from ameesh import (merge_queue, plan_git, registry,
                    review_designation as rd, work)

from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture

FAMILIES = ("claude", "codex", "deepseek")


class RebasedCorrectorDoubtTest(_ActivationFixture, PgTestCase):
    """Le correctif local est réécrit avant le push : seul le reçu le nomme."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "cx3", harness="codex", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                        " WHERE name = %s", ("cx3",))

    def commit_receipt(self, actor, harness, sha, *, turn="t", work_item_id=None):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("commit:" + sha, sha, actor, "s-" + harness, turn, harness,
             work_item_id))

    def _forget(self, sha):
        """Retire du clone de lecture un commit local jamais poussé (#4679)."""
        self.repo.git("reflog", "expire", "--expire=now", "--all")
        self.repo.git("gc", "--prune=now", "--quiet")
        self.assertIsNone(plan_git.local_commit(self.repo.root, sha))

    def _prepare_doubt(self, *, receipt_turn="t", receipt_lot=None,
                       create_turn_lot=False):
        """F1, correctif de cx3 amendé avant push, réécriture par l'auteur."""
        head_a = self.frozen
        self.freeze()                       # F1 désigne r1 (claude) et r2 (codex)
        # Le correctif local de cx3, puis son amend avant le push : le commit
        # poussé n'a aucun reçu, et le commit local reste inconnu du clone lu.
        self.repo.write("src/ameesh/mail.py", "lot\ncx3 petit correctif\n")
        local = self.repo.commit("cx3: correctif de relecture", agent="cx3")
        self.commit_receipt("cx3", "codex", local, turn=receipt_turn,
                            work_item_id=receipt_lot)
        env = dict(self.repo.dry,
                   GIT_AUTHOR_NAME="cx3", GIT_AUTHOR_EMAIL="cx3@ameesh.invalid",
                   GIT_COMMITTER_NAME="cx3",
                   GIT_COMMITTER_EMAIL="cx3@ameesh.invalid")
        self.repo.git("commit", "--amend", "-m",
                      "cx3: correctif de relecture (rebase avant push)", env=env)
        pushed = self.repo.git("rev-parse", "HEAD").strip()
        self.assertNotEqual(pushed, local)
        self._forget(local)
        self.db.execute(
            "UPDATE provenance_receipts SET at = now() - interval '10 minutes'"
            " WHERE subject = %s", ("commit:" + local,))
        if create_turn_lot:
            other = int(work.add(self.db, title="Autre lot du relecteur",
                                 app="ameesh", assignee="author")["id"])
            self.db.execute(
                "INSERT INTO turn_costs (agent, harness, session, recorded_at,"
                " work_item_id, turn_id)"
                " VALUES ('cx3', 'codex', 's-codex', now(), %s, %s)",
                (other, receipt_turn))
        elif receipt_turn == "t":
            # Le reçu du hook ne porte pas le lot ; son tour le nomme (#4679).
            self.db.execute(
                "INSERT INTO turn_costs (agent, harness, session, recorded_at,"
                " work_item_id, turn_id)"
                " VALUES ('cx3', 'codex', 's-codex', now(), %s, 't')",
                (self.lot,))
        work.milestone(self.db, self.lot, "verdict", sha=head_a, actor="cx3",
                       verdict="blocked")
        # L'auteur réécrit depuis le B : le commit poussé n'est ancêtre
        # d'aucune tête, et son contenu diffère (pas de report).
        self.repo.git("reset", "--hard", head_a)
        self.repo.write("src/ameesh/mail.py",
                        "lot\ncx3 petit correctif\nauteur : reprise\n")
        head_b = self.repo.commit("author: reprise du correctif", agent="author")
        self.receipts(commits=[head_b])
        self.frozen = head_b
        return head_a, local, pushed, head_b

    def test_a_rebased_corrector_is_excluded_by_doubt_and_the_freeze_says_it(self):
        _head_a, local, _pushed, _head_b = self._prepare_doubt()
        _mark, out = self.freeze()          # F2
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        names = [row["name"] for row in out["reviewers"]]
        self.assertNotIn("cx3", names)
        # Le §2 laisse la famille codex éligible : r2, de la même famille, relit.
        self.assertIn("r2", names)
        self.assertIn("r1", names)
        self.assertIn("cx3", out["why"])
        self.assertIn(local[:12], out["why"])
        self.assertIn("unresolved", out["why"].lower())
        self.assertIn("doubt", out["why"].lower())

    def test_the_merge_gate_refuses_the_doubted_corrector(self):
        _head_a, _local, _pushed, head_b = self._prepare_doubt()
        harnesses = {"r1": "claude", "r2": "codex", "cx3": "codex"}
        for actor, harness in harnesses.items():
            mark = work.milestone(self.db, self.lot, "verdict", sha=head_b,
                                  actor=actor, verdict="ok")
            self.verdict_receipt(mark, harness)
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/mail.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/*"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        out = merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), head_b,
            repo=self.repo.root, klass=klass, item=work.get(self.db, self.lot),
            required=2, families=FAMILIES)
        self.assertFalse(out.get("error"), out)
        self.assertIn("cx3", out["corrections"])
        self.assertNotIn("cx3", [entry["actor"] for entry in out["accepted"]])
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["r1", "r2"])

    def test_a_receipt_of_another_lot_does_not_exclude_the_reviewer(self):
        # Le tour du reçu porte un autre lot : ce commit local ne prouve rien
        # sur cette tête. Le relecteur reste un candidat de ce lot.
        self._prepare_doubt(receipt_turn="tour-autre-lot",
                            create_turn_lot=True)
        _mark, out = self.freeze()          # F2
        self.assertNotIn("cx3", out.get("fixers") or [])
        self.assertNotIn("unresolved", out["why"].lower())

    def verdict_receipt(self, mark, harness):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("milestone:%d" % int(mark["id"]), self.frozen, mark["actor"],
             "s-" + harness, "t", harness, self.lot))

    def test_an_unattached_receipt_is_reported_but_does_not_exclude(self):
        # B/P2 auditeur : un reçu introuvable **sans** rattachement (tour
        # absent du grand livre) ne prouve rien sur cette tête. Il ne fait pas
        # de son acteur un correcteur ; le gel le dit, avec le SHA absent.
        _head_a, local, _pushed, head_b = self._prepare_doubt(
            receipt_turn="tour-inconnu")
        _mark, out = self.freeze()          # F2
        self.assertNotIn("cx3", out.get("fixers") or [])
        self.assertIn("without a lot attachment", out["why"])
        self.assertIn(local[:12], out["why"])
        self.assertNotIn("excluded by doubt", out["why"].lower())
        # La porte ne rejette plus son avis comme celui d'un correcteur : il
        # est retenu (premier avis, famille codex), puis r1 (claude).
        for actor, harness in (("cx3", "codex"), ("r1", "claude"),
                               ("r2", "codex")):
            mark = work.milestone(self.db, self.lot, "verdict", sha=head_b,
                                  actor=actor, verdict="ok")
            self.verdict_receipt(mark, harness)
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/mail.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/*"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        gate = merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), head_b,
            repo=self.repo.root, klass=klass, item=work.get(self.db, self.lot),
            required=2, families=FAMILIES)
        self.assertFalse(gate.get("error"), gate)
        self.assertNotIn("cx3", gate["corrections"])
        self.assertNotIn("cx3", [entry["actor"] for entry in gate["rejected"]])
        self.assertIn("cx3", [entry["actor"] for entry in gate["accepted"]])
