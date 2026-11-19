# SPDX-License-Identifier: AGPL-3.0-only
"""#7759 : « review GO → merge » ne relance plus un candidat prêt qui attend
son créneau de paquet.

Constat (17/11) : 82 alertes de niveau 1 sur des lots validés qui
n'attendaient que le paquet suivant (#7630 ×7, #7671 ×5…), 38 tours pour
8,96 $. Attendu : quand `candidate_refusal` rend « ready », l'horloge part du
prochain créneau, et un créneau passé sans eux fait une seule alerte groupée
au composeur/pilote, pas une par lot.

Témoin : lot GO « ready », premier créneau passé, le suivant dans 20 min
(40 min depuis le créneau, au-delà de la cible de repli de 30 min) → aucun
niveau 1.
"""
from __future__ import annotations

from unittest import mock

from ameesh import exploitation, packet_candidates, packet_composition, work

from .test_4572_slo_candidate_cause import _SloBase, _seuils
from .test_5860_slo_creneau_manque import HEURE, _Cadence

READY = {"code": "", "detail": "", "owner": "", "gesture": "",
         "ready": True, "unknown": False}
AUTEUR = {"code": "conflict", "owner": "author", "detail": "conflict with main: f",
          "gesture": "resolve the conflict with the target, then freeze",
          "ready": False, "unknown": False}


class PretAuCreneauTest(_SloBase):
    def setUp(self):
        super().setUp()
        self.first = (int(self.GO // HEURE) + 1) * HEURE
        patch = mock.patch.object(exploitation, "_packet_calendar",
                                  return_value=_Cadence())
        patch.start()
        self.addCleanup(patch.stop)
        self.other = self.go_lot("un second lot gelé", "lot/7759-y")

    def go_lot(self, title, branch):
        lot = work.add(self.db, title=title, assignee="dev1", app="ameesh",
                       branch=branch, branch_target="main", actor="dev1")
        work.move(self.db, lot["id"], "build")
        work.move(self.db, lot["id"], "qa")
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor)"
            " VALUES (%s, 'frozen', to_timestamp(%s), 'dev1')",
            (lot["id"], self.GO - 1))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor, verdict)"
            " VALUES (%s, 'verdict', to_timestamp(%s), 'rel', 'ok')",
            (lot["id"], self.GO))
        return lot

    def all_alerts(self, at, cause=READY):
        with mock.patch.object(packet_candidates, "candidate_refusal",
                               return_value=dict(cause)):
            return exploitation.alerts(self.cfg, self.db, now=at,
                                       **_seuils(orchestrators_declared=["orch"]))

    @staticmethod
    def of(alerts, kind):
        return [a for a in alerts if a["type"] == kind
                and (kind != "slo_breach" or a.get("step") == "verdict_merge")]

    def test_temoin_creneau_dans_20_min(self):
        # 40 min après le premier créneau : la cible de repli (30 min) depuis
        # ce créneau est dépassée (niveau 1 avant #7759) ; le lot prêt attend
        # le suivant, aucune alerte n'est due.
        alerts = self.all_alerts(self.first + 40 * 60)
        self.assertEqual(self.of(alerts, "slo_breach"), [])
        self.assertEqual(self.of(alerts, "packet_ready_waiting"), [])

    def test_creneau_manque_une_seule_alerte_groupee(self):
        alerts = self.all_alerts(self.first + HEURE + 50 * 60)
        self.assertEqual(self.of(alerts, "slo_breach"), [])
        grouped = self.of(alerts, "packet_ready_waiting")
        self.assertEqual(len(grouped), 1, grouped)
        alert = grouped[0]
        self.assertEqual(alert["project"], "ameesh")
        self.assertEqual(sorted(entry["lot"] for entry in alert["lots"]),
                         sorted([self.lot["id"], self.other["id"]]))
        self.assertEqual(alert["next_slot"], self.first + 2 * HEURE)
        self.assertIn("do not nudge or reassign", alert["detail"])
        self.assertIn("#%d (1 slot(s) passed" % self.lot["id"], alert["detail"])
        # au seul pilote, ni à l'assigné ni à l'auditeur
        briefs = self.briefs(alert)
        self.assertEqual(set(briefs), {"orch"})
        # la clé est le projet : un lot qui sort du groupe ne refait rien
        later = dict(alert, lots=alert["lots"][:1], since=alert["since"] + 1)
        self.assertEqual(exploitation.alert_key(alert), exploitation.alert_key(later))

    def test_paquet_en_vol_tait_l_alerte_groupee(self):
        work.add(self.db, title="porteur", body="unités : #999999 @ cafebabe",
                 branch="integration/paquet-7759", app="ameesh", actor="test")
        alerts = self.all_alerts(self.first + HEURE + 50 * 60)
        self.assertEqual(self.of(alerts, "slo_breach"), [])
        self.assertEqual(self.of(alerts, "packet_ready_waiting"), [])

    def test_bail_de_composition_tait_l_alerte_groupee(self):
        with mock.patch.object(packet_composition, "holder",
                               return_value={"holder": "orch"}):
            alerts = self.all_alerts(self.first + HEURE + 50 * 60)
        self.assertEqual(self.of(alerts, "packet_ready_waiting"), [])

    def test_cause_auteur_inchangee(self):
        # au même instant que le témoin : la cause côté auteur garde sa ligne
        alerts = self.all_alerts(self.first + 40 * 60, cause=AUTEUR)
        lines = self.of(alerts, "slo_breach")
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(all(a["targets"] == ["assignee"] for a in lines))
        self.assertEqual(self.of(alerts, "packet_ready_waiting"), [])

    def test_sans_cadence_rien_ne_change(self):
        # sans créneau connu, il n'y a pas de « prochain créneau » : la ligne
        # par lot reste (#4572, le composeur le porte).
        with mock.patch.object(exploitation, "_packet_calendar", return_value=None):
            alerts = self.all_alerts(self.GO + 1801.0)
        self.assertEqual(len(self.of(alerts, "slo_breach")), 2)
        self.assertEqual(self.of(alerts, "packet_ready_waiting"), [])
