# SPDX-License-Identifier: AGPL-3.0-only
"""#4779 — un relecteur doit prouver la capacité `propose` et un rôle.

Défaut : le gel #26913 de #4770 a désigné `handoff-canary-3392`, une fiche
`capabilities: [read]`, `roles: []`, qui n'a jamais lancé de tour. Son siège
retenait la porte sans qu'aucun avis puisse venir. La sélection automatique
écarte désormais tout agent dont la fiche ne porte pas `propose` ou dont les
rôles sont vides ; une capacité absente du registre est un doute (refus +
journal), pas une preuve. Le même refus s'applique à la désignation manuelle.

Le retrait d'un siège règle aussi la demande de relecture encore non remise
chez l'agent (comme une demande à échéance : message soldé, aucun tour), avec
une trace au fil du lot. C'est le préalable au premier démarrage du canari.
"""
from __future__ import annotations

import unittest

from ameesh import fil, mail, registry, work
from ameesh import review_designation as rd
from ameesh import review_designation_activation as act

from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture

FAMILIES = ("claude", "codex", "deepseek")


def agent_row(name, *, team="ameesh", harness="deepseek", capabilities=("read", "report-drift", "propose"),
              roles=("implementation",), status="idle"):
    """Une ligne de registre telle que `registry.overview` la rend (#4779)."""
    return {"name": name, "team": team, "harness": harness, "session_harness": "",
            "status": status, "mode": "execute", "status_text": "",
            "capabilities": (capabilities if isinstance(capabilities, str)
                             else None if capabilities is None else list(capabilities)),
            "roles": (roles if isinstance(roles, str)
                      else None if roles is None else list(roles))}


class SelectionProfileTest(unittest.TestCase):
    """La sélection seule : aucune base, des lignes de registre."""

    def names(self, *rows):
        return [c.name for c in rd.candidates_from_registry(
            rows, project="ameesh", families=FAMILIES)]

    def test_a_read_only_canary_is_not_a_candidate(self):
        self.assertEqual(self.names(
            agent_row("handoff-canary-3392", capabilities=("read",), roles=()),
            agent_row("reviewer")), ["reviewer"])

    def test_a_propose_agent_with_a_role_is_kept(self):
        self.assertEqual(self.names(agent_row("reviewer")), ["reviewer"])

    def test_empty_roles_are_refused_without_journal(self):
        refused: list = []
        self.assertEqual(self.names(
            agent_row("no-role", roles=())), [])
        candidates = rd.candidates_from_registry(
            [agent_row("no-role", roles=())], project="ameesh", families=FAMILIES,
            unreadable_profiles=refused)
        self.assertEqual(candidates, [])
        self.assertEqual(refused, [])

    def test_null_roles_are_a_clean_refusal(self):
        # `roles` NULL = la fiche n'en déclare aucun (canon `_roles` rend None),
        # pas une donnée illisible : refus silencieux.
        refused: list = []
        rd.candidates_from_registry([agent_row("no-role", roles=None)],
                                    project="ameesh", families=FAMILIES,
                                    unreadable_profiles=refused)
        self.assertEqual(refused, [])

    def test_a_missing_capabilities_field_is_unreadable_and_journaled(self):
        refused: list = []
        candidates = rd.candidates_from_registry(
            [agent_row("no-caps", capabilities=None)], project="ameesh",
            families=FAMILIES, unreadable_profiles=refused)
        self.assertEqual(candidates, [])
        self.assertEqual(refused, [("no-caps", rd.CAPABILITIES_UNREADABLE)])

    def test_a_malformed_capabilities_field_is_unreadable(self):
        refused: list = []
        row = agent_row("bad-caps")
        row["capabilities"] = 42
        self.assertEqual(rd.candidates_from_registry(
            [row], project="ameesh", families=FAMILIES,
            unreadable_profiles=refused), [])
        self.assertEqual(refused, [("bad-caps", rd.CAPABILITIES_UNREADABLE)])

    def test_postgres_array_text_is_read(self):
        self.assertEqual(self.names(agent_row(
            "text-array", capabilities="{read,propose}", roles="{implementation}")),
            ["text-array"])

    def test_an_agent_of_another_team_is_still_filtered_before_the_profile(self):
        refused: list = []
        rd.candidates_from_registry(
            [agent_row("nexlink-agent", team="nexlink", capabilities=None)],
            project="ameesh", families=FAMILIES, unreadable_profiles=refused)
        # hors équipe : ce n'est pas la peine de journaliser son profil
        self.assertEqual(refused, [])


