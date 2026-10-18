# SPDX-License-Identifier: AGPL-3.0-only
"""#789 tranche 2 : l'alerte `slo_breach` (canon §9).

`exploitation.alerts()` mesure les étapes du §9 sur les lots ouverts et lève
une alerte par `(lot, étape, niveau)` ; `alert_briefs` l'adresse au niveau
atteint — 1× l'assigné, 2× le pilote, 3× le bureau et l'auditeur, l'owner en
plus pour un P1. Un lot en attente planifiée (dépendance ouverte) n'est pas en
retard : il est tu.
"""
from __future__ import annotations

import datetime
import json
import os
import time
import unittest
from unittest import mock

from ameesh import (alert_briefs, decisions, exploitation, notify, planned_waits,
                    registry, stagnation, storage, work, work_dependencies)
from ameesh.storage.postgres.operations import Operations

from .support import FAKEBIN, PgTestCase

HEURE = 3600.0
FAKE_NOTIFY_SEND = os.path.join(FAKEBIN, "notify-send")


def _seuils(**kw):
    args = exploitation.build_parser().parse_args(["alerts"])
    out = exploitation.thresholds(args)
    out.update(kw)
    return out


class _Base(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("orch", "dev1", "bureau", "auditeur"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
        # le lot reste en intake : « prêt, pas démarré » — l'étape qui court
        self.lot = work.add(self.db, title="un lot SLO", assignee="dev1")
        self.now = time.time()

    def age(self, seconds, lot=None):
        """Le lot est prêt (`created_at`) depuis `seconds`."""
        self.db.execute(
            "UPDATE work_items SET created_at = now() - make_interval(secs => %s)"
            " WHERE id = %s", (float(seconds), (lot or self.lot)["id"]))

    def slo(self, **kw):
        kw.setdefault("orchestrators_declared", ["orch"])
        return [a for a in exploitation.alerts(self.cfg, self.db, now=self.now,
                                               **_seuils(**kw))
                if a["type"] == "slo_breach"]

    def start(self, since_s=None):
        """Le lot démarre (build) ; `since_s` recule la transition."""
        work.move(self.db, self.lot["id"], "build")
        if since_s is not None:
            self.db.execute(
                "UPDATE work_item_events SET created_at = now() - make_interval(secs => %s)"
                " WHERE work_item_id = %s AND state = 'build'",
                (float(since_s), self.lot["id"]))

    def qa(self, verdict_s=3000.0):
        """Le lot passe en qa avec un verdict `ok` posé il y a `verdict_s`."""
        work.move(self.db, self.lot["id"], "build")
        work.move(self.db, self.lot["id"], "qa")
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor, verdict)"
            " VALUES (%s, 'verdict', now() - make_interval(secs => %s), 'rel', 'ok')",
            (self.lot["id"], float(verdict_s)))
        self.now = time.time()

    def qa_at(self, verdict_ts, *, created_s=7200.0, lot=None):
        """qa avec un verdict `ok` à l'instant absolu `verdict_ts` (#2335).

        Les autres étapes restent tenues : création `created_s` avant le
        verdict, build 30 min après la création (cible 1 h)."""
        lot = self.lot if lot is None else lot
        work.move(self.db, lot["id"], "build")
        work.move(self.db, lot["id"], "qa")
        created = float(verdict_ts) - float(created_s)
        self.db.execute("UPDATE work_items SET created_at = to_timestamp(%s)"
                        " WHERE id = %s", (created, lot["id"]))
        self.db.execute(
            "UPDATE work_item_events SET created_at = to_timestamp(%s)"
            " WHERE work_item_id = %s AND state = 'build'",
            (created + 1800.0, lot["id"]))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor, verdict)"
            " VALUES (%s, 'verdict', to_timestamp(%s), 'rel', 'ok')",
            (lot["id"], float(verdict_ts)))
        self.now = float(verdict_ts)

    def packet(self, body="", *, branch="integration/paquet-test", head=None):
        """Un porteur de paquet ouvert (#2335) : branche `integration/**`."""
        carrier = work.add(self.db, title="porteur de paquet test", body=body,
                           branch=branch, actor="test")
        if head:
            self.db.execute("UPDATE work_items SET branch_head = %s WHERE id = %s",
                            (head, carrier["id"]))
        return carrier

    def briefs(self, alert):
        listing = [{"name": n, "team": ""} for n in ("dev1", "orch", "bureau", "auditeur")]
        return alert_briefs.briefs(alert, listing, orchestrator_names=("orch",),
                                   auditor_names=("auditeur",))


