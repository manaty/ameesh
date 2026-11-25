# SPDX-License-Identifier: AGPL-3.0-only
"""#8266 : une pause budget de famille et de compte ne réveille qu'une fois.

Le 24/11, six agents codex en pause sur le même compte ont produit douze
courriers en une minute : chaque destinataire a été réveillé deux fois pour
une pause qu'aucune action ne pouvait lever (décision owner #233013 a). Le
lot demande une alerte par famille et par compte, qui liste les agents, des
courriers regroupés sur une fenêtre courte, et un rapport d'information sans
réveil tant que la décision owner datée couvre la pause — sauf l'agent
critique sans tour possible, qui garde son `critical_budget_pause` urgent.

La couverture est lue sur le canon (`auto_take`, trace mécanique de la
décision) et jamais devinée. Le banc vérifie aussi le texte, le routage vers
les orchestrateurs des projets des agents et la fenêtre de regroupement du
notifieur.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from ameesh import alert_briefs, exploitation, mechanics, notify, registry, storage

from .support import PgTestCase
from .test_l1616_mechanics import MECHANICS

PAUSE = "budget : pause (account secondaire, five_hour 96 %) until 14:20; …"
CRITICAL = ("budget : critical agent: pause (account primaire, five_hour 96 %) "
            "until 14:20; …")


def canon(*, families=("codex",), expire="2099-01-01"):
    """Le canon de test avec la trace `auto_take` de la décision datée."""
    return mechanics.compile_document(MECHANICS + (
        "auto_take:\n  excluded_families: [%s]\n  expire: %s\n"
        % (", ".join(families), expire)))


def canon_without_decision():
    return mechanics.compile_document(MECHANICS)


class PauseBase(PgTestCase):
    """Le décor : des agents codex en pause sur un compte du canon."""

    def setUp(self):
        super().setUp()
        self.doc = canon()
        self.now = time.time()

    def agent(self, name, host="AG1"):
        self.register(name, "codex", cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET host = %s WHERE name = %s",
                        (host, name))

    def pause(self, name, text=PAUSE, minutes=61, since=None):
        registry.set_status(self.db, name, "blocked", status_text=text,
                            error="budget")
        if since is None:
            since = self.now - minutes * 60
        self.db.execute(
            "UPDATE agent_registry SET status_since = to_timestamp(%s)"
            " WHERE name = %s", (since, name))

    def alerts(self, doc=None, now=None):
        with mock.patch.object(mechanics, "configured",
                               return_value=doc or self.doc):
            return exploitation.alerts(self.cfg, self.db, now=now or self.now)

    def one(self, doc=None, now=None):
        found = [a for a in self.alerts(doc, now)
                 if a["type"] == "budget_pause_long"]
        self.assertEqual(len(found), 1, found)
        return found[0]


class GroupingTest(PauseBase):
    """Une alerte par famille et par compte, quel que soit le nombre d'agents."""

    def test_trois_agents_une_seule_alerte_qui_les_liste(self):
        self.agent("ouvrier0", "AG1")
        self.agent("ouvrier1", "AG2")
        self.agent("ouvrier2", "AG3")
        for name in ("ouvrier0", "ouvrier1", "ouvrier2"):
            self.pause(name)
        alert = self.one()
        self.assertTrue(alert["grouped"])
        self.assertEqual(sorted(alert["agents"]),
                         ["ouvrier0", "ouvrier1", "ouvrier2"])
        self.assertEqual(alert["hosts"], ["AG1", "AG2", "AG3"])
        self.assertEqual(alert["harness"], "codex")
        self.assertEqual(alert["account"], "secondaire")
        self.assertEqual(sorted(alert["pauses"]),
                         ["ouvrier0", "ouvrier1", "ouvrier2"])
        self.assertIn("ouvrier0 1 h", alert["detail"])
        self.assertIn("3 codex agents share the pause", alert["detail"])

    def test_la_cle_ne_change_pas_quand_un_agent_rejoint_la_pause(self):
        self.agent("ouvrier0", "AG1")
        self.agent("ouvrier1", "AG2")
        self.pause("ouvrier0")
        alone = self.one()
        self.pause("ouvrier1")
        together = self.one()
        self.assertTrue(together["grouped"])
        self.assertEqual(notify.brief_key_of(alone),
                         notify.brief_key_of(together))
        # La clé est celle de la cause (famille, compte), pas d'un agent.
        self.assertEqual(exploitation.alert_key(together),
                         ("budget_pause_long", "codex", "secondaire"))

    def test_un_compte_inconnu_garde_une_alerte_par_agent(self):
        self.agent("ouvrier0")
        self.agent("ouvrier1")
        sans_compte = "budget : pause (five_hour 96 %) until 14:20; …"
        self.pause("ouvrier0", sans_compte)
        self.pause("ouvrier1", sans_compte)
        found = [a for a in self.alerts() if a["type"] == "budget_pause_long"]
        self.assertEqual(len(found), 2)
        self.assertFalse(any(a.get("grouped") for a in found))