class ManualSeatProfileTest(unittest.TestCase):
    """Un siège manuel sans `propose`/rôle ne couvre aucun avis (B1 cx2)."""

    def seats(self, *manual):
        rows = [agent_row("canary", harness="claude", capabilities=("read",), roles=()),
                agent_row("no-role", harness="claude", roles=None),
                agent_row("reviewer", harness="claude")]
        return act._manual_seats(
            list(manual), rows,
            authors=[{"name": "author", "family": "deepseek"}],
            families=FAMILIES)

    def test_a_read_only_manual_seat_covers_nothing(self):
        self.assertEqual([s["name"] for s in self.seats("canary", "reviewer")],
                         ["reviewer"])

    def test_a_manual_seat_without_roles_covers_nothing(self):
        self.assertEqual([s["name"] for s in self.seats("no-role", "reviewer")],
                         ["reviewer"])

    def test_a_propose_manual_seat_still_covers(self):
        self.assertEqual([s["name"] for s in self.seats("reviewer")],
                         ["reviewer"])


class ProfilePgTest(PgTestCase):
    """Le refus manuel et le règlement de la demande, sur une vraie base."""

    def setUp(self):
        super().setUp()
        for name, capabilities, roles in (
                ("author", ("read", "report-drift", "propose"), ("implementation",)),
                ("reviewer", ("read", "report-drift", "propose"), ("implementation",)),
                ("reviewer2", ("read", "report-drift", "propose"), ("implementation",)),
                ("canary", ("read",), ()),
                ("no-caps", None, ("implementation",))):
            registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host,
                            cwd=self.tmp, status="idle")
            self.db.execute(
                "UPDATE agent_registry SET team = 'ameesh', capabilities = %s::text[],"
                " roles = %s::text[] WHERE name = %s",
                (None if capabilities is None else "{%s}" % ",".join(capabilities),
                 None if roles is None else "{%s}" % ",".join(roles), name))

    def lot(self, state="qa"):
        item = work.add(self.db, title="profil de relecteur #4779", app="ameesh",
                        assignee="author")
        lot = int(item["id"])
        work.move(self.db, lot, "build", actor="human:alice")
        if state == "qa":
            work.move(self.db, lot, "qa", actor="human:alice")
        elif state == "merged":
            self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                            (lot,))
        return lot

    def test_manual_designation_refuses_a_read_only_canary(self):
        with self.assertRaises(work.WorkError) as ctx:
            work.check_reviewer(self.db, "canary")
        message = str(ctx.exception)
        self.assertIn("reviewer refused", message)
        self.assertIn("propose", message)
        self.assertIn("4779", message)

    def test_manual_designation_refuses_an_unreadable_capability(self):
        with self.assertRaises(work.WorkError) as ctx:
            work.check_reviewer(self.db, "no-caps")
        self.assertIn("capabilities", str(ctx.exception))
        self.assertIn("4779", str(ctx.exception))

    def test_manual_designation_keeps_a_propose_agent_and_a_human(self):
        self.assertEqual(work.check_reviewer(self.db, "reviewer"), "reviewer")
        self.assertEqual(work.check_reviewer(self.db, "human:alice"),
                         "human:alice")

    def test_remove_settles_the_pending_request_and_traces_the_lot_thread(self):
        lot = self.lot()
        work.designate_reviewer(self.cfg, self.db, lot, "reviewer",
                                actor="human:alice")
        rows = mail.review_requests(self.db, "reviewer", lot)
        self.assertEqual(len(rows), 1)
        message_id = int(rows[0]["id"])
        self.assertIsNone(rows[0].get("delivered_ts"))

        removed = work.designate_reviewer(self.cfg, self.db, lot, "reviewer",
                                          actor="human:alice", remove=True)
        self.assertTrue(removed.get("reviewer_changed"))
        self.assertEqual(removed.get("reviewer_settled"), [message_id])
        settled = self.db.query(
            "SELECT status, delivered_at FROM agent_mailbox WHERE id = %s",
            (message_id,))[0]
        self.assertEqual(settled["status"], "delivered")
        self.assertIsNotNone(settled["delivered_at"])

        entries = fil.transport_for(self.cfg).read(fil.ThreadRef("ameesh", lot))
        self.assertTrue(any("soldées sans tour" in entry.text for entry in entries),
                        [entry.text for entry in entries])

    def test_clear_reviewer_settles_every_removed_seat(self):
        lot = self.lot()
        for name in ("reviewer", "reviewer2"):
            work.designate_reviewer(self.cfg, self.db, lot, name,
                                    actor="human:alice")
        messages = [int(mail.review_requests(self.db, name, lot)[0]["id"])
                    for name in ("reviewer", "reviewer2")]
        row = work.clear_reviewer(self.cfg, self.db, lot, actor="human:alice")
        self.assertEqual(sorted(row.get("reviewer_settled") or []), sorted(messages))
        rows = self.db.query(
            "SELECT id, status FROM agent_mailbox WHERE id = %s OR id = %s"
            " ORDER BY id", tuple(messages))
        self.assertEqual([r["status"] for r in rows], ["delivered", "delivered"])

    def test_a_replay_after_the_lot_left_qa_still_settles_the_request(self):
        # Cas réel #4770 : le siège a déjà été retiré, le lot peut avoir
        # fusionné. Le rejeu de `--remove` doit régler la demande caduque sans
        # écrire au siège (aucune désignation hors qa).
        lot = self.lot(state="merged")
        message_id = mail.send(
            self.db, "ameesh", "canary", "Review request for lot #%d (test)" % lot,
            kind="request", work_item_id=str(lot),
            payload={"review_designation": {"lot": lot, "sha": "a" * 40}})
        row = work.designate_reviewer(self.cfg, self.db, lot, "canary",
                                      actor="human:alice", remove=True)
        self.assertFalse(row.get("reviewer_changed"))
        self.assertEqual(row.get("reviewer_settled"), [message_id])
        settled = self.db.query(
            "SELECT status FROM agent_mailbox WHERE id = %s", (message_id,))[0]
        self.assertEqual(settled["status"], "delivered")
        entries = fil.transport_for(self.cfg).read(fil.ThreadRef("ameesh", lot))
        self.assertTrue(any("soldées sans tour" in entry.text for entry in entries))

    def test_a_decision_request_is_not_a_review_request(self):
        # Un `request` lie au lot peut etre une question posee a l'owner
        # (`payload.decision`) : le retrait d'un siege humain ne doit pas la
        # solder.
        lot = self.lot()
        decision = mail.send(
            self.db, "ameesh", "human:alice",
            "Decision request for lot #%d (test)" % lot,
            kind="request", work_item_id=str(lot),
            payload={"decision": {"question": "go?", "lot": lot}})
        row = work.designate_reviewer(self.cfg, self.db, lot, "human:alice",
                                      actor="human:alice", remove=True)
        self.assertEqual(row.get("reviewer_settled"), [])
        kept = self.db.query(
            "SELECT status FROM agent_mailbox WHERE id = %s", (decision,))[0]
        self.assertEqual(kept["status"], "pending")


