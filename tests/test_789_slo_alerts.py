# SPDX-License-Identifier: AGPL-3.0-only
"""#789 tranche 2 : l'alerte `slo_breach` (canon §9).

`exploitation.alerts()` mesure les étapes du §9 sur les lots ouverts et lève
une alerte par `(lot, étape, niveau)` ; `alert_briefs` l'adresse au niveau
atteint — 1× l'assigné, 2× le pilote, 3× le bureau et l'auditeur, l'owner en
plus pour un P1. Un lot en attente planifiée (dépendance ouverte) n'est pas en
retard : il est tu.
"""
from __future__ import annotations

import time
import unittest

from ameesh import alert_briefs, exploitation, notify, registry, work, work_dependencies

from .support import PgTestCase

HEURE = 3600.0


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


if __name__ == "__main__":
    unittest.main()
