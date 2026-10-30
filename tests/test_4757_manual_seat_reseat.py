# SPDX-License-Identifier: AGPL-3.0-only
"""#4757 — un siège manuel en pause budget au-delà de l'échéance de l'avis.

Constat (nexlink, 29/10) : claude1, désigné à la main sur #4651 et #4707, est
resté « en pause budget jusqu'à 23:16 » puis « jusqu'à 00:57Z » ; les avis
claude ont attendu 1 h à 1 h 50, car la passe #3328 ne touchait jamais un
siège manuel.

Décision mesh-design (30/10 14:4xZ) : la passe rend un siège manuel quand
(1) le relecteur est injoignable pour `_unreachable_reviewer`, sans lire
l'heure « until » ; (2) l'échéance `green_verdict` est passée depuis la
désignation ; (3) il n'a pas de verdict sur la tête gelée ; (4) le remplaçant
est de la même famille ; (5) une seule fois par gel, journalisée, avec
courrier au désignateur. Sans remplaçant de la même famille, le siège reste
et le désignateur est prévenu.
"""
from __future__ import annotations

from ameesh import registry, storage, work
from ameesh import review_designation as rd
from ameesh import review_designation_activation as act

from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture

#: une pause de jauge (pas la fenêtre courte du compte, #3876)
BUDGET = "budget : claude/primaire 96 % >= 95 %, en pause jusqu'à 23:16"


class ManualSeatReseatTest(_ActivationFixture, PgTestCase):

    def _agent(self, name, harness):
        registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute(
            "UPDATE agent_registry SET team = 'ameesh', capabilities ="
            " ARRAY['read','report-drift','propose'], roles = ARRAY['implementation']"
            " WHERE name = %s", (name,))

    def _manual(self, name="m1", *, by="human:smichea"):
        """Le gel désigne r1 (claude) et r2 (codex), puis le pilote ajoute m1,
        enregistré après le gel pour que la sélection ne le prenne pas."""
        self.freeze()
        self._agent(name, "claude")
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (self.lot,))
        storage.of(self.db).work.designate_reviewer(
            self.lot, name, actor=by, event="reviewer %s designated" % name)

    def _pause(self, name="m1"):
        self.db.execute(
            "UPDATE agent_registry SET status = 'blocked', status_text = %s"
            " WHERE name = %s", (BUDGET, name))

    def _age(self, hours=3):
        """Le gel et la désignation datent de `hours` heures."""
        for table, column in (("work_item_milestones", "at"),
                              ("work_item_events", "created_at")):
            self.db.execute(
                "UPDATE %s SET %s = %s - make_interval(hours => %%s)"
                " WHERE work_item_id = %%s" % (table, column, column),
                (hours, self.lot))

    def _column(self):
        return sorted(self.db.query(
            "SELECT reviewers FROM work_items WHERE id = %s",
            (self.lot,))[0]["reviewers"] or [])

    def _alerts(self):
        return [m for m in self.mailbox()
                if "manual_reseat" in ((m["payload"] or {}).get("review_designation") or {})]

    def test_la_pause_budget_est_un_siege_injoignable(self):
        row = {"name": "m1", "status": "blocked", "status_text": BUDGET,
               "mode": "execute"}
        self.assertEqual(act._unreachable_reviewer(row, set()), "status blocked")

    def test_siege_manuel_en_pause_apres_l_echeance_va_a_la_meme_famille(self):
        self._agent("r3", "claude")
        self._manual()
        self._pause()
        self._age()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["manual_reseated"], 1, report)
        self.assertEqual(self._column(), ["r1", "r2", "r3"])
        requests = [m for m in self.mailbox()
                    if m["recipient"] == "r3" and m["kind"] == rd.REQUEST_KIND]
        self.assertEqual(len(requests), 1)
        notes = self.notes(act.MANUAL_RESEAT_PREFIX)
        self.assertEqual(len(notes), 1)
        self.assertIn("m1 (status blocked) → r3", notes[0])
        self.assertIn("reviewer r3 designated", self.notes("reviewer r3 "))
        self.assertIn("reviewer m1 removed", self.notes("reviewer m1 "))
        # m1 désigné par un humain : le courrier part au pilote du lot.
        self.assertEqual(len(self._alerts()), 1)
        # r3 reste un siège manuel au regel : écrit comme le geste du pilote.
        self.assertIn("r3", act._manual(self.db, work.get(self.db, self.lot)))
        mails = len(self.mailbox())
        again = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(again["manual_reseated"], 0, again)
        self.assertEqual(len(self.mailbox()), mails)
        self.assertEqual(len(self.notes(act.MANUAL_RESEAT_PREFIX)), 1)

    def test_un_relecteur_retire_par_le_pilote_ne_revient_pas(self):
        # r3 (claude) a été retiré à la main sur ce gel : le remplaçant de m1
        # est r5, jamais r3.
        self._agent("r3", "claude")
        self._agent("r5", "claude")
        self._manual()
        storage.of(self.db).work.designate_reviewer(
            self.lot, "r3", actor="human:smichea", event="reviewer r3 designated")
        storage.of(self.db).work.designate_reviewer(
            self.lot, "r3", actor="human:smichea", remove=True,
            event="reviewer r3 removed")
        self._pause()
        self._age()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["manual_reseated"], 1, report)
        self.assertEqual(self._column(), ["r1", "r2", "r5"])

    def test_avant_l_echeance_le_siege_reste(self):
        self._agent("r3", "claude")
        self._manual()
        self._pause()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["manual_reseated"], 0, report)
        self.assertEqual(self._column(), ["m1", "r1", "r2"])
        self.assertFalse(self.notes(act.MANUAL_RESEAT_PREFIX))

    def test_un_relecteur_joignable_garde_son_siege(self):
        self._agent("r3", "claude")
        self._manual()
        self._age()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["manual_reseated"], 0, report)
        self.assertEqual(self._column(), ["m1", "r1", "r2"])
        self.assertFalse(self.notes(act.MANUAL_RESEAT_PREFIX))

    def test_un_verdict_sur_la_tete_garde_le_siege(self):
        self._agent("r3", "claude")
        self._manual()
        work.milestone(self.db, self.lot, "verdict", sha=self.frozen, actor="m1",
                       verdict="ok", repo=self.repo.root, after_commit=[])
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " SELECT 'milestone:' || id, %s, 'm1', 's-claude', 't', 'claude', %s"
            "   FROM work_item_milestones WHERE work_item_id = %s AND kind = 'verdict'",
            (self.frozen, self.lot, self.lot))
        self._pause()
        self._age()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["manual_reseated"], 0, report)
        self.assertIn("m1", self._column())

    def test_sans_remplacant_de_la_meme_famille_le_siege_reste(self):
        # r1 (claude) siège déjà, pilot (claude) est le seul autre : on le
        # met en pause aussi. Un codex libre (r4) n'est pas un remplaçant.
        self._agent("r4", "codex")
        self._manual()
        self.db.execute("UPDATE agent_registry SET operator_paused = true"
                        " WHERE name = 'pilot'")
        self._pause()
        self._age()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["manual_kept"], 1, report)
        self.assertEqual(self._column(), ["m1", "r1", "r2"])
        notes = self.notes(act.MANUAL_RESEAT_PREFIX)
        self.assertEqual(len(notes), 1)
        self.assertIn("kept: no claude reviewer available", notes[0])
        self.assertEqual(len(self._alerts()), 1)
        again = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(again["manual_kept"], 0, again)
        self.assertEqual(len(self._alerts()), 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