class ProfileJournalTest(_ActivationFixture, PgTestCase):
    """Un profil illisible est refusé **et** journalisé sur le lot (#4779)."""

    def test_an_unreadable_capability_is_journaled_on_the_lot(self):
        self.db.execute("UPDATE agent_registry SET capabilities = NULL"
                        " WHERE name = 'r1'")
        _row, _out = self.freeze()
        notes = [n for n in self.notes()
                 if str(n).startswith(act.PROFILE_UNREADABLE_PREFIX)]
        self.assertTrue(notes, self.notes())
        self.assertTrue(any("r1" in note and rd.CAPABILITIES_UNREADABLE in note
                            for note in notes), notes)


class ManualCanaryFreezeTest(_ActivationFixture, PgTestCase):
    """Le siège manuel d'un canari ne couvre aucun avis au gel (B1 cx2)."""

    def test_a_manual_read_only_seat_does_not_cover_a_required_opinion(self):
        registry.upsert(self.db, "canary", harness="claude", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute(
            "UPDATE agent_registry SET team = 'ameesh', capabilities ="
            " ARRAY['read'], roles = ARRAY[]::text[] WHERE name = 'canary'")
        self.db.execute("UPDATE work_items SET reviewers = ARRAY['canary']"
                        " WHERE id = %s", (self.lot,))
        _row, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        names = sorted(str((r or {}).get("name") or "")
                       for r in out.get("reviewers") or ())
        self.assertEqual(names, ["r1", "r2"], out)
        self.assertNotIn("canary", names)


if __name__ == "__main__":
    unittest.main()