class AlerteTest(_Base):
    def test_niveau_1_assigne(self):
        self.age(1.5 * HEURE)
        alerts = self.slo()
        self.assertEqual([(a["step"], a["level"], a["targets"], a["reason"]) for a in alerts],
                         [("intake_started", 1, ["assignee"], "intake_started/level-1")])
        alert = alerts[0]
        self.assertEqual((alert["lot"], alert["agent"], alert["critical"], alert["urgent"]),
                         (self.lot["id"], "dev1", False, False))
        self.assertEqual(alert["threshold"], 3600)
        self.assertAlmostEqual(alert["value"], 1.5 * HEURE - 3600, delta=5)
        self.assertIn("slo_breach", exploitation.ALERT_TYPES)
        self.assertIn("slo_breach", notify.TYPE_LABELS)
        self.assertIn("slo_breach", alert_briefs.BRIEFED_TYPES)

    def test_niveau_2_pilote(self):
        self.age(2.5 * HEURE)
        alerts = self.slo()
        self.assertEqual([(a["level"], a["targets"]) for a in alerts],
                         [(2, ["pilot"])])
        self.assertEqual(alerts[0]["orchestrators"], ["orch"])

    def test_niveau_2_ne_prend_pas_un_assigneur_pour_pilote(self):
        # #1982 : l'historique L94 (agents ayant confié des lots) ne fait pas
        # un pilote — l'heuristique `history=True` briefait des agents de
        # chantier sans mandat ; le niveau 2 vise le rôle (déclaré ou canon).
        registry.upsert(self.db, "dist", harness="claude", host=self.cfg.host)
        registry.upsert(self.db, "dev2", harness="claude", host=self.cfg.host)
        autre = work.add(self.db, title="à distribuer")
        work.assign(self.db, int(autre["id"]), "dev2", actor="dist")
        self.assertIn("dist", storage.of(self.db).operations.assigners(
            since_s=30 * 24 * HEURE))
        self.age(2.5 * HEURE)
        alerts = self.slo()
        self.assertEqual([(a["level"], a["targets"]) for a in alerts],
                         [(2, ["pilot"])])
        self.assertEqual(alerts[0]["orchestrators"], ["orch"])

    def test_p1_niveau_3_owner_et_urgent(self):
        self.db.execute("UPDATE work_items SET priority = 1 WHERE id = %s",
                        (self.lot["id"],))
        self.age(50 * 60)
        alerts = self.slo()
        self.assertEqual(len(alerts), 1, alerts)
        alert = alerts[0]
        self.assertEqual((alert["level"], alert["targets"]),
                         (3, ["bureau", "auditeur", "owner"]))
        self.assertEqual(alert["threshold"], 900)          # délai court du P1
        self.assertTrue(alert["critical"])
        self.assertTrue(alert["urgent"])

    def test_etape_tenue_sans_alerte(self):
        self.age(60)
        self.assertEqual(self.slo(), [])

    def test_attente_planifiee_exclue(self):
        bloqueur = work.add(self.db, title="bloqueur")
        work_dependencies.add(self.db, self.lot["id"], bloqueur["id"], actor="test")
        self.age(4 * HEURE)
        self.assertEqual(self.slo(), [])

    def test_etapes_tenues_puis_verdict_recent(self):
        # créé il y a 4 h, démarré 30 min plus tard, verdict ok il y a 5 min :
        # aucune étape en dépassement, seule la fusion reste à faire
        self.age(4 * HEURE)
        self.start(3.5 * HEURE)
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s",
                        (self.lot["id"],))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor, verdict)"
            " VALUES (%s, 'verdict', now() - interval '5 minutes', 'rel', 'ok')",
            (self.lot["id"],))
        self.assertEqual(self.slo(), [])


