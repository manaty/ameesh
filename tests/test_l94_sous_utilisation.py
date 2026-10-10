# SPDX-License-Identifier: AGPL-3.0-only
"""L94 : la sous-utilisation alerte autant que la surcharge — `plan_underused`,
`idle_capacity`, `orchestrator_held`, `host_underused`, leur envoi par
`ameesh notify` et leurs seuils réglables."""
from __future__ import annotations

import time
import unittest

from ameesh import exploitation, mail, notify, registry, storage, sous_utilisation, work

from .support import PgTestCase

HEURE = 3600.0
JOUR = 86400.0


def _seuils(**kw):
    """Seuils d'`alerts()` : les défauts, les autres alertes neutralisées."""
    args = exploitation.build_parser().parse_args(["alerts"])
    out = exploitation.thresholds(args)
    out.update(kw)
    return out


def _types(alertes, *types):
    return [a for a in alertes if a["type"] in types]


class _Base(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE quota_gauge_readings, host_resources, turn_costs "
                        "RESTART IDENTITY")
        self.now = time.time()

    # -- jauges --------------------------------------------------------------
    def jauge(self, harness, key, used, resets_in, window_s, account=None):
        storage.of(self.db).operations.record_gauges([{
            "harness": harness, "key": key, "used": used,
            "resets_at": self.now + resets_in, "window_s": window_s, "account": account}])

    # -- agents --------------------------------------------------------------
    def agent(self, name, harness="claude", *, status="idle", owner=None, since_s=2 * HEURE,
              host=None, responsible="human:proprio"):
        registry.upsert(self.db, name, harness=harness, host=host or self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET status = %s, lease_owner = %s,"
            " lease_expires_at = CASE WHEN %s THEN now() + interval '1 hour' END,"
            " responsible = %s WHERE name = %s",
            (status, owner if owner is not None else "runner-%s@h" % name,
             owner != "", responsible, name))
        self.db.execute("UPDATE agent_registry SET status_since = now() - make_interval("
                        "secs => %s), last_turn_at = now() - make_interval(secs => %s)"
                        " WHERE name = %s", (since_s, since_s, name))

    def listing(self):
        return storage.of(self.db).operations.listing()