class CoverageTest(PauseBase):
    """La décision owner datée ne couvre que ce que le canon déclare."""

    def test_decison_non_echue_couvre_et_le_rapport_est_information(self):
        self.agent("ouvrier0")
        self.pause("ouvrier0")
        alert = self.one()
        self.assertTrue(alert["covered_by_decision"])
        self.assertEqual(alert["coverage"]["family"], "codex")
        self.assertEqual(alert["coverage"]["source"], "canon auto_take")
        self.assertGreater(alert["coverage"]["until_ts"], self.now)
        listing = storage.of(self.db).operations.listing()
        by_name = {row["name"]: row for row in listing}
        quiet = alert_briefs.quiet_modes(alert, ["orchestrateur"], by_name)
        self.assertEqual(quiet["orchestrateur"]["payload"], {"info": True})
        text = alert_briefs._budget_pause_long(alert)
        self.assertIn("Expected action: none", text)
        self.assertIn("information report", text)
        self.assertIn("no wake", text)
        self.assertIn("processus/mecanique.yaml", text)

    def test_la_couverture_suit_la_reprise_annoncee(self):
        self.agent("ouvrier0")
        resume = self.now + 3600
        self.pause("ouvrier0",
                   "budget : pause (account secondaire, five_hour 96 %%) "
                   "until 14:20@%d; …" % int(resume), minutes=25 * 60)
        alert = self.one()
        self.assertTrue(alert["covered_by_decision"])
        self.assertAlmostEqual(alert["coverage"]["until_ts"], resume, delta=2)

    def test_decision_echue_ne_couvre_plus(self):
        self.agent("ouvrier0")
        # now (2027) est au-delà de l'expiration du canon (26/11/2026).
        later = 1800000000.0
        self.pause("ouvrier0", since=later - 7200)
        alert = self.one(canon(expire="2026-11-26"), now=later)
        self.assertNotIn("covered_by_decision", alert)
        listing = storage.of(self.db).operations.listing()
        by_name = {row["name"]: row for row in listing}
        self.assertEqual(
            alert_briefs.quiet_modes(alert, ["orchestrateur"], by_name), {})
        text = alert_briefs._budget_pause_long(alert)
        self.assertIn("Expected action: check the caps", text)

    def test_famille_non_exclue_ne_couvre_pas(self):
        self.agent("ouvrier0")
        self.pause("ouvrier0")
        alert = self.one(canon(families=("deepseek",)))
        self.assertNotIn("covered_by_decision", alert)
        self.assertIn("Expected action: check the caps",
                      alert_briefs._budget_pause_long(alert))

    def test_sans_canon_aucune_couverture_devinee(self):
        self.agent("ouvrier0")
        self.pause("ouvrier0")
        alert = self.one(canon_without_decision())
        self.assertNotIn("covered_by_decision", alert)

    def test_agent_critique_garde_son_alerte_urgente(self):
        self.agent("ouvrier0")
        self.pause("ouvrier0", CRITICAL)
        alerts = self.alerts()
        types = [a["type"] for a in alerts]
        self.assertIn("critical_budget_pause", types)
        self.assertIn("budget_pause_long", types)
        critical = [a for a in alerts if a["type"] == "critical_budget_pause"][0]
        listing = storage.of(self.db).operations.listing()
        by_name = {row["name"]: row for row in listing}
        self.assertEqual(
            alert_briefs.quiet_modes(critical, ["orchestrateur"], by_name), {})
        pause = [a for a in alerts if a["type"] == "budget_pause_long"][0]
        self.assertIn("critical agent(s) ouvrier0 keep their own urgent",
                      alert_briefs._budget_pause_long(pause))


