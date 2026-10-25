# SPDX-License-Identifier: AGPL-3.0-only
"""#3447 : un lot réservé à un exécutant externe déclaré (bureau, owner) n'est
pas pris par L165 ; l'alerte et le débit le nomment au lieu de « sans
assigné » ; sa part agent (sous-lot de préparation) reste assignable."""
from __future__ import annotations

import re
import threading
import time
import unittest
from unittest import mock

from ameesh import (alert_briefs, debit, exploitation, stagnation,
                    sous_utilisation, storage, work)

from .test_l119_backlog import HEURE, _Base, _seuils


class PriseExterneTest(_Base):
    def external_agent(self, name="bureau", responsible="human:proprio"):
        """Un agent `externe` (0030) : joignable pour un déclaratif, jamais
        réveillé par un exécuteur."""
        self.agent(name, harness="codex", responsible=responsible)
        self.db.execute("UPDATE agent_registry SET mode = 'externe' WHERE name = %s",
                        (name,))

    def reserve(self, title="geste root", *, app="nexlink", executor="bureau", **kw):
        item = work.add(self.db, title=title, app=app, external_executor=executor,
                        actor="human:proprio", cfg=self.cfg, **kw)
        # L165 laisse un lot tout juste créé à qui l'a créé (5 min) : le
        # témoin porte sur un lot installé.
        self.db.execute("UPDATE work_items SET created_at = created_at"
                        " - make_interval(secs => %s) WHERE id = %s",
                        (HEURE, item["id"]))
        return work.get(self.db, item["id"])

    def test_deux_passages_l165_ne_prennent_pas_le_lot_reserve(self):
        self.external_agent()
        self.agent("claude1", team="nexlink")
        lot = self.reserve()
        self.assertEqual(lot["external_executor"], "bureau")
        self.assertIsNone(lot["assignee"])
        # Le filtre précède le LIMIT de la file : le lot n'est pas offert.
        self.assertNotIn(lot["id"],
                         [i["id"] for i in storage.of(self.db).work.unassigned_lots(10)])
        self.assertEqual(self.take(), [])
        self.agent("claude2", team="nexlink")
        self.assertEqual(self.take(), [])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

        # Une attribution directe est refusée (deux exécutants) ; le geste est
        # la levée de la réservation.
        with self.assertRaises(work.WorkError) as raised:
            work.assign(self.db, lot["id"], "claude1")
        self.assertIn("reserved to bureau", str(raised.exception))
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

        # Levée : le lot retourne à la file ordinaire et L165 le prend.
        cleared = work.set_external_executor(self.db, lot["id"], None,
                                             actor="human:proprio", cfg=self.cfg,
                                             clear=True)
        self.assertIsNone(cleared["item"]["external_executor"])
        self.assertEqual([t["item"]["id"] for t in self.take()], [lot["id"]])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "claude1")

    def test_un_autre_lot_de_l_equipe_est_pris_normalement(self):
        self.external_agent()
        self.agent("claude1", team="nexlink")
        reserve = self.reserve()
        ordinaire = work.add(self.db, title="travail ordinaire", app="nexlink")
        self.db.execute("UPDATE work_items SET created_at = created_at"
                        " - make_interval(secs => %s) WHERE id = %s",
                        (HEURE, ordinaire["id"]))
        self.assertEqual([t["item"]["id"] for t in self.take()], [ordinaire["id"]])
        self.assertIsNone(work.get(self.db, reserve["id"])["assignee"])

    def test_sous_lot_de_preparation_reste_assignable(self):
        self.external_agent()
        self.agent("claude1", team="nexlink")
        parent = self.reserve()
        child = work.add(self.db, title="préparation du script", app="nexlink",
                         parent_lot=parent["id"], actor="human:proprio",
                         cfg=self.cfg)
        child = work.get(self.db, child["id"])
        # Le champ ne se propage pas : la part d'un agent n'est pas réservée.
        self.assertIsNone(child.get("external_executor"))
        out = work.assign(self.db, child["id"], "claude1")
        self.assertEqual(out["item"]["assignee"], "claude1")
        self.assertIsNone(work.get(self.db, parent["id"])["assignee"])

    def test_le_declaratif_est_un_geste_de_pilote_et_cible_un_externe(self):
        self.external_agent()
        self.agent("claude1", team="nexlink")
        lot = work.add(self.db, title="lot", app="nexlink")
        # Un agent ordinaire ne se réserve pas : il s'attribue.
        with self.assertRaises(work.WorkError) as raised:
            work.set_external_executor(self.db, lot["id"], "claude1",
                                       actor="human:proprio", cfg=self.cfg)
        self.assertIn("not an external one", str(raised.exception))
        with self.assertRaises(work.WorkError) as raised:
            work.set_external_executor(self.db, lot["id"], "bureau",
                                       actor="claude1", cfg=self.cfg)
        self.assertIn("external executor refused", str(raised.exception))

    def test_l_alerte_et_le_debit_nomment_l_executant(self):
        self.external_agent()
        self.agent("claude1", team="nexlink")
        lot = self.reserve()
        # L'attente (stale_lot, work list, work show) nomme l'exécutant.
        detail = stagnation.waiting({"state": "intake", "external_executor": "bureau",
                                     "assignee": None}, {}, [])
        self.assertEqual(detail["who"], "bureau")
        self.assertIn("start by bureau", detail["label"])
        # L'alerte SLO aussi, au lieu de « unassigned ».
        text = alert_briefs._slo_breach(
            {"lot": lot["id"], "title": lot["title"], "lot_state": "intake",
             "step": "intake_started", "step_label": "start", "level": 1,
             "value": 7200, "threshold": 3600, "agent": None,
             "external_executor": "bureau", "waiting": {}})
        self.assertIn("reserved to bureau (external executor)", text)
        self.assertNotIn("unassigned", text)
        # Le geste du brief n'est pas « reassign » : la réservation se lève
        # d'abord, ou l'exécutant agit.
        pilot = alert_briefs._slo_breach(
            {"lot": lot["id"], "title": lot["title"], "lot_state": "intake",
             "step": "intake_started", "step_label": "start", "level": 2,
             "value": 7200, "threshold": 3600, "agent": None,
             "external_executor": "bureau", "waiting": {}}, level=2)
        self.assertIn("do not assign", pilot)
        self.assertIn("external-executor %d --clear" % lot["id"], pilot)
        # Le débit le classe en attente externe, pas en attente d'agent.
        report = debit.build(
            items=[{"id": lot["id"], "title": lot["title"], "app": lot["app"],
                    "assignee": None, "priority": 2,
                    "external_executor": "bureau",
                    "created_ts": time.time() - HEURE}],
            milestones=[], events=[{"work_item_id": lot["id"], "id": 1,
                                    "state": "intake", "at_ts": time.time() - HEURE,
                                    "note": "created"}],
            now=time.time(), installed="1.21.0")
        self.assertEqual([row["lot"] for row in report["external_waits"]], [lot["id"]])
        self.assertEqual(report["external_waits"][0]["who"], "bureau")
        self.assertFalse([row for row in report["slow_waits"]
                          if int(row.get("lot") or 0) == lot["id"]])
        self.assertIn("Attentes externes", debit.format_text(report))

    def test_cli_add_reserve_et_clear(self):
        self.external_agent()
        proc = self.mesh("work", "add", "--title", "geste root", "--app", "agence-x",
                         "--new-project", "--external-executor", "bureau",
                         "--actor", "human:proprio")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("executor : bureau (external, reserved", proc.stdout)
        lot_id = int(re.search(r"#(\d+)", proc.stdout).group(1))
        item = work.get(self.db, lot_id)
        self.assertEqual(item["external_executor"], "bureau")
        self.assertIsNone(item["assignee"])
        proc = self.mesh("work", "external-executor", str(lot_id), "--clear",
                         "--actor", "human:proprio")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("external executor cleared", proc.stdout)
        self.assertIsNone(work.get(self.db, lot_id)["external_executor"])

    def test_l_alerte_stale_lot_nomme_l_executant(self):
        self.external_agent()
        lot = self.reserve()
        # La ligne de stagnation porte la réservation, et l'attente la nomme.
        rows = stagnation.stagnant_lots(self.db, threshold_s=0, now=time.time())
        row = next(r for r in rows if r["id"] == lot["id"])
        self.assertEqual(row["external_executor"], "bureau")
        self.assertIn("bureau", row["waiting"]["label"])
        # Le brief `stale_lot` ne dit plus « unassigned » et ne propose pas
        # d'assignation : il nomme l'exécutant et la levée par le pilote.
        alert = {"type": "stale_lot", "agent": None, "lot": lot["id"],
                 "title": lot["title"], "lot_state": lot["state"],
                 "value": 7 * HEURE, "waiting_for": row["waiting_for"],
                 "waiting": row["waiting"],
                 "external_executor": row["external_executor"]}
        text = alert_briefs._stale_lot(alert)
        self.assertIn("reserved to bureau (external executor)", text)
        self.assertNotIn("unassigned", text)
        self.assertIn("ameesh work external-executor %d --clear" % lot["id"], text)
        self.assertNotIn("--claim", text)
        # Le détail de l'alerte (`exploitation`) porte la même action.
        action = exploitation.stale_action(
            {"id": lot["id"], "external_executor": "bureau"})
        self.assertIn("external executor bureau acts", action)
        self.assertNotIn("assign it", action)

    def test_delegation_refusee_sur_lot_reserve(self):
        self.external_agent()
        self.agent("claude1", team="nexlink")
        lot = self.reserve()
        with self.assertRaises(work.WorkError) as raised:
            work.delegate(self.db, lot["id"], "claude1", within="30m",
                          actor="human:proprio", cfg=self.cfg)
        self.assertIn("reserved to bureau", str(raised.exception))
        after = work.get(self.db, lot["id"])
        self.assertIsNone(after["assignee"])
        self.assertEqual(after["external_executor"], "bureau")
        self.assertIsNone(storage.of(self.db).work.current_delegation(lot["id"]))
        # La levée rend la délégation possible : le refus tient à la
        # réservation, pas à la délégation elle-même.
        work.set_external_executor(self.db, lot["id"], None, actor="human:proprio",
                                   cfg=self.cfg, clear=True)
        out = work.delegate(self.db, lot["id"], "claude1", within="30m",
                            actor="human:proprio", cfg=self.cfg)
        self.assertEqual(out["item"]["assignee"], "claude1")
        self.assertIsNone(out["item"]["external_executor"])

    def test_course_reservation_delegation_un_seul_gagnant(self):
        """La course ``external_executor`` / ``assignee`` se tranche sous le
        verrou : les deux gestes lisent le lot libre, un seul écrit."""
        self.external_agent()
        self.agent("claude1", team="nexlink")
        lot = work.add(self.db, title="course réservation", app="nexlink")
        db1, db2 = self.connect(), self.connect()
        lus = threading.Barrier(2, timeout=10)
        vrai_get = work.get
        lectures: list = []
        results: list = [None, None]

        def get_puis_attendre(db, item_id):
            row = vrai_get(db, item_id)
            lectures.append(item_id)
            if len(lectures) <= 2:
                lus.wait(timeout=10)   # les deux lectures voient le lot libre
            return row

        def reserver():
            try:
                results[0] = ("reserve", work.set_external_executor(
                    db1, lot["id"], "bureau", actor="human:proprio", cfg=self.cfg))
            except work.WorkError as exc:
                results[0] = ("reserve-refusee", exc)

        def deleguer():
            try:
                results[1] = ("delegue", work.delegate(
                    db2, lot["id"], "claude1", within="30m",
                    actor="human:proprio", cfg=self.cfg))
            except work.WorkError as exc:
                results[1] = ("delegation-refusee", exc)

        try:
            with mock.patch.object(work, "get", side_effect=get_puis_attendre):
                threads = [threading.Thread(target=reserver),
                           threading.Thread(target=deleguer)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
                self.assertFalse(any(thread.is_alive() for thread in threads))
        finally:
            db1.close()
            db2.close()
        self.assertGreaterEqual(len(lectures), 2)
        gagnants = [r for r in results if r and r[0] in ("reserve", "delegue")]
        perdants = [r for r in results if r and r[0] not in ("reserve", "delegue")]
        self.assertEqual(len(gagnants), 1, results)
        self.assertEqual(len(perdants), 1, results)
        item = work.get(self.db, lot["id"])
        if gagnants[0][0] == "reserve":
            self.assertEqual(item["external_executor"], "bureau")
            self.assertIsNone(item["assignee"])
            self.assertIn("reserved", str(perdants[0][1]))
        else:
            self.assertIsNone(item["external_executor"])
            self.assertEqual(item["assignee"], "claude1")
            self.assertIn("assigned", str(perdants[0][1]))

    def test_idle_capacity_nomme_le_lot_reserve(self):
        self.external_agent()
        self.agent("claude1", team="nexlink")
        lot = self.reserve()
        alerts = exploitation.alerts(self.cfg, self.db,
                                     **dict(_seuils(), idle_capacity_s=1))
        idle = [a for a in alerts if a.get("type") == "idle_capacity"]
        self.assertTrue(idle, alerts)
        row = idle[0]
        self.assertEqual(row.get("external_lots"),
                         [{"lot": lot["id"], "executor": "bureau"}])
        self.assertIn("reserved to an external executor", row.get("detail"))
        self.assertIn("bureau", row.get("detail"))
        self.assertNotIn(lot["id"], row.get("startable_lots") or [])
        brief = sous_utilisation.orchestrator_briefs(
            row, [dict(name="orchestrateur", team="nexlink")])
        if brief:
            self.assertIn("Reserved to an external executor", next(iter(brief.values())))