class PlanUnderusedTest(_Base):
    def alertes(self, **kw):
        return _types(exploitation.alerts(self.cfg, self.db, now=self.now, **_seuils(**kw)),
                      "plan_underused")

    def test_sous_rythme_sur_sept_jours(self):
        # 3 jours écoulés sur 7 : rythme permis 53 % ; 20 % utilisés → écart 33
        self.jauge("codex", "codex-10080min", 0.20, 4 * JOUR, 7 * JOUR, account="primaire")
        self.jauge("codex", "codex-10080min", 0.45, 4 * JOUR, 7 * JOUR, account="secondaire")
        out = self.alertes()
        self.assertEqual([(a["account"], a["reason"]) for a in out],
                         [("primaire", "sous_rythme")])
        alerte = out[0]
        self.assertEqual((alerte["harness"], alerte["gauge"], alerte["value"]),
                         ("codex", "codex-10080min", 80))
        self.assertAlmostEqual(alerte["lost"], 0.8)
        self.assertAlmostEqual(alerte["pace_cap"], 3 / 7 + 0.10, places=2)
        # la projection au rythme actuel : 20 % en 3/7 → 47 % à l'échéance
        self.assertAlmostEqual(alerte["lost_at_pace"], 1 - 0.2 * 7 / 3, places=2)
        self.assertIn("perte estimée", alerte["detail"])
        self.assertIn("rythme permis de 53 %", alerte["detail"])
        # `since` : le début de la fenêtre — stable tant qu'elle dure
        self.assertAlmostEqual(alerte["since"], self.now + 4 * JOUR - 7 * JOUR, places=0)
        # seuil réglable, et 0 désactive
        self.assertEqual(self.alertes(plan_pace_gap_pct=40), [])
        self.assertEqual(self.alertes(plan_pace_gap_pct=0), [])

    def test_debut_de_fenetre_et_fenetre_courte_hors_rythme(self):
        # 10 % écoulés seulement : l'écart ne dit encore rien
        self.jauge("claude", "seven_day", 0.0, 6.3 * JOUR, 7 * JOUR, account="primaire")
        # fenêtre de 5 h : jamais jugée au rythme, seulement en fin de fenêtre
        self.jauge("claude", "five_hour", 0.05, 3 * HEURE, 5 * HEURE, account="primaire")
        self.assertEqual(self.alertes(), [])

    def test_fin_de_fenetre(self):
        self.jauge("claude", "five_hour", 0.10, 30 * 60, 5 * HEURE, account="primaire")
        self.jauge("claude", "seven_day", 0.70, 2 * JOUR, 7 * JOUR, account="primaire")
        out = self.alertes()
        self.assertEqual([(a["gauge"], a["reason"]) for a in out],
                         [("five_hour", "fin_de_fenetre")])
        self.assertIn("remise à zéro dans 30 min", out[0]["detail"])
        self.assertEqual(out[0]["value"], 90)
        # au-dessus du seuil d'utilisation, ou réglage plus court : rien
        self.assertEqual(self.alertes(plan_used_pct=10), [])
        self.assertEqual(self.alertes(plan_tail_s=600), [])

    def test_fenetre_bloquee_par_une_autre_au_seuil(self):
        # la fenêtre de 7 jours est au seuil : la capacité de 5 h n'est pas
        # utilisable, rien n'est perdu par négligence
        self.jauge("claude", "five_hour", 0.10, 30 * 60, 5 * HEURE, account="primaire")
        self.jauge("claude", "seven_day", 0.95, 2 * JOUR, 7 * JOUR, account="primaire")
        self.assertEqual(self.alertes(), [])

    def test_releve_echu_et_releves_sans_compte(self):
        # fenêtre échue : la suivante ne court qu'au premier usage
        self.jauge("codex", "codex-300min", 0.0, -60, 5 * HEURE, account="secondaire")
        # relevé sans compte d'un harnais qui a des comptes : c'est le défaut,
        # déjà compté
        self.jauge("codex", "codex-10080min", 0.20, 4 * JOUR, 7 * JOUR)
        self.jauge("codex", "codex-10080min", 0.20, 4 * JOUR, 7 * JOUR, account="primaire")
        self.assertEqual([a["account"] for a in self.alertes()], ["primaire"])

    def test_sans_comptes_et_contexte(self):
        self.jauge("claude", "seven_day", 0.10, 3 * JOUR, 7 * JOUR)
        self.agent("claude1")
        self.agent("deepseek1", "deepseek", status="running")
        self.db.execute("INSERT INTO turn_costs (agent, harness, model, usd) "
                        "VALUES ('deepseek1', 'deepseek', 'deepseek-pro', 12.5)")
        out = self.alertes()
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["account"])
        self.assertEqual(out[0]["idle_agents"], ["claude1"])
        self.assertEqual(out[0]["responsible"], "human:proprio")
        self.assertIn("agents claude au repos : claude1", out[0]["detail"])
        self.assertIn("12.50 $ payés au token sur 24 h", out[0]["detail"])
        self.assertEqual(exploitation._format_alert(out[0])[:33],
                         "[plan_underused] claude/défaut : ")

    def test_cle_de_dedoublonnage_par_compte(self):
        self.jauge("codex", "codex-10080min", 0.20, 4 * JOUR, 7 * JOUR, account="primaire")
        self.jauge("codex", "codex-10080min", 0.10, 4 * JOUR, 7 * JOUR, account="secondaire")
        out = self.alertes()
        self.assertEqual(len({exploitation.alert_key(a) for a in out}), 2)
        # les clés des autres alertes ne changent pas (état de notify)
        autre = {"type": "host_pressure", "agent": None, "host": "h", "since": None}
        self.assertEqual(len(exploitation.alert_key(autre)), 6)