class PaquetTest(_Base):
    """#2335 : un paquet en cours est une attente planifiée, et l'horloge
    GO → fusion ne part qu'au premier créneau de paquet après le GO."""

    GO = datetime.datetime(2026, 10, 17, 23, 5, tzinfo=datetime.timezone.utc).timestamp()

    def test_lot_dans_un_paquet_ouvert_est_une_attente_planifiee(self):
        self.qa_at(self.GO)
        self.packet(body="unités : #%d @ abcdef0 #999999 @ cafebabe" % self.lot["id"])
        self.now = self.GO + 75 * 60
        self.assertEqual(self.slo(), [])
        rows = storage.of(self.db).operations.open_lots_activity(None)
        described = stagnation.describe(self.db, rows, now=self.now, open_items=rows)
        info = described[self.lot["id"]]
        self.assertEqual((info["planned_wait"] or {}).get("what"), "packet")
        self.assertEqual((info["waiting"] or {}).get("what"), "packet")
        self.assertIn("packet merge", info["waiting"]["label"])
        self.assertIn("packet merge", work_dependencies.wait_label(info["planned_wait"]))
        self.assertIsNone(info["stale"])

    def test_lot_exclu_en_prose_reste_signale(self):
        # B1 (relecture dev7) : un lot seulement cité (« exclu », « garder »)
        # n'est pas transporté ; son alerte ne doit pas être tue par le
        # porteur. Seule une unité nommée avec sa tête l'est.
        self.qa_at(self.GO)
        exclu = work.add(self.db, title="exclu du paquet", assignee="dev1")
        self.qa_at(self.GO, lot=exclu)
        self.packet(body="unités : #%d @ abcdef0 ; #%d exclu (verdict manquant)"
                    % (self.lot["id"], exclu["id"]))
        self.now = self.GO + 125 * 60         # 01:10, après le créneau 00:00
        alerts = self.slo()
        self.assertEqual([a["lot"] for a in alerts], [exclu["id"]], alerts)
        self.assertEqual(alerts[0]["step"], "verdict_merge")
        self.assertIn("pilot", alerts[0]["targets"])

    def test_non_inclus_avant_le_creneau_ne_signale_rien(self):
        # GO 23:05, premier créneau 00:00 : à 00:20, 20 min < cible 30 min.
        self.qa_at(self.GO)
        self.packet(body="unités : #999999 @ cafebabe")
        self.now = self.GO + 75 * 60          # 00:20
        self.assertEqual(self.slo(), [])

    def test_non_inclus_creneau_manque_signale_au_niveau_1(self):
        # 01:10 : le créneau 00:00 est passé et la cible de 30 min est
        # dépassée ; c'est le pilote qui porte la fusion dans le paquet.
        self.qa_at(self.GO)
        self.packet(body="unités : #999999 @ cafebabe")
        self.now = self.GO + 125 * 60         # 01:10
        alerts = self.slo()
        self.assertEqual(len(alerts), 1, alerts)
        self.assertEqual(alerts[0]["step"], "verdict_merge")
        self.assertGreaterEqual(alerts[0]["level"], 1)
        self.assertIn("pilot", alerts[0]["targets"])

    def test_les_mentions_en_prose_ne_sont_pas_des_unites(self):
        # Les quatre faux positifs réels de la relecture dev7 : un lot cité
        # dans une phrase (« exclu », « garder ») n'est pas transporté.
        rows = [{"id": i, "state": "qa"} for i in (2203, 1976, 2062, 1024)]
        carrier = {"id": 9001, "state": "qa", "branch": "integration/paquet-test",
                   "body": "Paquet ; #2203 exclu ; #1976 admis mais exclu du paquet ; "
                           "Périmètre enregistré au lot #2062 ; garder #1024."}
        self.assertEqual(planned_waits.packet_waits(rows, rows + [carrier]), {})

    def test_cadence_du_canon_sans_porteur_observe(self):
        # B2 (relecture dev7) : entre deux paquets (porteur précédent fusionné,
        # suivant pas encore ouvert), la cadence déclarée au canon s'applique
        # quand même ; l'horloge ne repart pas du GO.
        class Cadence:
            @staticmethod
            def next_packet(at):
                return float(at) + 3600.0
        with mock.patch.object(exploitation, "_packet_calendar", return_value=Cadence()):
            self.assertEqual(exploitation._packet_slot(
                None, self.GO, "ameesh", observed=False), self.GO + 3600.0)

    def test_creneau_horaire_utc_seulement_si_paquet_observe(self):
        self.assertEqual(exploitation._packet_slot(
            None, self.GO, "ameesh", observed=True), self.GO + 55 * 60)
        self.assertIsNone(exploitation._packet_slot(
            None, self.GO, "ameesh", observed=False))


