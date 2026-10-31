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

import dataclasses
from unittest import mock

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

    def _redesignate(self, name="r1"):
        """r1, automatique au gel, est redésigné à la main par le pilote (#3602)."""
        storage.of(self.db).work.designate_reviewer(
            self.lot, name, actor="human:smichea", remove=True,
            event="reviewer %s removed" % name)
        storage.of(self.db).work.designate_reviewer(
            self.lot, name, actor="human:smichea",
            event="reviewer %s designated" % name)

    def _frozen_qa(self):
        self.freeze()
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (self.lot,))

    def test_redesigne_a_la_main_avant_l_echeance_aucun_chemin_ne_le_prend(self):
        # Le gel est vieux, le geste du pilote récent : le délai manuel court
        # depuis le geste, y compris pour la passe et pour le rejeu --apply.
        self._agent("r3", "claude")
        self._frozen_qa()
        self._age()
        self._redesignate()
        self._pause("r1")
        self.assertIn("r1", act._manual(self.db, work.get(self.db, self.lot)))
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["manual_reseated"], 0, report)
        self.assertEqual(report["reassigned"], 0, report)
        self.assertEqual(self._column(), ["r1", "r2"])
        act.reviewers(self.cfg, self.db, self.lot, apply=True)
        self.assertEqual(self._column(), ["r1", "r2"])

    def test_redesigne_a_la_main_apres_l_echeance_suit_le_chemin_manuel(self):
        self._agent("r3", "claude")
        self._agent("r4", "codex")
        self._frozen_qa()
        self._redesignate()
        self._pause("r1")
        self._age()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["manual_reseated"], 1, report)
        self.assertEqual(report["reassigned"], 0, report)
        self.assertEqual(self._column(), ["r2", "r3"])
        self.assertEqual(len(self._alerts()), 1)

    def test_redesigne_sans_remplacant_le_chemin_automatique_ne_le_prend_pas(self):
        # Un codex libre (r4) n'est pas un remplaçant de r1 (claude).
        self._agent("r4", "codex")
        self._frozen_qa()
        self.db.execute("UPDATE agent_registry SET operator_paused = true"
                        " WHERE name = 'pilot'")
        self._redesignate()
        self._pause("r1")
        self._age()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["manual_kept"], 1, report)
        self.assertEqual(report["reassigned"], 0, report)
        self.assertEqual(self._column(), ["r1", "r2"])

    def test_siege_automatique_mort_a_cote_d_un_manuel_le_manuel_reste_manuel(self):
        self._agent("r4", "codex")
        self._frozen_qa()
        self._age()
        self._redesignate()
        self._pause("r2")
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["reassigned"], 1, report)
        self.assertEqual(self._column(), ["r1", "r4"])
        self.assertIn("r1", act._manual(self.db, work.get(self.db, self.lot)))

    def test_famille_de_l_auteur_le_siege_reste(self):
        # Auteur claude : r3 (claude) ne prouve aucune indépendance.
        self._agent("r3", "claude")
        self._manual()
        self.db.execute("UPDATE agent_registry SET harness = 'claude'"
                        " WHERE name = 'author'")
        self.db.execute("UPDATE provenance_receipts SET harness = 'claude'"
                        " WHERE work_item_id = %s", (self.lot,))
        self.db.execute("UPDATE turn_costs SET harness = 'claude' WHERE agent = 'author'")
        self._pause()
        self._age()
        inputs = act.prepare(self.cfg, self.db, self.lot,
                             freeze=act._latest_freeze(self.db, self.lot))
        self.assertIn("claude", act._head_families(inputs))
        self.assertIsNone(act._same_family_replacement(inputs, "claude", set()))
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["manual_reseated"], 0, report)
        self.assertEqual(report["manual_kept"], 1, report)
        self.assertEqual(self._column(), ["m1", "r1", "r2"])
        self.assertIn("kept: family claude wrote this head",
                      self.notes(act.MANUAL_RESEAT_PREFIX)[0])
        self.assertEqual(len(self._alerts()), 1)

    def test_provenance_inconnue_le_siege_reste(self):
        self._agent("r3", "claude")
        self._manual()
        self._pause()
        self._age()
        prepare = act.prepare

        def unproven(*args, **kwargs):
            return dataclasses.replace(prepare(*args, **kwargs),
                                       unknown_authors=["abc123"])

        with mock.patch.object(act, "prepare", unproven):
            report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["manual_reseated"], 0, report)
        self.assertEqual(report["manual_kept"], 1, report)
        self.assertEqual(self._column(), ["m1", "r1", "r2"])
        self.assertIn("kept: author provenance unknown",
                      self.notes(act.MANUAL_RESEAT_PREFIX)[0])
        self.assertEqual(len(self._alerts()), 1)

    def _during_prepare(self, gesture):
        """`gesture` s'intercale entre la lecture de la passe et le verrou."""
        prepare = act.prepare

        def racing(*args, **kwargs):
            gesture()
            return prepare(*args, **kwargs)

        return mock.patch.object(act, "prepare", racing)

    def _assert_race_lost(self, report):
        self.assertEqual(report["manual_reseated"], 0, report)
        self.assertEqual(report["manual_kept"], 0, report)
        self.assertFalse(self.notes(act.MANUAL_RESEAT_PREFIX))
        self.assertFalse(self._alerts())

    def test_redesigne_pendant_prepare_le_nouveau_delai_vaut(self):
        self._agent("r3", "claude")
        self._manual()
        self._pause()
        self._age()
        with self._during_prepare(lambda: self._redesignate("m1")):
            report = act.reassign_unreachable(self.cfg, self.db)
        self._assert_race_lost(report)
        self.assertEqual(self._column(), ["m1", "r1", "r2"])

    def test_retour_du_relecteur_pendant_prepare_le_siege_reste(self):
        self._agent("r3", "claude")
        self._manual()
        self._pause()
        self._age()
        back = lambda: self.db.execute(  # noqa: E731
            "UPDATE agent_registry SET status = 'idle', status_text = ''"
            " WHERE name = 'm1'")
        with self._during_prepare(back):
            report = act.reassign_unreachable(self.cfg, self.db)
        self._assert_race_lost(report)
        self.assertEqual(self._column(), ["m1", "r1", "r2"])

    def test_retrait_ecrit_dans_une_note_libre_n_exclut_personne(self):
        self._agent("r3", "claude")
        self._manual()
        storage.of(self.db).work.note(self.lot, "qa", "reviewer r3 removed", "r2")
        self._pause()
        self._age()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["manual_reseated"], 1, report)
        self.assertEqual(self._column(), ["r1", "r2", "r3"])

    def _retire(self, name):
        self.db.execute("UPDATE agent_registry SET retired_at = now()"
                        " WHERE name = %s", (name,))

    def test_l_assigne_n_est_jamais_le_remplacant(self):
        # r5 (claude) tient le lot : seul candidat claude, il ne relit pas
        # son propre lot (#705) ; le siège reste, noté et signalé.
        self._agent("r5", "claude")
        self._manual()
        self.db.execute("UPDATE work_items SET assignee = 'r5' WHERE id = %s",
                        (self.lot,))
        self.db.execute("UPDATE agent_registry SET operator_paused = true"
                        " WHERE name = 'pilot'")
        self._pause()
        self._age()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["manual_kept"], 1, report)
        self.assertFalse(report.get("errors"), report)
        self.assertEqual(self._column(), ["m1", "r1", "r2"])
        notes = self.notes(act.MANUAL_RESEAT_PREFIX)
        self.assertEqual(len(notes), 1)
        self.assertIn("kept: no claude reviewer available", notes[0])
        self.assertEqual(len(self._alerts()), 1)

    def test_deux_sieges_l_assigne_n_annule_pas_l_autre_remplacement(self):
        self._agent("r3", "claude")
        self._agent("r5", "claude")
        self._manual("m1")
        self._agent("m2", "claude")
        storage.of(self.db).work.designate_reviewer(
            self.lot, "m2", actor="human:smichea", event="reviewer m2 designated")
        self.db.execute("UPDATE work_items SET assignee = 'r5' WHERE id = %s",
                        (self.lot,))
        self.db.execute("UPDATE agent_registry SET operator_paused = true"
                        " WHERE name = 'pilot'")
        self._pause("m1")
        self._pause("m2")
        self._age()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertFalse(report.get("errors"), report)
        self.assertEqual(report["manual_reseated"], 1, report)
        self.assertEqual(self._column(), ["m2", "r1", "r2", "r3"])
        notes = self.notes(act.MANUAL_RESEAT_PREFIX)
        self.assertEqual(len(notes), 1)
        self.assertIn("m1 (status blocked) → r3", notes[0])
        self.assertIn("m2 (status blocked) kept", notes[0])

    def test_pilote_retire_le_siege_bouge_quand_meme(self):
        # m1 désigné par un humain : le repli est le pilote, retiré ; aucun
        # courrier ne peut partir, le remplacement et la note restent.
        self._agent("r3", "claude")
        self._manual()
        self._retire("pilot")
        self._pause()
        self._age()
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertFalse(report.get("errors"), report)
        self.assertEqual(report["manual_reseated"], 1, report)
        self.assertEqual(self._column(), ["r1", "r2", "r3"])
        notes = self.notes(act.MANUAL_RESEAT_PREFIX)
        self.assertEqual(len(notes), 2, notes)
        self.assertTrue(any("designator not told" in n for n in notes), notes)
        self.assertFalse(self._alerts())
        again = act.reassign_unreachable(self.cfg, self.db)
        self.assertFalse(again.get("errors"), again)
        self.assertEqual(len(self.notes(act.MANUAL_RESEAT_PREFIX)), 2)

    def test_designateur_retire_le_courrier_va_au_pilote(self):
        self._agent("d1", "codex")
        self._retire("d1")
        self.assertEqual(act._manual_reseat_recipient(self.db, "d1", "pilot"), "pilot")
        self.assertEqual(act._manual_reseat_recipient(self.db, "r2", "pilot"), "r2")
        self._retire("pilot")
        self.assertEqual(act._manual_reseat_recipient(self.db, "d1", "pilot"), "")
        self.assertEqual(act._manual_reseat_recipient(self.db, "human:smichea", "pilot"), "")

    def test_un_courrier_refuse_n_annule_pas_le_remplacement(self):
        from ameesh import mail
        self._agent("r3", "claude")
        self._manual()
        self._pause()
        self._age()
        real = mail.send

        def send(db, sender, recipient, body, **kw):
            if "manual_reseat" in ((kw.get("payload") or {})
                                   .get("review_designation") or {}):
                raise RuntimeError("refused")
            return real(db, sender, recipient, body, **kw)

        with mock.patch.object(mail, "send", send):
            report = act.reassign_unreachable(self.cfg, self.db)
        self.assertFalse(report.get("errors"), report)
        self.assertEqual(report["manual_reseated"], 1, report)
        self.assertEqual(self._column(), ["r1", "r2", "r3"])
        self.assertTrue(any("designator not told" in n
                            for n in self.notes(act.MANUAL_RESEAT_PREFIX)))


if __name__ == "__main__":
    import unittest
    unittest.main()