class IdleCapacityTest(_Base):
    def alertes(self, **kw):
        return _types(exploitation.alerts(self.cfg, self.db, now=self.now, **_seuils(**kw)),
                      "idle_capacity", "orchestrator_held")

    def test_lots_sans_assigne(self):
        self.agent("claude1")
        self.agent("codex2", "codex")
        self.agent("frais", since_s=60)                     # au repos depuis 1 min
        self.agent("arrete", status="stopped", owner="")    # pas réveillable
        self.assertEqual(self.alertes(), [])                # rien n'attend
        lot = work.add(self.db, title="à prendre")
        out = self.alertes()
        self.assertEqual(len(out), 1)
        alerte = out[0]
        self.assertEqual((alerte["reason"], alerte["agents"], alerte["lots"]),
                         ("travail_en_attente", ["claude1", "codex2"], [lot["id"]]))
        self.assertIn("Action : répartir", alerte["detail"])
        self.assertEqual(alerte["responsible"], "human:proprio")
        # un agent avec un lot assigné ou du courrier n'est pas « au repos sans lot »
        work.assign(self.db, lot["id"], "claude1")
        self.assertEqual(self.alertes(), [])
        self.assertEqual(self.alertes(idle_capacity_s=0), [])

    def test_forfait_au_repos_pendant_que_le_token_travaille(self):
        self.agent("claude1")
        self.agent("deepseek1", "deepseek", status="running", since_s=60)
        out = self.alertes()
        self.assertEqual([(a["reason"], a["paid_working"]) for a in out],
                         [("forfait_au_repos", ["deepseek1"])])
        self.assertIn("payés au token travaillent (deepseek1)", out[0]["detail"])

    def test_courrier_en_souffrance_chez_un_agent_occupe(self):
        self.agent("claude1")
        self.agent("claude2", status="running", since_s=60)
        mail.send(self.db, "x", "claude2", "à traiter")
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '2 hours'")
        out = self.alertes()
        self.assertEqual([a["reason"] for a in out if a["type"] == "idle_capacity"],
                         ["travail_en_attente"])
        self.assertIn("courrier en souffrance chez claude2", out[0]["detail"])

    def test_orchestrateur_tenu_par_attach(self):
        self.agent("claude1")
        self.agent("orch", status="running", owner="attach:proprio@poste", since_s=3 * HEURE)
        work.add(self.db, title="à répartir")
        mail.send(self.db, "claude1", "orch", "fini, la suite ?")
        # non déclaré, ni rôle, ni lot confié : pas un orchestrateur connu
        self.assertEqual([a["type"] for a in self.alertes()], ["idle_capacity"])
        out = self.alertes(orchestrators_declared=("orch",))
        tenu = _types(out, "orchestrator_held")
        self.assertEqual([(a["agent"], a["value"], a["owner"]) for a in tenu],
                         [("orch", 1, "attach:proprio@poste")])
        self.assertIn("il ne répartit plus", tenu[0]["detail"])
        repos = _types(out, "idle_capacity")[0]
        self.assertIn("réveiller l'orchestrateur : orch est tenu", repos["detail"])
        # seuil plus long que la tenue : rien
        self.assertEqual(_types(self.alertes(orchestrators_declared=("orch",),
                                             orchestrator_held_s=4 * HEURE),
                                "orchestrator_held"), [])
        # courrier lu : rien
        self.db.execute("UPDATE agent_mailbox SET delivered_at = now()")
        self.assertEqual(_types(self.alertes(orchestrators_declared=("orch",)),
                                "orchestrator_held"), [])

    def test_orchestrateur_reconnu_a_ses_assignations(self):
        self.agent("claude1")
        self.agent("orch", status="running", owner="attach:proprio@poste", since_s=3 * HEURE)
        lot = work.add(self.db, title="lot")
        work.assign(self.db, lot["id"], "claude1", actor="agent:orch")
        self.assertEqual(sous_utilisation.orchestrators(self.cfg, self.db, self.listing()),
                         ["orch"])
        # l'assigné qui se prend lui-même un lot n'est pas un orchestrateur
        autre = work.add(self.db, title="autre")
        work.assign(self.db, autre["id"], "orch", actor="orch")
        self.assertEqual(sous_utilisation.orchestrators(self.cfg, self.db, self.listing()),
                         ["orch"])


class HostUnderusedTest(_Base):
    def releve(self, host, *, load, cpus=4, turns=0, ago_s=0, mem=8 * 1024 ** 3):
        self.db.execute(
            "INSERT INTO host_resources (host, sampled_at, mem_available_bytes, "
            "swap_used_bytes, load1, cpu_count, disk_free_bytes, turns_in_progress) "
            "VALUES (%s, now() - make_interval(secs => %s), %s, 0, %s, %s, %s, %s)",
            (host, ago_s, mem, load, cpus, 50 * 1024 ** 3, turns))

    def alertes(self, **kw):
        return _types(exploitation.alerts(self.cfg, self.db, now=self.now, **_seuils(**kw)),
                      "host_underused")

    def test_hote_a_vide_pendant_qu_un_autre_est_sous_pression(self):
        for ago in (3500, 2400, 1200, 10):
            self.releve("vm", load=0.1, ago_s=ago)
        self.releve("poste", load=2.0, cpus=12, turns=8, mem=100 * 1024 ** 2)
        out = self.alertes()
        self.assertEqual([(a["host"], a["reason"], a["pressured"]) for a in out],
                         [("vm", "pression_ailleurs", ["poste"])])
        self.assertIn("aucun déplacement automatique", out[0]["detail"])
        self.assertIn("ameesh placement check", out[0]["detail"])
        # une seule pointe au-dessus du seuil de charge : pas à vide
        self.releve("vm", load=2.0, ago_s=600)
        self.assertEqual(self.alertes(), [])
        self.assertEqual(len(self.alertes(host_underused_load=0.6)), 1)

    def test_rien_sans_pression_ni_attente_ni_fenetre_couverte(self):
        for ago in (3500, 10):
            self.releve("vm", load=0.1, ago_s=ago)
        self.releve("poste", load=1.0, cpus=12)
        self.assertEqual(self.alertes(), [])
        # des agents attendent ailleurs (en pause) : suggestion
        self.agent("claude1", status="blocked", host="poste")
        self.assertEqual([a["reason"] for a in self.alertes()], ["agents_en_attente"])
        # fenêtre plus longue que les relevés : on ne conclut pas
        self.assertEqual(self.alertes(host_underused_s=4 * HEURE), [])
        self.assertEqual(self.alertes(host_underused_s=0), [])