class BriefsTest(_Base):
    def test_niveau_1_assigne_seul(self):
        self.age(1.5 * HEURE)
        out = self.briefs(self.slo()[0])
        self.assertEqual(sorted(out), ["dev1"])
        self.assertIn("SLO breach, level 1", out["dev1"])
        self.assertIn("late by 30 min (target 1 h)", out["dev1"])
        self.assertIn("ameesh work note %d" % self.lot["id"], out["dev1"])

    def test_niveau_2_pilote_seul(self):
        self.age(2.5 * HEURE)
        out = self.briefs(self.slo()[0])
        self.assertEqual(sorted(out), ["orch"])
        self.assertIn("pilot: reassign", out["orch"])
        self.assertIn("late by 1 h 30 (target 1 h)", out["orch"])

    def test_niveau_3_bureau_et_auditeur(self):
        self.db.execute("UPDATE work_items SET priority = 1 WHERE id = %s",
                        (self.lot["id"],))
        self.age(50 * 60)
        out = self.briefs(self.slo()[0])
        self.assertEqual(sorted(out), ["auditeur", "bureau"])
        self.assertIn("bureau or auditor", out["bureau"])
        for text in out.values():
            self.assertIn("late by 35 min (target 15 min)", text)
        self.assertIn("the owner is notified", out["bureau"])


class AttentePorteurTest(_Base):
    """#2231 : quand le blocage n'est pas chez le porteur (attente de paquet,
    lot `blocked`), l'alerte vise le pilote/le composeur et ne prescrit ni
    relance ni réassignation du porteur. L'horloge GO → merge ne part qu'une
    fois tous les avis requis acquis sur la tête gelée."""

    def test_attente_paquet_vise_le_pilote(self):
        self.qa(verdict_s=50 * 60)
        alerts = self.slo()
        self.assertEqual([(a["step"], a["level"], a["targets"]) for a in alerts],
                         [("verdict_merge", 1, ["pilot"])])
        alert = alerts[0]
        self.assertEqual((alert["waiting"] or {}).get("what"), "merge")
        self.assertEqual(alert["orchestrators"], ["orch"])
        out = self.briefs(alert)
        self.assertEqual(sorted(out), ["orch"])
        self.assertIn("merge vehicle/package", out["orch"])
        self.assertIn("do not reassign or nudge its holder", out["orch"])
        self.assertNotIn("ameesh work assign", out["orch"])

    def test_attente_paquet_niveau_3_pousse_le_pilote(self):
        self.qa(verdict_s=100 * 60)
        alerts = self.slo()
        self.assertEqual([(a["level"], a["targets"]) for a in alerts],
                         [(3, ["pilot", "bureau", "auditeur"])])
        out = self.briefs(alerts[0])
        self.assertEqual(sorted(out), ["auditeur", "bureau", "orch"])
        for text in out.values():
            self.assertIn("merge vehicle/package", text)
            self.assertIn("push the pilot/composer", text)
            self.assertNotIn("reassign it now", text)

    def test_lot_bloque_vise_le_pilote(self):
        self.qa(verdict_s=50 * 60)
        work.move(self.db, self.lot["id"], "blocked",
                  note="attente d'un 2e avis hors DeepSeek/Claude (Codex 22/10)")
        self.now = time.time()
        alerts = self.slo()
        self.assertEqual([(a["step"], a["targets"]) for a in alerts],
                         [("verdict_merge", ["pilot"])])
        self.assertEqual((alerts[0]["waiting"] or {}).get("what"), "unblock")
        out = self.briefs(alerts[0])
        self.assertIn("`blocked`", out["orch"])
        self.assertIn("unblock", out["orch"])
        self.assertNotIn("reassign it now", out["orch"])

    def test_premier_avis_ne_fait_pas_le_go(self):
        self.qa(verdict_s=50 * 60)
        klass = {"class": "sensitive", "doubt": False, "frozen_sha": "a" * 40,
                 "repo": None}
        with mock.patch("ameesh.lot_class.of_lot", return_value=klass), \
                mock.patch("ameesh.merge_queue.review_gate",
                           return_value={"ok": False, "required": 2, "accepted": [1]}):
            self.assertEqual(self.slo(), [])
        with mock.patch("ameesh.lot_class.of_lot", return_value=klass), \
                mock.patch("ameesh.merge_queue.review_gate",
                           return_value={"ok": True, "required": 2,
                                         "accepted": [1, 2]}):
            alerts = self.slo()
        self.assertEqual([(a["step"], a["targets"]) for a in alerts],
                         [("verdict_merge", ["pilot"])])

    def test_classe_douteuse_ne_tait_pas_l_alerte(self):
        self.qa(verdict_s=50 * 60)
        klass = {"class": "sensitive", "doubt": True, "frozen_sha": "", "repo": None}
        with mock.patch("ameesh.lot_class.of_lot", return_value=klass):
            self.assertEqual(len(self.slo()), 1)


    def test_unblock_outside_verdict_merge_keeps_the_assignee_text(self):
        # The brief follows `_slo_targets`: outside `verdict_merge` the targets
        # stay the assignee's, so the text is not the pilot's.
        alert = {"lot": self.lot["id"], "level": 1, "step": "signal_assigned",
                 "waiting": {"what": "unblock"}, "agent": "dev1",
                 "value": 5400, "threshold": 3600}
        text = alert_briefs._slo_breach(alert)
        self.assertIn("assignee: act on it", text)
        self.assertNotIn("do not reassign its holder", text)