class BriefsTest(unittest.TestCase):
    """Le groupe parle aux orchestrateurs de tous ses projets et à l'auditeur."""

    def test_routage_union_des_projets_et_auditeur(self):
        alert = {
            "type": "budget_pause_long", "agent": "ouvrier0",
            "agents": ["ouvrier0", "ouvrier1", "ouvrier2"],
            "grouped": True, "harness": "codex", "account": "secondaire",
            "hosts": ["AG1", "AG2"], "detail": "3 agents",
            "covered_by_decision": True,
            "coverage": {"family": "codex", "until_ts": 1e12,
                         "rule": "processus/mecanique.yaml",
                         "rule_expire": "2099-01-01", "timezone": "Europe/Paris"},
            "orchestrators": ["orch-amh", "orch-nxl"],
        }
        listing = [
            {"name": "ouvrier0", "team": "ameesh"},
            {"name": "ouvrier1", "team": "ameesh"},
            {"name": "ouvrier2", "team": "nexlink"},
            {"name": "orch-amh", "team": "ameesh"},
            {"name": "orch-nxl", "team": "nexlink"},
            {"name": "auditeur", "team": "ameesh"},
        ]
        out = alert_briefs.briefs(
            alert, listing, auditor_names=["auditeur"],
            orchestrator_names=("orch-amh", "orch-nxl"))
        self.assertEqual(set(out), {"orch-amh", "orch-nxl", "auditeur"})
        self.assertIn("3 agents paused", out["orch-amh"])
        self.assertIn("no wake", out["orch-amh"])
        # En pause, les agents listés ne reçoivent pas le rapport.
        self.assertNotIn("ouvrier0", out)

    def test_sujet_nombre_d_agents(self):
        subject = notify._subject({
            "type": "budget_pause_long", "agent": "ouvrier0",
            "agents": ["ouvrier0", "ouvrier1"], "grouped": True,
            "harness": "codex", "account": "secondaire",
        })
        self.assertIn("2 agents", subject)
        self.assertIn("account codex/secondaire", subject)


class CoalesceTest(PgTestCase):
    """Les courriers d'une même cause sont regroupés sur une fenêtre courte."""

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM agent_mailbox")
        self.briefs = mock.patch.object(alert_briefs, "briefs",
                                        return_value={"pilote": "pause budget"})
        self.briefs.start()
        self.addCleanup(self.briefs.stop)
        self.notifier = notify.Notifier(
            self.cfg, notify.parse_config({"default": []}), log=lambda text: None)

    def alert(self, *, now, covered=True, since=None):
        since = now - 3700 if since is None else since
        return {
            "schema": "ameesh-alert/1", "type": "budget_pause_long",
            "agent": "ouvrier", "agents": ["ouvrier"], "since": since,
            "value": 3700, "threshold": 3600, "host": "AG1",
            "harness": "codex", "account": "secondaire", "detail": "pause",
            "covered_by_decision": covered,
            "coverage": {"family": "codex", "until_ts": now + 86400,
                         "rule": "processus/mecanique.yaml",
                         "rule_expire": "2099-01-01", "timezone": "Europe/Paris"},
            "orchestrators": ["pilote"],
        }

    def box(self):
        return [row for row in storage.of(self.db).mailbox.history("pilote", 20)
                if (row.get("payload") or {}).get("alert") == "budget_pause_long"]

    def test_une_pause_couverte_attend_la_fenetre_puis_part_une_fois(self):
        now = time.time()
        alert = self.alert(now=now)
        self.notifier._brief_agents(self.db, [alert], None, now)
        self.assertEqual(self.box(), [])
        entry = self.notifier.state["briefs"][notify.brief_key_of(alert)]
        self.assertIsNotNone(entry.get("coalesce_until"))

        # La fenêtre part du franchissement du seuil (since + threshold) :
        # 3700 s de pause = seuil franchi il y a 100 s, fin dans 20 s.
        self.notifier._brief_agents(self.db, [alert], None, now + 30)
        rows = self.box()
        self.assertEqual(len(rows), 1)
        self.assertTrue((rows[0].get("payload") or {}).get("info"))
        entry = self.notifier.state["briefs"][notify.brief_key_of(alert)]
        self.assertNotIn("coalesce_until", entry)
        self.assertEqual(entry["to"], ["pilote"])

        # Rien de neuf, aucune relance avant 6 h.
        self.notifier._brief_agents(self.db, [alert], None, now + 60)
        self.assertEqual(len(self.box()), 1)

    def test_la_chute_de_couverture_reveille_sans_attendre(self):
        now = time.time()
        covered = self.alert(now=now)
        self.notifier._brief_agents(self.db, [covered], None, now)
        self.assertEqual(self.box(), [])
        uncovered = self.alert(now=now + 10, covered=False)
        self.notifier._brief_agents(self.db, [uncovered], None, now + 10)
        rows = self.box()
        self.assertEqual(len(rows), 1)
        self.assertFalse((rows[0].get("payload") or {}).get("info"))

    def test_une_pause_non_couverte_part_tout_de_suite(self):
        now = time.time()
        self.notifier._brief_agents(self.db, [self.alert(now=now, covered=False)],
                                    None, now)
        self.assertEqual(len(self.box()), 1)


if __name__ == "__main__":
    unittest.main()
