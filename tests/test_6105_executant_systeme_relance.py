# SPDX-License-Identifier: AGPL-3.0-only
"""#6105 (panier de #6091) : le responsable humain déclaré d'un exécutant
non-agent (`system:…`) est nommé par le débit et les briefs — la relance d'une
attente réservée lui revient —, et une réservation dont le système a été
retiré est signalée au pilote au lieu d'attendre en silence."""
from __future__ import annotations

import time

from ameesh import alert_briefs, debit, exploitation, work

from .test_l119_backlog import HEURE, _Base, _seuils


class ExecutantSystemeRelanceTest(_Base):
    def declare(self, name="system:ima", responsible="human:proprio", **kw):
        kw.setdefault("actor", "human:proprio")
        return work.declare_external_executor(
            self.db, name, responsible, cfg=self.cfg, **kw)

    def reserve(self, title="geste ima", executor="system:ima", **kw):
        item = work.add(self.db, title=title, app="nexlink",
                        external_executor=executor, actor="human:proprio",
                        cfg=self.cfg, **kw)
        # L165 laisse un lot tout juste créé à qui l'a créé (5 min) : les
        # témoins portent sur un lot installé.
        self.db.execute("UPDATE work_items SET created_at = created_at"
                        " - make_interval(secs => %s) WHERE id = %s",
                        (HEURE, item["id"]))
        return work.get(self.db, item["id"])

    def external_wait(self, lot, *, wait_s=600):
        report = debit.read(self.db, cfg=self.cfg, now=time.time(),
                            wait_s=wait_s, jobs=False, canons=[])
        return report, next(row for row in report["external_waits"]
                            if row["lot"] == lot["id"])

    def stale_alert(self, lot):
        seuils = _seuils()
        seuils["stale_lot_s"] = 0
        alerts = exploitation.alerts(self.cfg, self.db, **seuils)
        return next(alert for alert in alerts
                    if alert.get("type") == "stale_lot"
                    and alert.get("lot") == lot["id"])

    # -- N1 : le responsable déclaré est nommé et relancé --------------------
    def test_le_debit_nomme_et_relance_le_responsable_declare(self):
        self.declare()
        lot = self.reserve()
        report, row = self.external_wait(lot)
        self.assertEqual(row["who"], "system:ima")
        self.assertEqual(row["responsible"], "human:proprio")
        self.assertFalse(row["retired"])
        self.assertIn("non-agent system", row["gesture"])
        self.assertIn("human:proprio", row["gesture"])
        # #6105 : la relance existe et s'adresse au responsable humain.
        self.assertTrue(row["reminder_due"])
        text = debit.format_text(report)
        self.assertIn("human:proprio", text)
        self.assertIn("relance due", text)
        self.assertIn("responsable déclaré", text)

    def test_les_briefs_nomment_le_responsable_declare(self):
        self.declare()
        lot = self.reserve()
        alert = {"type": "stale_lot", "agent": None, "lot": lot["id"],
                 "title": lot["title"], "lot_state": lot["state"],
                 "value": 7 * HEURE, "waiting_for": "start",
                 "waiting": {"what": "start", "who": "system:ima"},
                 "external_executor": "system:ima",
                 "external_responsible": "human:proprio",
                 "external_retired": False}
        brief = alert_briefs._stale_lot(alert)
        self.assertIn("declared responsible human:proprio", brief)
        self.assertIn("human:proprio acts", brief)
        self.assertIn("not an agent", brief)
        slo = alert_briefs._slo_breach(
            dict(alert, type="slo_breach", step="intake_started",
                 step_label="start", level=1, threshold=HEURE))
        self.assertIn("declared responsible human:proprio", slo)
        self.assertIn("human:proprio (declared responsible for the non-agent "
                      "system system:ima)", slo)
        # L'alerte SLO réelle porte la déclaration, pas seulement le brief.
        stale = self.stale_alert(lot)
        self.assertEqual(stale.get("external_responsible"), "human:proprio")
        self.assertFalse(stale.get("external_retired"))
        self.assertIn("responsible human:proprio", alert_briefs._stale_lot(stale))
        self.assertIn("human:proprio", stale.get("action") or "")

    # -- N2 : une réservation dont le système est retiré revient au pilote ---
    def test_un_systeme_retire_est_signale_au_pilote(self):
        self.declare()
        lot = self.reserve()
        retired = work.retire_external_executor(
            self.db, "system:ima", actor="human:proprio", cfg=self.cfg)
        # Le geste de retrait nomme tout de suite les lots encore réservés.
        self.assertEqual(retired["open_lots"], [lot["id"]])
        report, row = self.external_wait(lot)
        self.assertTrue(row["retired"])
        self.assertEqual(row["retired_by"], "human:proprio")
        self.assertIn("RETIRED non-agent system", row["gesture"])
        self.assertIn("pilot clears the reservation", row["gesture"])
        self.assertIn("ameesh work external-executor %d --clear" % lot["id"],
                      row["gesture"])
        self.assertIn("système retiré", debit.format_text(report))
        # Les briefs `stale_lot` et `slo_breach` ne laissent pas le lot
        # attendre un système qui n'existe plus : le pilote agit.
        stale = self.stale_alert(lot)
        self.assertTrue(stale.get("external_retired"))
        self.assertEqual(stale.get("external_retired_by"), "human:proprio")
        brief = alert_briefs._stale_lot(stale)
        self.assertIn("retired non-agent system", brief)
        self.assertIn("pilot clears the reservation", brief)
        self.assertIn("ameesh work external-executor %d --clear" % lot["id"], brief)
        self.assertIn("retired non-agent system", stale.get("action") or "")
        slo = alert_briefs._slo_breach(dict(
            stale, type="slo_breach", step="intake_started", step_label="start",
            level=1, value=2 * HEURE, threshold=HEURE))
        self.assertIn("retired non-agent system", slo)
        self.assertIn("pilot: ", slo)

    def test_l_alerte_slo_porte_le_responsable_puis_le_retrait(self):
        self.declare()
        lot = self.reserve()
        # Le lot est « prêt, pas démarré » depuis 2 h : l'étape intake court.
        self.db.execute(
            "UPDATE work_items SET created_at = now() - make_interval(secs => %s)"
            " WHERE id = %s", (2 * HEURE, lot["id"]))
        seuils = _seuils()
        now = time.time()
        alerts = exploitation.alerts(self.cfg, self.db, now=now, **seuils)
        slo = next(alert for alert in alerts
                   if alert.get("type") == "slo_breach"
                   and alert.get("lot") == lot["id"])
        self.assertEqual(slo.get("external_responsible"), "human:proprio")
        self.assertFalse(slo.get("external_retired"))
        # Retiré, le même lot ne nomme plus de responsable : le pilote agit.
        work.retire_external_executor(
            self.db, "system:ima", actor="human:proprio", cfg=self.cfg)
        alerts = exploitation.alerts(self.cfg, self.db, now=now + 60, **seuils)
        slo = next(alert for alert in alerts
                   if alert.get("type") == "slo_breach"
                   and alert.get("lot") == lot["id"])
        self.assertTrue(slo.get("external_retired"))
        self.assertEqual(slo.get("external_retired_by"), "human:proprio")

    def test_le_retrait_nomme_les_lots_et_work_show_dit_retire(self):
        self.declare()
        lot = self.reserve()
        proc = self.mesh("work", "external-executor", "--retire", "system:ima",
                         "--actor", "human:proprio")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("1 open lot(s) still reserved", proc.stdout)
        self.assertIn("#%d" % lot["id"], proc.stdout)
        self.assertIn("the debit signals them as retired", proc.stdout)
        proc = self.mesh("work", "show", str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("retired non-agent system", proc.stdout)
        self.assertIn("the pilot clears or reassigns", proc.stdout)
