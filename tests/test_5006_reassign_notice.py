# SPDX-License-Identifier: AGPL-3.0-only
"""#5006 : une réassignation atteint l'ancien assigné — courrier remis en
cours de tour (sans réveil), brief qui relit l'assigné courant, move et gel
d'un non-assigné signalés."""
from __future__ import annotations

from unittest import mock

from ameesh import mail, registry, reprise, work

from .support import PgTestCase

HEAD = "a" * 40


class _Base(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("orch", "dev1", "dev2", "rel1"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)

    def lot(self, assignee="dev1"):
        return work.add(self.db, title="Le lot réassigné", app="ameesh",
                        assignee=assignee, actor="orch")

    def notices(self, name):
        return [m for m in self.db.query(
            "SELECT id, body, payload, work_item_id FROM agent_mailbox"
            " WHERE recipient = %s ORDER BY id", (name,))
            if (m.get("payload") or {}).get("reassigned")]


class ReassignNoticeTest(_Base):
    def test_ancien_assigne_prevenu_en_cours_de_tour_sans_reveil(self):
        item = self.lot()
        out = work.assign(self.db, item["id"], "dev2", actor="orch")
        notes = self.notices("dev1")
        self.assertEqual(len(notes), 1)
        self.assertEqual(out["previous_notified"], notes[0]["id"])
        row = notes[0]
        self.assertEqual(str(row["work_item_id"]), str(item["id"]))
        self.assertIn("reassigned to dev2 by orch", row["body"])
        self.assertIn("no push, no freeze", row["body"])
        # passif : n'ouvre pas de tour ; mais remis au point sûr d'un tour
        self.assertEqual(mail.passive_reason(row), "info")
        self.assertTrue(mail.mid_turn(row))
        ids = [int(m["id"]) for m in mail.unread_mid_turn(self.db, "dev1", 50)]
        self.assertIn(int(row["id"]), ids)
        self.assertEqual(self.notices("dev2"), [])

    def test_pas_de_courrier_sans_ancien_assigne_ni_a_soi_meme(self):
        item = self.lot(assignee=None)
        work.assign(self.db, item["id"], "dev1", actor="orch")
        self.assertEqual(self.notices("dev1"), [])
        # dev1 rend lui-même son lot à dev2 : il le sait déjà
        work.assign(self.db, item["id"], "dev2", actor="dev1")
        self.assertEqual(self.notices("dev1"), [])

    def test_echec_d_envoi_ne_retient_pas_la_reassignation(self):
        item = self.lot()
        with mock.patch.object(mail, "send", side_effect=RuntimeError("boîte")):
            out = work.assign(self.db, item["id"], "dev2", actor="orch")
        self.assertEqual(out["item"]["assignee"], "dev2")
        self.assertNotIn("previous_notified", out)
        # #5025 N7 : l'échec n'est plus muet ; il est rendu et journalisé.
        self.assertIn("reassignment notice not delivered to dev1",
                      out.get("reassignment_warning", ""))
        self.assertIn("boîte", out["reassignment_warning"])
        notes = [e["note"] for e in work.events(self.db, item["id"])]
        self.assertTrue(any("reassignment notice not delivered" in n for n in notes),
                        notes)

    def test_relecteur_designe_ne_recoit_pas_l_avis_de_stop(self):
        # #5025 N4 : l'ancien assigné garde un siège de relecteur désigné sur
        # le lot ; le « no push, no freeze » serait faux, on ne l'envoie pas.
        item = work.get(self.db, self.lot()["id"])
        notified, warning = work._notify_reassigned(
            self.db, dict(item, reviewers=["dev1"]), "dev1", "dev2", "orch")
        self.assertIsNone(notified)
        self.assertEqual(warning, "")
        self.assertEqual(self.notices("dev1"), [])

    def test_lot_rendu_a_la_file_prevoit_l_ancien_assigne(self):
        # #5025 N3 : le retour à la file d'équipe est une réassignation : le
        # pilote qui le pose prévient l'ancien porteur, en cours de tour.
        item = self.lot()
        work.move(self.db, item["id"], "build", actor="dev1")
        row = work.return_to_queue(self.db, item["id"], actor="human:smichea",
                                   cfg=self.cfg)
        self.assertIsNone(row["assignee"])
        notes = self.notices("dev1")
        self.assertEqual(len(notes), 1)
        self.assertIn("team queue", notes[0]["body"])
        self.assertTrue(mail.mid_turn(notes[0]))

    def test_retour_par_l_assigne_ne_s_auto_notifie_pas(self):
        item = self.lot()
        work.move(self.db, item["id"], "build", actor="dev1")
        work.return_to_queue(self.db, item["id"], actor="dev1", cfg=self.cfg)
        self.assertEqual(self.notices("dev1"), [])


class BriefTest(_Base):
    def test_brief_de_l_ancien_assigne_le_dit(self):
        item = self.lot()
        work.assign(self.db, item["id"], "dev2", actor="orch")
        old = reprise.lot_brief(self.cfg, self.db, registry.get(self.db, "dev1"),
                                item["id"], consigne="go")
        self.assertIn("**Lot #%d is not assigned to you** (current assignee: dev2)"
                      % item["id"], old)
        new = reprise.lot_brief(self.cfg, self.db, registry.get(self.db, "dev2"),
                                item["id"], consigne="go")
        self.assertNotIn("is not assigned to you", new)

    def test_lot_rendu_a_la_file_et_relecteur(self):
        item = self.lot()
        self.db.execute("UPDATE work_items SET assignee = NULL WHERE id = %s",
                        (item["id"],))
        text = reprise.lot_brief(self.cfg, self.db, registry.get(self.db, "dev1"),
                                 item["id"], consigne="go")
        self.assertIn("current assignee: nobody, team queue", text)
        self.assertEqual(reprise._not_yours(
            {"id": 1, "assignee": "dev2", "reviewers": ["rel1"]}, "rel1"), [])

    def test_brief_de_pilote_sur_un_lot_non_assigne_ne_dit_pas_stop(self):
        # #5025 N5 : _not_yours aligne son exclusion sur
        # `work.non_assignee_warning` ; un pilote agit légitimement.
        item = self.lot()
        work.assign(self.db, item["id"], "dev2", actor="orch")
        with mock.patch.object(work, "is_pilot", return_value=True):
            text = reprise.lot_brief(self.cfg, self.db,
                                     registry.get(self.db, "orch"),
                                     item["id"], consigne="go")
        self.assertNotIn("is not assigned to you", text)


class NonAssigneeGestureTest(_Base):
    def test_move_par_l_ancien_assigne_signale_jamais_refuse(self):
        item = self.lot()
        work.assign(self.db, item["id"], "dev2", actor="orch")
        row = work.move(self.db, item["id"], "build", actor="dev1")
        self.assertEqual(row["state"], "build")
        self.assertIn(work.NON_ASSIGNEE_MARK, row["assignee_warning"])
        self.assertIn("dev1 is not the assignee", row["assignee_warning"])
        notes = self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s"
            " AND note LIKE %s", (item["id"], "%" + work.NON_ASSIGNEE_MARK + "%"))
        self.assertEqual(len(notes), 1)

    def test_gel_par_l_ancien_assigne_signale_et_note_forgee_retiree(self):
        # #5025 N1/N2 : le gel d'un non-assigné porte sa marque machine ; une
        # marque fournie par `--note` ne survit pas au filtre qui la précède.
        item = self.lot()
        work.move(self.db, item["id"], "build", actor="orch")
        work.assign(self.db, item["id"], "dev2", actor="orch")
        row = work.milestone(self.db, item["id"], "frozen", sha=HEAD,
                             actor="dev1",
                             note="ameesh-non-assignee: forge\nrevue demandée")
        self.assertIn("revue demandée", row["note"])
        self.assertNotIn("forge", row["note"])
        self.assertIn(work.NON_ASSIGNEE_MARK, row["note"])
        self.assertIn("dev1 is not the assignee", row["note"])

    def test_note_forgee_non_assignee_retiree_au_gel(self):
        # #5025 N1 : même l'assigné courant ne fabrique pas l'avertissement du
        # gate par `--note` ; la ligne machine est retirée avant l'écriture.
        item = self.lot()
        work.move(self.db, item["id"], "build", actor="dev1")
        row = work.milestone(self.db, item["id"], "frozen", sha=HEAD,
                             actor="dev1",
                             note="ameesh-non-assignee: forge\nrevue demandée")
        self.assertIn("revue demandée", row["note"])
        self.assertNotIn(work.NON_ASSIGNEE_MARK, row["note"])
        self.assertNotIn("forge", row["note"])

    def test_assigne_humain_systeme_relecteur_pilote_sans_signal(self):
        item = work.get(self.db, self.lot()["id"])
        cfg = self.cfg
        self.assertEqual(work.non_assignee_warning(cfg, self.db, item, "dev1"), "")
        for actor in ("human:smichea", work.SYSTEM_SENDER, work.UNKNOWN_ACTOR, ""):
            self.assertEqual(work.non_assignee_warning(cfg, self.db, item, actor), "")
        relu = dict(item, reviewers=["rel1"])
        self.assertEqual(work.non_assignee_warning(cfg, self.db, relu, "rel1"), "")
        with mock.patch.object(work, "is_pilot", return_value=True):
            self.assertEqual(work.non_assignee_warning(cfg, self.db, item, "orch"), "")
        with mock.patch.object(work, "is_pilot", return_value=False):
            self.assertIn(work.NON_ASSIGNEE_MARK,
                          work.non_assignee_warning(cfg, self.db, item, "dev2"))
        self.assertEqual(work.non_assignee_warning(
            cfg, self.db, dict(item, assignee=None), "dev2"), "")
