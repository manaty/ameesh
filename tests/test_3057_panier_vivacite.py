# SPDX-License-Identifier: AGPL-3.0-only
"""#3057 : panier de #3036 (vivacité des alertes), défauts non bloquants.

Témoins :

* un lot assigné au bureau `externe` part au pilote dès le niveau 1, et le
  brief nomme le bureau parmi les absents (route voulue, documentée) ;
* un bail rendu depuis moins de `LEASE_GRACE_S` (redémarrage d'exécuteur)
  n'escalade pas et n'écrit pas au fil : la remise attend le retour ; la
  grâce passée, la perte est traitée comme avant ;
* une alerte dont les destinataires morts le restent ne recalcule pas ses
  briefs à chaque passe ; elle le fait dès que l'un revient ;
* à la relance de 6 h, un destinataire revenu reçoit le brief initial et pas,
  à la passe suivante, la relance ;
* un `pending` qui n'est plus destinataire est oublié.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import alert_briefs, notify

from .support import PgTestCase
from .test_3036_alert_liveness import LivenessFixture, _row, _slo


class RoutePureTest(unittest.TestCase):

    def test_niveau_1_bureau_externe_assigne_part_au_pilote_et_est_nomme(self):
        listing = [_row("bureau", mode="externe"), _row("orch"), _row("auditeur")]
        briefs = alert_briefs.briefs(
            _slo(level=1, targets=("assignee",), agent="bureau", orchestrators=()),
            listing, auditor_names=["auditeur"], orchestrator_names=("orch",))
        self.assertEqual(list(briefs), ["orch"])
        self.assertIn("bureau (external desk, reads the lot thread). "
                      "Escalated from level 1 to level 2", briefs["orch"])

    def test_grace_du_bail_rendu(self):
        listing = [_row("orch", lease=False), _row("stop", lease=False, status="stopped"),
                   _row("loin", lease=False, host="lost"), _row("vif"),
                   {"name": "hors-base"}]
        since = {"parti": 1.0}
        out, held = alert_briefs.lease_grace(listing, {"lost"}, since, 1000.0)
        # seul le bail rendu a une grâce ; un arrêt ou un hôte perdu est prouvé
        self.assertEqual(held, {"orch"})
        self.assertEqual(since, {"orch": 1000.0})       # « parti » a quitté le registre
        self.assertTrue({r["name"]: r for r in out}["orch"]["lease_live"])
        out, held = alert_briefs.lease_grace(
            listing, {"lost"}, since, 1000.0 + alert_briefs.LEASE_GRACE_S)
        self.assertEqual(held, set())
        self.assertFalse({r["name"]: r for r in out}["orch"]["lease_live"])
        # retour du bail : la date d'absence est oubliée
        alert_briefs.lease_grace([_row("orch")], set(), since, 2000.0)
        self.assertEqual(since, {})


class PanierNotifierTest(LivenessFixture, PgTestCase):

    def idle(self):
        return {"type": "idle_capacity", "agents": ["dev1"], "lots": [],
                "orchestrators": ["orch"], "threshold": 1800.0,
                "detail": "1 agent au repos. Action : répartir", "project": "ameesh"}

    def test_redemarrage_dans_la_grace_ni_escalade_ni_fil(self):
        self.dead("orch", "bureau", "auditeur")
        self.release("dev1")                 # l'exécuteur redémarre
        notifier = self.notifier()
        alert = _slo(level=1, targets=("assignee",))
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(self.thread("ameesh", 42), [])
        self.assertEqual(self.mails("dev1"), [])
        # retour quelques secondes plus tard : une seule remise, à l'assigné
        self.lease("dev1")
        self.now += 30.0
        notifier.run_pass(self.db, current=[alert])
        [mail] = self.mails("dev1")
        self.assertIn("SLO breach, level 1", mail["body"])
        self.assertEqual(self.thread("ameesh", 42), [])

    def test_grace_passee_la_perte_est_traitee(self):
        self.dead("orch", "bureau", "auditeur")
        self.release("dev1")
        notifier = self.notifier()
        alert = _slo(level=1, targets=("assignee",))
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(self.thread("ameesh", 42), [])
        self.now += alert_briefs.LEASE_GRACE_S
        notifier.run_pass(self.db, current=[alert])
        [entry] = self.thread("ameesh", 42)
        self.assertIn("dev1 (no live lease)", entry.text)

    def test_morts_qui_le_restent_ne_recalculent_pas_les_briefs(self):
        self.dead("auditeur")
        notifier = self.notifier()
        alert = self.idle()
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.mails("orch")), 1)
        [entry] = notifier.state["briefs"].values()
        self.assertEqual(entry["dead"], ["auditeur"])
        with mock.patch.object(alert_briefs, "briefs",
                               wraps=alert_briefs.briefs) as spy:
            for _ in range(3):
                self.now += 30.0
                notifier.run_pass(self.db, current=[alert])
            self.assertEqual(spy.call_count, 0)
            # l'auditeur revient : réévaluation, remise du brief initial
            self.lease("auditeur")
            self.now += 30.0
            notifier.run_pass(self.db, current=[alert])
            self.assertEqual(spy.call_count, 1)
        [mail] = self.mails("auditeur")
        self.assertFalse(mail["payload"]["reminder"])
        self.assertEqual(len(self.mails("orch")), 1)

    def test_relance_et_retour_dans_la_meme_passe_sans_double(self):
        self.dead("auditeur")
        notifier = self.notifier()
        alert = self.idle()
        notifier.run_pass(self.db, current=[alert])
        # 6 h plus tard, l'auditeur revient au moment de la relance
        self.lease("auditeur")
        self.now += alert_briefs.REMIND_AFTER_S
        notifier.run_pass(self.db, current=[alert])
        orch = self.mails("orch")
        self.assertEqual([m["payload"]["reminder"] for m in orch], [False, True])
        [mail] = self.mails("auditeur")
        self.assertFalse(mail["payload"]["reminder"])
        [entry] = notifier.state["briefs"].values()
        self.assertTrue(entry["reminded"])
        # passe suivante : ni relance pour le revenu, ni second envoi
        self.now += 30.0
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.mails("auditeur")), 1)
        self.assertEqual(len(self.mails("orch")), 2)

    def test_pending_orphelin_est_oublie(self):
        notifier = self.notifier()
        alert = self.idle()
        key = notify.key_of(alert)
        notifier.state["briefs"][key] = {
            "type": "idle_capacity", "sent_ts": self.now, "reminded": True,
            "to": ["auditeur", "orch"], "dead": [], "pending": ["ancien"]}
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(notifier.state["briefs"][key]["pending"], [])
        with mock.patch.object(alert_briefs, "briefs",
                               wraps=alert_briefs.briefs) as spy:
            self.now += 30.0
            notifier.run_pass(self.db, current=[alert])
            self.assertEqual(spy.call_count, 0)
        self.assertEqual(self.mails("orch"), [])


if __name__ == "__main__":
    unittest.main()
