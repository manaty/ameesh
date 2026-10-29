# SPDX-License-Identifier: AGPL-3.0-only
"""#4582 : au regel, un siège gardé dont l'avis n'est pas reporté est prévenu.

Constat du 29/10 (lot #4422) : un siège manuel (auditeur, claude) gardé d'un
gel à l'autre comptait comme pourvu (`missing: 0`) mais ne recevait aucune
demande pour la nouvelle tête, alors que son verdict n'était pas reporté
(`verdict not carried`, #2442). L'avis dormait jusqu'au SLO de 2 h.

Le témoin : gel A avec l'avis OK de R ; gel B où cet avis n'est pas reporté →
R est dans `sent` de la désignation de B, avec la commande du jalon de B.
Un siège dont l'avis est reporté (ou déjà rendu sur la tête) n'est pas relancé.
"""
from __future__ import annotations

from ameesh import review_designation_pg as pg
from ameesh import work
from ameesh.work_reviewers import designated

from .test_2487_designation_carry import _Fixture


class _ReaskFixture(_Fixture):
    """Le lot gelé, plus les gestes et lectures du siège manuel gardé."""

    def redesignate(self, name: str = "rev1") -> None:
        """Le pilote garde le siège de `name` à la main (geste du 29/10)."""
        work.designate_reviewer(self.cfg, self.db, self.lot, name,
                                actor="human:smichea")

    def last_mark(self) -> dict:
        rows = self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s"
            " AND note LIKE %s ORDER BY id DESC LIMIT 1",
            (self.lot, pg.MARK_PREFIX + "%"))
        return pg.designation_of(rows[0]["note"]) if rows else {}

    def request_mails(self) -> list:
        return [row for row in self.mailbox()
                if isinstance(row.get("payload"), dict)
                and row["payload"].get("review_designation")
                and row["payload"]["review_designation"].get("state") == "designated"]


class DesignationReaskTest(_ReaskFixture):
    """Un lot gelé, un siège manuel gardé, une nouvelle tête non réalignée."""

    def test_a_manual_seat_whose_verdict_is_not_carried_is_asked_on_the_new_head(self):
        # Gel A : la désignation automatique choisit rev1 et rev2.
        _, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        # rev1 rend son avis sur A, puis le pilote en fait un siège manuel
        # gardé (geste du 29/10 sur #4422).
        self.verdict("rev1", self.repo.reviewed)
        self.redesignate("rev1")

        # Gel B : une vraie tête d'auteur, l'avis de rev1 n'est pas reportable.
        changed = self.repo.author_commit()
        self.receipt(changed)
        _, outcome = self.freeze(changed)
        self.assertEqual(outcome["state"], "designated", outcome)

        # Le siège de rev1 est gardé (il ne fait pas partie des relecteurs
        # automatiques de B)...
        self.assertEqual([r["name"] for r in outcome["reviewers"]], ["rev2"])
        self.assertEqual(designated(work.get(self.db, self.lot)), ["rev1", "rev2"])
        # ...mais une demande pour la tête exacte de B lui est envoyée.
        mark = self.last_mark()
        self.assertEqual([entry["to"] for entry in mark["sent"]],
                         ["rev1", "rev2"])
        sent = [row for row in self.request_mails()
                if row["recipient"] == "rev1"
                and row["payload"]["review_designation"].get("sha") == changed]
        self.assertEqual(len(sent), 1, self.request_mails())
        self.assertIn("verdict ok|blocked --sha %s" % changed, sent[0]["body"])

    def test_a_manual_seat_with_a_verdict_on_the_frozen_head_is_not_asked_again(self):
        # Gel A : rev1 devient un siège manuel et vote sur A.
        _, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.verdict("rev1", self.repo.reviewed)
        self.redesignate("rev1")
        # Regel de la MÊME tête : rev1 a déjà un avis sur elle, rien ne part.
        self.db.execute("DELETE FROM agent_mailbox")
        _, outcome = self.freeze(self.repo.reviewed)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.assertEqual([row["recipient"] for row in self.request_mails()],
                         ["rev2"])


class CarriedSeatTest(_ReaskFixture):
    """Un siège reporté n'est pas relancé (la face inverse du témoin)."""

    def test_a_manual_seat_whose_verdict_is_carried_is_not_asked(self):
        self.freeze(self.repo.reviewed)
        self.verdict("rev1", self.repo.reviewed)
        work.designate_reviewer(self.cfg, self.db, self.lot, "rev1",
                                actor="human:smichea")
        self.verdict("rev2", self.repo.reviewed)
        self.db.execute("DELETE FROM agent_mailbox")
        frozen = self.repo.merge_target()
        _, outcome = self.freeze(frozen)
        self.assertEqual(outcome["state"], "carried", outcome)
        self.assertEqual([r["name"] for r in outcome["reviewers"]], ["rev1", "rev2"])
        self.assertEqual([row["recipient"] for row in self.mailbox()], [])

    def test_a_partial_carry_does_not_ask_the_seat_it_reports(self):
        # Gel A : rev1 et rev2 sont désignés. rev1 vote sur A, qui se réaligne
        # en B ; rev2 vote sur une tête C que B n'atterrit pas. Le plan de B
        # reporte rev1 et refuse rev2 : la désignation réelle tourne pour le
        # siège manquant, mais ne relance pas rev1 (son avis est reporté).
        self.freeze(self.repo.reviewed)
        self.verdict("rev1", self.repo.reviewed)
        # B = la tête relue A réalignée sur la cible ; C = B + un commit
        # d'auteur, sur lequel rev2 rend son verdict non reportable.
        frozen = self.repo.merge_target()
        changed = self.repo.author_commit("lot, autre tête\n")
        self.verdict("rev2", changed)
        self.redesignate("rev1")
        self.db.execute("DELETE FROM agent_mailbox")
        _, outcome = self.freeze(frozen)
        self.assertEqual(outcome["state"], "designated", outcome)
        self.assertNotIn("rev1", [row["recipient"] for row in self.mailbox()])
        self.assertIn("rev2", [row["recipient"] for row in self.mailbox()])
        mark = self.last_mark()
        self.assertNotIn("rev1", [entry["to"] for entry in mark["sent"]])
        self.assertIn("rev2", [entry["to"] for entry in mark["sent"]])
