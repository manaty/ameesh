# SPDX-License-Identifier: AGPL-3.0-only
"""#8159 — SLO « review GO → merge » : sans train déclaré, le pilote du projet.

Constat (#8156, orchestrateur nexlink, 23/11 15:49Z) : aucun train ni radar
n'est déclaré pour nexlink ; le paquet est composé à la main par le pilote
(§12). Les niveaux 1 et 2 du SLO de #7678 attendaient pourtant un geste du
train, qui n'existe pas.

Attendu : quand le projet du lot ne déclare ni train ni radar à la
configuration de l'hôte, l'alerte d'un porteur validé ou d'un véhicule de
paquet vise d'abord le pilote du projet, et son texte dit « no train declared:
manual composition by the pilot (§12) ». Un projet qui a un train (ou un
radar) garde le comportement actuel.
"""
from __future__ import annotations

import dataclasses
import unittest
from unittest import mock

from ameesh import alert_briefs, exploitation, registry, work

from .support import PgTestCase

HEURE = 3600.0
PHRASE = "no train declared for nexlink: manual composition by the pilot (§12)"


class TrainDeclaredTest(unittest.TestCase):
    """La lecture de la configuration de l'hôte et de l'état central."""

    def cfg(self, **blocks):
        return mock.Mock(spec=["train", "radar"], train=blocks.get("train", {}),
                         radar=blocks.get("radar", {}))

    def test_sans_train_ni_radar_le_projet_n_est_pas_declare(self):
        self.assertFalse(exploitation._train_declared(self.cfg(), "nexlink"))

    def test_un_train_ou_un_radar_du_projet_le_declare(self):
        self.assertTrue(exploitation._train_declared(
            self.cfg(train={"projects": {"nexlink": {}}}), "nexlink"))
        self.assertTrue(exploitation._train_declared(
            self.cfg(radar={"projects": {"nexlink": {"repo": "/r"}}}), "nexlink"))

    def test_le_train_d_un_autre_projet_ne_compte_pas(self):
        self.assertFalse(exploitation._train_declared(
            self.cfg(train={"projects": {"ameesh": {}}},
                     radar={"projects": {"ameesh": {}}}), "nexlink"))

    def test_un_bloc_illisible_garde_le_comportement_declare(self):
        self.assertTrue(exploitation._train_declared(
            self.cfg(radar=["nexlink"]), "nexlink"))
        self.assertTrue(exploitation._train_declared(
            self.cfg(train={"projects": ["nexlink"]}), "nexlink"))

    def test_un_train_vivant_vu_a_l_etat_central_le_declare(self):
        # B1 de ameesh-dev3 : le train d'un autre hôte compte, cfg local vide
        self.assertTrue(exploitation._train_declared(self.cfg(), "nexlink",
                                                     {"nexlink"}))
        self.assertFalse(exploitation._train_declared(self.cfg(), "nexlink",
                                                      {"ameesh"}))

    def test_un_etat_central_illisible_garde_le_comportement_declare(self):
        self.assertTrue(exploitation._train_declared(self.cfg(), "nexlink", None))


class TargetsTest(unittest.TestCase):
    """Les rôles d'escalade d'une attente de paquet sans train."""

    def test_le_pilote_passe_en_tete_a_tout_palier(self):
        waiting = {"what": "packet", "self": True, "no_train": "nexlink"}
        for targets in (["assignee"], ["pilot"], ["avatar", "auditeur", "pilot"]):
            got = exploitation._slo_targets(
                {"step": "verdict_merge", "targets": targets}, waiting)
            self.assertEqual(got[0], "pilot", got)
            self.assertNotIn("assignee", got)
            self.assertEqual(len(got), len(set(got)))

    def test_avec_train_les_cibles_sont_inchangees(self):
        waiting = {"what": "packet", "self": True}
        self.assertEqual(exploitation._slo_targets(
            {"step": "verdict_merge", "targets": ["avatar", "auditeur", "pilot"]},
            waiting), ["avatar", "auditeur", "pilot"])


