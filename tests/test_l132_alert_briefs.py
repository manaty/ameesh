# SPDX-License-Identifier: AGPL-3.0-only
"""L132 : `stale_lot`, `plan_underused` et `idle_capacity` partent aussi, par
courrier `event`, aux orchestrateurs et à l'auditeur, avec l'action attendue ;
une fois par alerte, une relance après 6 h. L713 : `stale_lot` ne réveille plus
l'auditeur, qui le lit à sa passe (`ameesh alerts --json`)."""
from __future__ import annotations

import time
import unittest

from ameesh import alert_briefs, notify, registry

from .support import PgTestCase

HEURE = 3600.0


def _listing(*rows):
    return [dict(name=n, team=t) for n, t in rows]


STALE = {"type": "stale_lot", "agent": "dev1", "lot": 42, "title": "export",
         "lot_state": "build", "value": 7 * 3600, "waiting_for": None,
         "orchestrators": ["orch", "orch-b"]}
PLAN = {"type": "plan_underused", "harness": "claude", "account": "pro",
        "gauge": "7d", "detail": "compte claude/pro, fenêtre 7d : 20 % utilisés",
        "idle_agents": ["dev2"], "orchestrators": ["orch", "orch-b"]}


class BriefsTest(unittest.TestCase):
    """Les textes : destinataires et commande exacte."""

    def setUp(self):
        self.listing = _listing(("orch", "a"), ("orch-b", "b"), ("dev1", "a"),
                                ("dev2", "b"), ("auditeur", ""))

    def test_stale_lot(self):
        briefs = alert_briefs.briefs(STALE, self.listing, auditor_names=["auditeur"])
        self.assertEqual(sorted(briefs), ["orch"])   # équipe de l'assigné
        text = briefs["orch"]
        self.assertIn('#42 "export"', text)
        self.assertIn('ameesh mail send dev1 "…" --lot 42', text)
        self.assertIn("ameesh work merged 42 --sha", text)
        self.assertIn("ameesh work close 42 --abandoned", text)

    def test_stale_lot_ne_reveille_pas_l_auditeur(self):
        """L713 : lu à la passe de l'auditeur, pas poussé ; les autres alertes si."""
        briefs = alert_briefs.briefs(STALE, self.listing, auditor_names=["auditeur"])
        self.assertNotIn("auditeur", briefs)
        idle = {"type": "idle_capacity", "agents": ["dev1"], "lots": [],
                "orchestrators": ["orch"], "detail": "1 agent au repos"}
        self.assertIn("auditeur", alert_briefs.briefs(idle, self.listing,
                                                      auditor_names=["auditeur"]))

    def test_stale_lot_sans_assigne(self):
        alert = dict(STALE, agent=None)
        briefs = alert_briefs.briefs(alert, self.listing)
        self.assertEqual(sorted(briefs), ["orch", "orch-b"])     # équipe inconnue : tous
        self.assertIn("ameesh work assign 42 <agent>", briefs["orch"])

    def test_plan_underused(self):
        briefs = alert_briefs.briefs(PLAN, self.listing, auditor_names=["auditeur"])
        self.assertEqual(sorted(briefs), ["auditeur", "orch-b"])
        self.assertIn("idle agents (dev2)", briefs["orch-b"])
        self.assertIn("--lot <id>", briefs["orch-b"])
        no_idle = alert_briefs.briefs(dict(PLAN, idle_agents=[]), self.listing)
        self.assertIn("ameesh resume <agent>", no_idle["orch"])

    def test_idle_capacity_et_autres(self):
        alert = {"type": "idle_capacity", "agents": ["dev1"], "lots": [],
                 "orchestrators": ["orch"], "threshold": 1800.0,
                 "detail": "1 agent au repos. Action : répartir"}
        briefs = alert_briefs.briefs(alert, self.listing, auditor_names=["auditeur"])
        self.assertIn("ameesh work assign", briefs["orch"])
        self.assertIn("Action : répartir", briefs["auditeur"])
        # #1608 : bloqué -> l'auditeur ne reçoit pas l'ordre d'assigner ;
        # démarrable -> il le reçoit
        bloque = dict(alert, lots=[12], startable_lots=[],
                      blocked_lots={"12": {"what": "dependency", "lots": [7]}})
        auditeur = alert_briefs.briefs(bloque, self.listing, auditor_names=["auditeur"])["auditeur"]
        self.assertIn("Action : répartir", auditeur)
        self.assertNotIn("Lots are waiting", auditeur)
        demarrable = dict(alert, lots=[12], startable_lots=[12])
        auditeur = alert_briefs.briefs(demarrable, self.listing,
                                       auditor_names=["auditeur"])["auditeur"]
        self.assertIn("Lots are waiting", auditeur)
        self.assertEqual(alert_briefs.briefs({"type": "long_turn"}, self.listing), {})

    def test_orchestrateur_et_auditeur(self):
        briefs = alert_briefs.briefs(dict(STALE, orchestrators=["auditeur"]), self.listing,
                                     auditor_names=["auditeur"])
        self.assertEqual(list(briefs), ["auditeur"])

    def test_auditeurs(self):
        self.assertEqual(alert_briefs.auditors(self.listing), ["auditeur"])
        self.assertEqual(alert_briefs.auditors(_listing(("x", ""))), [])

    def test_relance(self):
        text = alert_briefs.reminder("Stale lot", 0.0, 6 * HEURE)
        self.assertTrue(text.startswith("Reminder, alert still active for 6 h"))


class NotifierBriefsTest(PgTestCase):
    """Le service : une fois, une relance à 6 h, rien après ; hors `notify.types`."""

    def setUp(self):
        super().setUp()
        for name, team in (("orch", "a"), ("dev1", "a"), ("auditeur", None)):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
            self.db.execute("UPDATE agent_registry SET team = %s WHERE name = %s",
                            (team, name))
        self.now = time.time()

    def mails(self, name):
        return self.db.query("SELECT body, kind, payload FROM agent_mailbox"
                             " WHERE recipient = %s ORDER BY id", (name,))

    def test_une_fois_puis_une_relance(self):
        clock = [self.now]
        ncfg = notify.parse_config({"default": []})
        self.assertNotIn("stale_lot", ncfg.types)
        notifier = notify.Notifier(self.cfg, ncfg, sender=object(), log=lambda t: None,
                                   clock=lambda: clock[0])
        alert = dict(STALE, orchestrators=["orch"])
        notifier.run_pass(self.db, current=[alert])
        notifier.run_pass(self.db, current=[alert])
        rows = self.mails("orch")
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["kind"], "event")
        self.assertIn("ameesh mail send dev1", rows[0]["body"])
        self.assertEqual(len(self.mails("auditeur")), 0)   # L713 : pas de réveil
        clock[0] += 6 * HEURE
        notifier.run_pass(self.db, current=[alert])
        rows = self.mails("orch")
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1]["body"].startswith("Reminder"))
        clock[0] += 6 * HEURE
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.mails("orch")), 2)               # une seule relance
        notifier.run_pass(self.db, current=[])                     # résolue : oubliée
        self.assertEqual(notifier.state["briefs"], {})
        notifier.run_pass(self.db, current=[alert])                # levée de nouveau
        self.assertEqual(len(self.mails("orch")), 3)


if __name__ == "__main__":
    unittest.main()
