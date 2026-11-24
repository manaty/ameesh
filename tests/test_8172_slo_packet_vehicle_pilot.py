# SPDX-License-Identifier: AGPL-3.0-only
"""#8172 — SLO « review GO → merge » : un véhicule de paquet va au pilote.

Constat (#7678 nexlink, 23/11 ; diagnostic corrigé sur #8156) : le train
nexlink est vivant, mais `train_candidates` exclut par conception un porteur
`integration/**` comme unité (`packet-vehicle`). Un lot validé qui est son
propre véhicule n'est jamais pris par le train : le pilote le fusionne à la
main (§12). L'alerte sonnait pourtant vers le porteur.

Attendu : l'alerte d'un véhicule validé nomme le pilote du projet du lot et
dit « packet vehicle: merged by the pilot (§12), the train does not take
it », train vivant ou non. Un lot ordinaire garde le comportement de #8159.
"""
from __future__ import annotations

import dataclasses
from unittest import mock

from ameesh import alert_briefs, exploitation, registry, work

from .support import PgTestCase
from .test_8159_slo_no_train_pilot import HEURE, PHRASE

VEHICLE = alert_briefs.PACKET_VEHICLE_MERGE


class SloPacketVehicleTest(PgTestCase):
    """Le chemin réel : un lot nexlink validé dont la fusion tarde."""

    GO = 1_792_950_000.0
    HEAD = "c" * 40
    AGENTS = ("dev1", "orch-nx", "orch-amh", "auditeur")

    def setUp(self):
        super().setUp()
        for name in self.AGENTS:
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET lease_owner = 'runner-' || name,"
            " lease_expires_at = now() + interval '1 hour'")
        # le train d'un test précédent ne survit pas au suivant
        self.db.execute("DELETE FROM integration_trains")
        self.db.execute("DELETE FROM merge_locks")
        self.now = self.GO + 65 * 60.0

    def lot(self, branch):
        item = work.add(self.db, title="lot nexlink", app="nexlink", branch=branch,
                        actor="test", assignee="dev1")
        work.move(self.db, item["id"], "build")
        work.move(self.db, item["id"], "qa")
        created = self.GO - 2 * HEURE
        self.db.execute(
            "UPDATE work_items SET created_at = to_timestamp(%s), branch_head = %s"
            " WHERE id = %s", (created, self.HEAD, item["id"]))
        self.db.execute(
            "UPDATE work_item_events SET created_at = to_timestamp(%s)"
            " WHERE work_item_id = %s AND state = 'build'", (created + 1800.0, item["id"]))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor)"
            " VALUES (%s, 'frozen', to_timestamp(%s), 'dev1')", (item["id"], self.GO - 1))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor,"
            " verdict) VALUES (%s, 'verdict', to_timestamp(%s), 'rel', 'ok')",
            (item["id"], self.GO))
        return item["id"]

    def live_train(self):
        """Le train nexlink vivant du 23/11 : cible centrale fraîche tenue par
        l'exécuteur d'un bail `train-service` vivant, sur un autre hôte."""
        self.db.execute(
            "INSERT INTO merge_locks (target, owner, epoch, expires_at)"
            " VALUES ('nexlink:develop', NULL, 1, NULL)")
        self.db.execute(
            "INSERT INTO integration_trains (target_key, document, updated_at)"
            " VALUES ('nexlink:develop', %s::jsonb, now() - interval '1 minute')",
            ('{"reservation": {"key": "nexlink:develop",'
             ' "owner": "train:runner-nx@autre-hote"}}',))
        self.db.execute(
            "INSERT INTO merge_locks (target, owner, epoch, expires_at)"
            " VALUES ('train-service', 'train-service:runner-nx@autre-hote:abc',"
            " 1, now() + interval '10 minutes')")

    def alerts(self, lot):
        cfg = dataclasses.replace(self.cfg, train={}, radar={})
        args = exploitation.build_parser().parse_args(["alerts"])
        seuils = exploitation.thresholds(args)
        seuils.update(orchestrators_declared=["orch-nx", "orch-amh"])
        with mock.patch.object(exploitation, "_review_gate_open", return_value=True):
            rows = exploitation.alerts(cfg, self.db, now=self.now, **seuils)
        return [row for row in rows if row["type"] == "slo_breach"
                and row["lot"] == lot and row["step"] == "verdict_merge"]

    def briefs(self, alert, porter_team="ameesh"):
        # le porteur n'est pas de l'équipe nexlink : le pilote visé est celui
        # du projet du lot, jamais celui des équipes du porteur
        listing = [{"name": "dev1", "team": porter_team},
                   {"name": "orch-nx", "team": "nexlink"},
                   {"name": "orch-amh", "team": "ameesh"},
                   {"name": "auditeur", "team": ""}]
        return alert_briefs.briefs(alert, listing,
                                   orchestrator_names=("orch-nx", "orch-amh"),
                                   auditor_names=("auditeur",))

    def assert_vehicle_to_pilot(self, alert):
        waiting = alert["waiting"] or {}
        self.assertTrue(waiting.get("self"))
        self.assertEqual(waiting.get("packet_vehicle"), "nexlink")
        self.assertEqual(alert["targets"][0], "pilot")
        self.assertNotIn("assignee", alert["targets"])
        self.assertIn(VEHICLE, alert["detail"])
        self.assertNotIn("no train declared", alert["detail"])
        briefs = self.briefs(alert)
        self.assertEqual(set(briefs) & {"dev1", "orch-amh"}, set(), briefs)
        self.assertIn("orch-nx", briefs)
        self.assertIn(VEHICLE, briefs["orch-nx"])
        self.assertNotIn("no train declared", briefs["orch-nx"])
        text = alert_briefs._slo_breach(dict(alert, level=3))
        self.assertIn(VEHICLE, text)
        self.assertIn("push the pilot of nexlink to merge it", text)

    def test_vehicule_valide_avec_un_train_vivant(self):
        self.live_train()
        lot = self.lot("integration/7678-agplv3-source")
        alerts = self.alerts(lot)
        self.assertEqual(len(alerts), 1, alerts)
        self.assertNotIn("no_train", alerts[0]["waiting"])
        self.assert_vehicle_to_pilot(alerts[0])

    def test_vehicule_valide_sans_train(self):
        lot = self.lot("integration/7678-agplv3-source")
        alerts = self.alerts(lot)
        self.assertEqual(len(alerts), 1, alerts)
        self.assertEqual(alerts[0]["waiting"].get("no_train"), "nexlink")
        self.assert_vehicle_to_pilot(alerts[0])

    def test_lot_ordinaire_avec_un_train_vivant(self):
        self.live_train()
        lot = self.lot("lot/8172-ordinaire")
        alerts = self.alerts(lot)
        self.assertEqual(len(alerts), 1, alerts)
        for alert in alerts:
            waiting = alert["waiting"] or {}
            self.assertEqual(waiting.get("what"), "merge")
            self.assertNotIn("packet_vehicle", waiting)
            self.assertNotIn("no_train", waiting)
            self.assertNotIn(VEHICLE, alert["detail"])
            self.assertNotIn(PHRASE, alert["detail"])
            for text in self.briefs(alert, porter_team="nexlink").values():
                self.assertNotIn(VEHICLE, text)
