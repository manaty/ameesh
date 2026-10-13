# SPDX-License-Identifier: AGPL-3.0-only
"""#789 tranche 2 : l'alerte `slo_breach` (canon §9).

`exploitation.alerts()` mesure les étapes du §9 sur les lots ouverts et lève
une alerte par `(lot, étape, niveau)` ; `alert_briefs` l'adresse au niveau
atteint — 1× l'assigné, 2× le pilote, 3× le bureau et l'auditeur, l'owner en
plus pour un P1. Un lot en attente planifiée (dépendance ouverte) n'est pas en
retard : il est tu.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from ameesh import (alert_briefs, exploitation, notify, progress, registry, work,
                    work_dependencies)

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


class BriefsTest(_Base):
    def test_niveau_1_assigne_seul(self):
        self.age(1.5 * HEURE)
        out = self.briefs(self.slo()[0])
        self.assertEqual(sorted(out), ["dev1"])
        self.assertIn("SLO breach, level 1", out["dev1"])
        self.assertIn("ameesh work note %d" % self.lot["id"], out["dev1"])

    def test_niveau_2_pilote_seul(self):
        self.age(2.5 * HEURE)
        out = self.briefs(self.slo()[0])
        self.assertEqual(sorted(out), ["orch"])
        self.assertIn("pilot: reassign", out["orch"])

    def test_niveau_3_bureau_et_auditeur(self):
        self.db.execute("UPDATE work_items SET priority = 1 WHERE id = %s",
                        (self.lot["id"],))
        self.age(50 * 60)
        out = self.briefs(self.slo()[0])
        self.assertEqual(sorted(out), ["auditeur", "bureau"])
        self.assertIn("bureau or auditor", out["bureau"])
        self.assertIn("the owner is notified", out["bureau"])


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
    """#1107 (n2 P3 de #1035) : la passe SLO isole un lot illisible.

    Un instant qui n'en est pas un fait lever la conversion `float()` du
    moteur ; sans garde, ce seul lot faisait tomber **toute** la passe
    d'alertes. Le lot fautif est signalé (`slo_unreadable`), les lots sains
    gardent leurs alertes."""

    def _lire(self, malsain):
        vrai = progress._work_item_milestones

        def lire(item, events, actions, declared=None):
            if int(item["id"]) == int(malsain["id"]):
                raise ValueError("could not convert string to float: 'pas-un-instant'")
            return vrai(item, events, actions, declared)
        return lire

    def test_lot_illisible_signale_les_autres_gardent_leurs_alertes(self):
        self.age(1.5 * HEURE)                           # lot sain, en retard
        sain = work.add(self.db, title="un autre lot SLO", assignee="dev1")
        self.age(1.5 * HEURE, lot=sain)
        malsain = work.add(self.db, title="lot illisible", assignee="dev1")
        self.age(1.5 * HEURE, lot=malsain)

        with mock.patch.object(progress, "_work_item_milestones",
                               side_effect=self._lire(malsain)):
            alerts = exploitation.alerts(self.cfg, self.db, now=self.now,
                                         **_seuils(orchestrators_declared=["orch"]))

        sains = {a["lot"] for a in alerts if a["type"] == "slo_breach"}
        signaux = [a for a in alerts if a["type"] == "slo_unreadable"]
        self.assertEqual(sains, {self.lot["id"], sain["id"]})
        self.assertEqual([a["lot"] for a in signaux], [malsain["id"]])
        self.assertEqual(signaux[0]["agent"], "dev1")
        self.assertIn("ValueError", signaux[0]["detail"])
        self.assertIn("pas-un-instant", signaux[0]["detail"])
        # un type d'alerte non enregistré ferait tomber le tri d'`alerts()`
        self.assertIn("slo_unreadable", exploitation.ALERT_TYPES)
        self.assertIn("slo_unreadable", notify.TYPE_LABELS)


if __name__ == "__main__":
    unittest.main()