class AttenteDecisionTest(_Base):
    """#2378 : un lot `waiting_human` à demande de décision ouverte attend son
    décideur. Non échue, c'est une attente prévue (aucune alerte) ; échue,
    l'alerte vise le pilote et prescrit de vérifier l'échéance avec le
    demandeur, jamais de réassigner le porteur ni de relancer l'owner (canon
    §9). Sans demande, rien ne change."""

    def ask(self, due_in_s=2 * HEURE):
        view = decisions.ask(self.cfg, self.db, requester="dev1",
                             question="Quel critère pour la porte des avis ?",
                             options=["a=union", "b=par auteur"],
                             lot=str(self.lot["id"]), due_ts=time.time() + due_in_s)
        self.now = time.time()
        return view

    def expire(self, view, ago_s=10 * 60):
        """La demande est échue depuis `ago_s` (l'échéance passe en base)."""
        self.db.execute(
            "UPDATE agent_mailbox SET payload = jsonb_set(payload, '{decision,due_ts}',"
            " to_jsonb(%s::float8)) WHERE id = %s",
            (time.time() - ago_s, view["id"]))
        self.now = time.time()

    def test_demande_ouverte_non_echue_tait_l_alerte(self):
        self.qa(verdict_s=100 * 60)
        self.assertEqual([a["level"] for a in self.slo()], [3])
        view = self.ask()
        self.assertEqual(work.get(self.db, self.lot["id"])["state"], "waiting_human")
        self.assertEqual(self.slo(), [])
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db, now=self.now,
                                                          **_seuils(stale_lot_s=60))
                          if a["type"] == "stale_lot" and a.get("lot") == self.lot["id"]],
                         [])
        wait = stagnation_wait(self.db, self.lot, self.now)
        self.assertEqual((wait["what"], wait["decision"]), ("decision", view["id"]))
        self.assertIn("decision #%d" % view["id"], work_dependencies.wait_label(wait))

    def test_demande_echue_vise_le_pilote_sans_reassignation(self):
        self.qa(verdict_s=100 * 60)
        view = self.ask()
        self.expire(view)
        alerts = self.slo()
        self.assertEqual([(a["step"], a["level"], a["targets"]) for a in alerts],
                         [("verdict_merge", 3, ["pilot", "bureau", "auditeur"])])
        waiting = alerts[0]["waiting"]
        self.assertEqual((waiting["what"], waiting["decision"]), ("decision", view["id"]))
        out = self.briefs(alerts[0])
        self.assertEqual(sorted(out), ["auditeur", "bureau", "orch"])
        for text in out.values():
            self.assertIn("decision #%d" % view["id"], text)
            self.assertIn("check the deadline with the requester", text)
            self.assertNotIn("remind the decider", text)
            self.assertIn("deadline", text)
            self.assertNotIn("reassign it now", text)
            self.assertNotIn("ameesh work assign", text)

    def test_demande_echue_niveau_1_vise_le_pilote(self):
        self.qa(verdict_s=50 * 60)
        self.expire(self.ask())
        alerts = self.slo()
        self.assertEqual([(a["level"], a["targets"]) for a in alerts], [(1, ["pilot"])])
        out = self.briefs(alerts[0])
        self.assertEqual(sorted(out), ["orch"])
        self.assertIn("pilot: the lot is `waiting_human` on decision", out["orch"])
        self.assertIn("do not reassign its holder", out["orch"])

    def test_waiting_human_sans_demande_inchange(self):
        # ligne antérieure à L233 : `work.move` refuserait la transition
        self.qa(verdict_s=50 * 60)
        self.db.execute("UPDATE work_items SET state = 'waiting_human' WHERE id = %s",
                        (self.lot["id"],))
        self.now = time.time()
        alerts = self.slo()
        self.assertEqual([(a["level"], a["targets"]) for a in alerts], [(1, ["assignee"])])
        self.assertNotIn("decision", alerts[0]["waiting"])
        self.assertIn("assignee: act on it", self.briefs(alerts[0])["dev1"])