class SloNoTrainTest(PgTestCase):
    """Le chemin réel : un véhicule nexlink validé dont la CI n'a pas démarré."""

    GO = 1_792_950_000.0
    HEAD = "b" * 40
    AGENTS = ("dev1", "orch-nx", "orch-amh", "auditeur")

    def setUp(self):
        super().setUp()
        for name in self.AGENTS:
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET lease_owner = 'runner-' || name,"
            " lease_expires_at = now() + interval '1 hour'")
        self.carrier = work.add(self.db, title="porteur nexlink",
                                app="nexlink", branch="integration/7678-test",
                                actor="test", assignee="dev1")
        work.move(self.db, self.carrier["id"], "build")
        work.move(self.db, self.carrier["id"], "qa")
        created = self.GO - 2 * HEURE
        self.db.execute(
            "UPDATE work_items SET created_at = to_timestamp(%s), branch_head = %s"
            " WHERE id = %s", (created, self.HEAD, self.carrier["id"]))
        self.db.execute(
            "UPDATE work_item_events SET created_at = to_timestamp(%s)"
            " WHERE work_item_id = %s AND state = 'build'",
            (created + 1800.0, self.carrier["id"]))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor)"
            " VALUES (%s, 'frozen', to_timestamp(%s), 'dev1')",
            (self.carrier["id"], self.GO - 1))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor,"
            " verdict) VALUES (%s, 'verdict', to_timestamp(%s), 'rel', 'ok')",
            (self.carrier["id"], self.GO))
        # niveau 1 : le créneau suit le GO, la cible est de 30 min
        self.now = self.GO + 65 * 60.0

    def alerts(self, cfg):
        args = exploitation.build_parser().parse_args(["alerts"])
        seuils = exploitation.thresholds(args)
        seuils.update(orchestrators_declared=["orch-nx", "orch-amh"])
        with mock.patch.object(exploitation, "_review_gate_open",
                               return_value=True):
            rows = exploitation.alerts(cfg, self.db, now=self.now, **seuils)
        return [row for row in rows
                if row["type"] == "slo_breach"
                and row["lot"] == self.carrier["id"]
                and row["step"] == "verdict_merge"]

    def briefs(self, alert):
        listing = [{"name": "dev1", "team": "nexlink"},
                   {"name": "orch-nx", "team": "nexlink"},
                   {"name": "orch-amh", "team": "ameesh"},
                   {"name": "auditeur", "team": ""}]
        return alert_briefs.briefs(alert, listing,
                                   orchestrator_names=("orch-nx", "orch-amh"),
                                   auditor_names=("auditeur",))

    def test_sans_train_l_alerte_va_au_pilote_du_projet(self):
        cfg = dataclasses.replace(self.cfg, train={}, radar={})
        alerts = self.alerts(cfg)
        self.assertEqual(len(alerts), 1, alerts)
        alert = alerts[0]
        self.assertEqual(alert["level"], 1)
        waiting = alert["waiting"] or {}
        self.assertTrue(waiting.get("self"))
        self.assertEqual(waiting.get("no_train"), "nexlink")
        self.assertEqual(alert["targets"][0], "pilot")
        self.assertNotIn("assignee", alert["targets"])
        self.assertIn(PHRASE, alert["detail"])
        briefs = self.briefs(alert)
        self.assertIn("orch-nx", briefs)
        self.assertNotIn("dev1", briefs)
        self.assertNotIn("orch-amh", briefs)
        self.assertIn(PHRASE, briefs["orch-nx"])
        self.assertIn("dispatch the full CI yourself", briefs["orch-nx"])

    def test_le_pilote_est_celui_du_projet_pas_celui_de_l_equipe_du_porteur(self):
        # B2 de ameesh-dev3 : l'assigné du porteur n'est pas du projet du lot
        cfg = dataclasses.replace(self.cfg, train={}, radar={})
        alert = self.alerts(cfg)[0]
        for team in ("ameesh", ""):
            with self.subTest(team=team):
                listing = [{"name": "dev1", "team": team},
                           {"name": "orch-nx", "team": "nexlink"},
                           {"name": "orch-amh", "team": "ameesh"},
                           {"name": "auditeur", "team": ""}]
                briefs = alert_briefs.briefs(
                    alert, listing, orchestrator_names=("orch-nx", "orch-amh"),
                    auditor_names=("auditeur",))
                self.assertIn("orch-nx", briefs)
                self.assertNotIn("orch-amh", briefs)
                self.assertNotIn("dev1", briefs)

    def live_train(self, *, age_s=60.0, lease=True):
        """Un train nexlink tenu par un AUTRE hôte : cible centrale et bail."""
        self.db.execute(
            "INSERT INTO merge_locks (target, owner, epoch, expires_at)"
            " VALUES ('nexlink:develop', 'train:autre-hote', 1, now() + interval '1 hour')")
        self.db.execute(
            "INSERT INTO integration_trains (target_key, document, updated_at)"
            " VALUES ('nexlink:develop', '{}'::jsonb, now() - make_interval(secs => %s))",
            (age_s,))
        if lease:
            self.db.execute(
                "INSERT INTO merge_locks (target, owner, epoch, expires_at)"
                " VALUES ('train-service:train-role:3', 'train-service:autre-hote:x',"
                " 1, now() + interval '10 minutes')")

    def test_un_train_vivant_sur_un_autre_hote_garde_le_comportement_actuel(self):
        self.live_train()
        cfg = dataclasses.replace(self.cfg, train={}, radar={})
        alert = self.alerts(cfg)[0]
        self.assertNotIn("no_train", alert["waiting"] or {})
        self.assertNotIn("no train declared", alert["detail"])

    def test_une_cible_ancienne_ou_sans_bail_ne_fait_pas_un_train(self):
        for kwargs in ({"age_s": 3 * 24 * HEURE}, {"lease": False}):
            with self.subTest(**kwargs):
                self.db.execute("DELETE FROM integration_trains")
                self.db.execute("DELETE FROM merge_locks")
                self.live_train(**kwargs)
                cfg = dataclasses.replace(self.cfg, train={}, radar={})
                alert = self.alerts(cfg)[0]
                self.assertEqual((alert["waiting"] or {}).get("no_train"), "nexlink")

    def test_le_niveau_3_pousse_le_pilote_du_projet(self):
        cfg = dataclasses.replace(self.cfg, train={}, radar={})
        alert = dict(self.alerts(cfg)[0], level=3)
        text = alert_briefs._slo_breach(alert)
        self.assertIn(PHRASE, text)
        self.assertIn("push the pilot of nexlink", text)

    def test_un_projet_avec_train_garde_le_comportement_actuel(self):
        for block in ({"train": {"projects": {"nexlink": {}}}},
                      {"radar": {"projects": {"nexlink": {}}}}):
            with self.subTest(block=sorted(block)):
                cfg = dataclasses.replace(self.cfg, **block)
                alerts = self.alerts(cfg)
                self.assertEqual(len(alerts), 1, alerts)
                alert = alerts[0]
                self.assertNotIn("no_train", alert["waiting"] or {})
                self.assertNotIn("no train declared", alert["detail"])
                for text in self.briefs(alert).values():
                    self.assertIn("itself the packet vehicle", text)
                    self.assertNotIn("no train declared", text)


if __name__ == "__main__":
    unittest.main()