class NotifyTest(_Base):
    def test_types_par_defaut_texte_et_resume(self):
        for kind in sous_utilisation.UNDERUSE_TYPES:
            self.assertIn(kind, notify.DEFAULT_TYPES)
            self.assertIn(kind, exploitation.ALERT_TYPES)
        alerte = {"type": "plan_underused", "agent": None, "harness": "codex",
                  "account": "secondaire", "gauge": "codex-10080min", "since": self.now - JOUR,
                  "detail": "perte estimée : 86 %"}
        message = notify.render(alerte, "raised", self.now)
        self.assertEqual(message.title, "ameesh : forfait sous-employé (codex/secondaire)")
        self.assertIn("compte codex/secondaire · fenêtre codex-10080min", message.body)
        self.assertFalse(message.urgent)
        resume = notify.render_summary({"raised": {"plan_underused": 2, "idle_capacity": 1,
                                                   "orphan_lot": 1}, "resolved": 0},
                                       "poste")
        self.assertIn("sous-utilisation : 3 (1 capacité au repos, 2 forfait sous-employé)",
                      resume.body)

    def test_envoi_puis_resolution(self):
        self.jauge("codex", "codex-10080min", 0.20, 4 * JOUR, 7 * JOUR, account="primaire")
        self.agent("codex2", "codex")
        envois = []

        class Faux:
            def send(self, channel, message):
                envois.append(message)

        ncfg = notify.parse_config({"default": ["desktop"]})
        notifier = notify.Notifier(self.cfg, ncfg, sender=Faux(), log=lambda t: None,
                                   clock=lambda: self.now)
        records = notifier.run_pass(self.db, thresholds=_seuils())
        levees = [r for r in records if r["type"] == "plan_underused"]
        self.assertEqual([(r["event"], r["human"], r["delivery"]) for r in levees],
                         [("raised", "human:proprio", "envoyee")])
        self.assertEqual(notifier.run_pass(self.db, thresholds=_seuils()), [])
        # le compte reprend du service : résolue
        self.jauge("codex", "codex-10080min", 0.45, 4 * JOUR, 7 * JOUR, account="primaire")
        records = notifier.run_pass(self.db, thresholds=_seuils())
        self.assertEqual([(r["type"], r["event"]) for r in records],
                         [("plan_underused", "resolved")])
        self.assertIn("résolue", envois[-1].title)


class OptionsTest(unittest.TestCase):
    def test_seuils_par_defaut_et_options(self):
        args = exploitation.build_parser().parse_args(["alerts"])
        seuils = exploitation.thresholds(args)
        self.assertEqual((seuils["plan_tail_s"], seuils["plan_used_pct"],
                          seuils["plan_pace_gap_pct"], seuils["idle_capacity_s"],
                          seuils["orchestrator_held_s"], seuils["host_underused_s"],
                          seuils["host_underused_load"], seuils["host_underused_turns"]),
                         (86400.0, 50.0, 25.0, 1800.0, 1800.0, 3600.0, 0.25, 1.0))
        args = exploitation.build_parser().parse_args(
            ["alerts", "--plan-pace-gap", "10", "--orchestrators", "orch, coord"])
        seuils = exploitation.thresholds(args)
        self.assertEqual((seuils["plan_pace_gap_pct"], seuils["orchestrators_declared"]),
                         (10.0, ("orch", "coord")))


if __name__ == "__main__":
    unittest.main()