def stagnation_wait(db, lot, now):
    from ameesh import stagnation
    row = work.get(db, lot["id"])
    return stagnation.describe(db, [row], now=now)[int(lot["id"])]["planned_wait"]


class NotifyLivreTest(_Base):
    """B1 (#1035 R1) : la livraison RÉELLE par le Notifier — un P1 atteint le
    propriétaire même si son type est hors `notify.types`, un dépassement
    ordinaire reste filtrable, le courrier d'agent porte l'urgence du P1, et
    la clé de dédoublonnage change quand le niveau monte."""

    def setUp(self):
        super().setUp()
        self.desk = os.path.join(self.tmp, "desktop-789-%s.log" % self._testMethodName)
        self.logs: list = []
        self.now = time.time()

    def notifier(self, **raw):
        raw.setdefault("default", ["desktop"])
        raw.setdefault("default_human", "human:owner")
        return notify.Notifier(
            self.cfg, notify.parse_config(raw), None, log=self.logs.append,
            clock=lambda: self.now,
            sender=notify.Sender(env={
                "AMEESH_NOTIFY_SEND_BIN": FAKE_NOTIFY_SEND,
                "AMEESH_FAKE_NOTIFY_LOG": self.desk,
                "PATH": os.environ.get("PATH", "")}))

    def desktop(self):
        if not os.path.exists(self.desk):
            return []
        with open(self.desk, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def payload(self, recipient):
        rows = self.db.query("SELECT payload FROM agent_mailbox WHERE recipient = %s"
                             " ORDER BY id DESC LIMIT 1", (recipient,))
        self.assertTrue(rows, "aucun courrier pour %s" % recipient)
        return rows[0]["payload"] or {}

    def p1(self):
        self.db.execute("UPDATE work_items SET priority = 1 WHERE id = %s",
                        (self.lot["id"],))
        self.age(50 * 60)
        alerts = [a for a in self.slo() if a["critical"]]
        self.assertEqual(len(alerts), 1, alerts)
        return alerts

    def test_p1_livre_au_proprietaire_meme_type_hors_notify_types(self):
        alerts = self.p1()
        records = self.notifier(types=["long_turn"]).run_pass(self.db, current=alerts)
        raised = [r for r in records if r["event"] == "raised" and r["type"] == "slo_breach"]
        self.assertEqual([(r["human"], r["source"], r["delivery"]) for r in raised],
                         [("human:owner", "defaut", "envoyee")])
        argv = self.desktop()
        self.assertEqual(len(argv), 1, argv)
        self.assertIn("--urgency=critical", argv[0])
        self.assertIn("work item #%d" % self.lot["id"], argv[0][-1])
        self.assertIn("level 3", argv[0][-1])

    def test_non_p1_reste_filtrable_puis_livre_si_configure(self):
        self.age(1.5 * HEURE)
        alerts = self.slo()
        self.assertFalse(alerts[0]["critical"])
        records = self.notifier(types=["long_turn"]).run_pass(self.db, current=alerts)
        self.assertEqual([r for r in records if r["event"] == "raised"], [])
        self.assertEqual(self.desktop(), [])
        records = self.notifier(types=["long_turn", "slo_breach"]).run_pass(
            self.db, current=alerts)
        raised = [r for r in records if r["event"] == "raised"]
        self.assertEqual([(r["type"], r["human"], r["delivery"]) for r in raised],
                         [("slo_breach", "human:owner", "envoyee")])
        self.assertEqual(len(self.desktop()), 1)

    def test_courrier_agent_urgent_pour_un_p1_et_ordinaire_sinon(self):
        self.p1()
        self.notifier(types=["long_turn"]).run_pass(self.db, current=self.slo())
        self.assertTrue(self.payload("bureau").get("urgent"))
        self.db.execute("UPDATE agent_mailbox SET delivered_at = now()")
        self.db.execute("DELETE FROM agent_mailbox")
        self.db.execute("UPDATE work_items SET priority = 2 WHERE id = %s",
                        (self.lot["id"],))
        self.age(1.5 * HEURE)
        self.notifier(types=["long_turn"]).run_pass(self.db, current=self.slo())
        self.assertNotIn("urgent", self.payload("dev1"))

    def test_cle_de_dedoublonnage_change_avec_le_niveau(self):
        self.age(1.5 * HEURE)
        bas = self.slo()[0]
        self.age(2.5 * HEURE)
        haut = self.slo()[0]
        self.assertEqual((bas["reason"], haut["reason"]),
                         ("intake_started/level-1", "intake_started/level-2"))
        self.assertNotEqual(notify.key_of(bas), notify.key_of(haut))


class LotIllisibleTest(_Base):
    """#1107 (n2 P3 de #1035) : un lot illisible n'éteint plus les alertes.

    Un instant qui n'en est pas un fait lever `float()` dans le lecteur partagé
    (`stagnation.describe`) ; sans garde, ce seul lot faisait tomber **toute**
    la passe d'alertes. Le lot fautif est signalé (`lot_unreadable`), sans
    horodatage inventé ; les lots sains gardent leurs alertes. La ligne
    malformée est injectée à la lecture (`open_lots_activity`) : le calcul des
    jalons, de l'attente et du SLO reste le vrai."""

    def _illisible(self, malsain, **champs):
        vrai = Operations.open_lots_activity

        def lire(this, limit):
            return [dict(row, **champs) if int(row["id"]) == int(malsain["id"]) else row
                    for row in vrai(this, limit)]
        return mock.patch.object(Operations, "open_lots_activity", lire)

    def _lots(self):
        self.age(1.5 * HEURE)                           # lot sain, en retard
        # #540 : dev1 a déjà un lot non démarré, un second irait à la file
        # d'équipe (sans assigné) ; le lot illisible a donc son propre agent,
        # pour vérifier que le signal va bien à l'assigné.
        registry.upsert(self.db, "dev3", harness="claude", host=self.cfg.host)
        malsain = work.add(self.db, title="lot illisible", assignee="dev3")
        self.age(1.5 * HEURE, lot=malsain)
        return malsain

    def _alertes(self, malsain, **champs):
        with self._illisible(malsain, **champs):
            # 7 h plus tard : les deux lots sont aussi stagnants (seuil 6 h)
            return exploitation.alerts(self.cfg, self.db, now=self.now + 7 * HEURE,
                                       **_seuils(orchestrators_declared=["orch"]))

    def _verifie(self, alerts, malsain, valeur, slo):
        par_type = lambda t: [a["lot"] for a in alerts if a["type"] == t]
        self.assertEqual(par_type("slo_breach"), slo)
        self.assertEqual(par_type("stale_lot"), [self.lot["id"]])
        signaux = [a for a in alerts if a["type"] == "lot_unreadable"]
        self.assertEqual([a["lot"] for a in signaux], [malsain["id"]])  # un seul signal
        self.assertEqual(signaux[0]["agent"], "dev3")
        self.assertIsNone(signaux[0]["since"])           # aucun instant inventé
        self.assertIn("ValueError", signaux[0]["detail"])
        self.assertIn(valeur, signaux[0]["detail"])
        # un type d'alerte non enregistré ferait tomber le tri d'`alerts()`
        self.assertIn("lot_unreadable", exploitation.ALERT_TYPES)
        self.assertIn("lot_unreadable", notify.TYPE_LABELS)

    def test_created_ts_malforme(self):
        malsain = self._lots()
        alerts = self._alertes(malsain, created_ts="pas-un-instant")
        self._verifie(alerts, malsain, "pas-un-instant", [self.lot["id"]])

    def test_last_activity_ts_malforme(self):
        malsain = self._lots()
        alerts = self._alertes(malsain, last_activity_ts="jamais")
        # le SLO ne lit pas `last_activity_ts` : le lot garde son échéance
        self._verifie(alerts, malsain, "jamais", [self.lot["id"], malsain["id"]])


if __name__ == "__main__":
    unittest.main()
