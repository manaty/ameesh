# SPDX-License-Identifier: AGPL-3.0-only
"""#1021 : l'assignation refuse un agent que l'exécuteur ne réveillera pas.

#885/#886/#967 ont dormi chez des agents Codex en pause budget : la garde
d'attribution ne regardait que la réveillabilité du registre
(`registry.wakeable`), jamais la tenue budget/rythme. `work.check_assignee`
juge désormais la tenue là où l'exécuteur la calcule (`account_turn.turn_hold` :
la pause annoncée par l'exécuteur, puis une évaluation fraîche des comptes de
l'hôte), refuse en nommant la raison et l'échéance, et laisse passer un agent
sain (aucun faux positif).
"""
from __future__ import annotations

import dataclasses
import os
import time
from unittest import mock

from ameesh import account_turn, backlog, budget, exploitation, registry, work

from .test_l242b_critical_accounts import _Comptes
from .test_l94_underuse import _seuils, _types


class TenueAttributionTest(_Comptes):
    """Codex primaire/secondaire déclarés ; l'agent visé est sur cet hôte."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg = dataclasses.replace(
            self.cfg, accounts={"codex": [{"name": "primaire"}, self.secondaire]})
        self.name = "ouvrier-codex"
        registry.upsert(self.db, self.name, harness="codex", host=self.cfg.host,
                        cwd=self.tmp)

    def lot(self, **kw):
        kw.setdefault("cfg", self.cfg)
        return work.add(self.db, title="a confier", **kw)

    def _lots(self) -> int:
        return int(self.db.query("SELECT count(*) AS n FROM work_items")[0]["n"])

    # -- la tenue est refusée ---------------------------------------------

    def test_tenue_de_rythme_refuse_l_attribution(self):
        # les deux comptes Codex au seuil, sans pause encore annoncée par
        # l'exécuteur (statut `idle`) : c'est l'évaluation fraîche qui tranche
        self.jauge("primaire", 96)
        self.jauge("secondaire", 96)
        lot = self.lot()
        with self.assertRaises(work.WorkError) as ctx:
            work.assign(self.db, lot["id"], self.name, cfg=self.cfg)
        msg = str(ctx.exception)
        self.assertIn("is held —", msg)
        self.assertIn("until ", msg)                 # l'échéance est nommée
        self.assertIn("codex", msg)
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

    def test_creation_avec_assignee_refusee_ne_cree_rien(self):
        self.jauge("primaire", 96)
        self.jauge("secondaire", 96)
        with self.assertRaises(work.WorkError):
            self.lot(assignee=self.name)
        self.assertEqual(self._lots(), 0)

    def test_pause_annoncee_d_un_agent_distant_refusee(self):
        # l'hôte d'un autre agent n'est pas lisible ici : c'est la pause que
        # son exécuteur a écrite qui tranche
        registry.upsert(self.db, "distant", harness="codex", host="hote-voisin",
                        cwd=self.tmp)
        self.db.execute(
            "UPDATE agent_registry SET status = 'blocked', status_text = %s"
            " WHERE name = %s",
            ("budget : pause (account secondaire, codex-300min 96 %) until 14:20; "
             "all codex accounts at threshold", "distant"))
        with self.assertRaises(work.WorkError) as ctx:
            work.check_assignee(self.db, "distant", cfg=self.cfg)
        self.assertIn("until 14:20", str(ctx.exception))
        # sans pause annoncée, les comptes de CET hôte ne le jugent pas
        self.jauge("primaire", 96)
        self.jauge("secondaire", 96)
        self.db.execute("UPDATE agent_registry SET status = 'idle', status_text = ''"
                        " WHERE name = %s", ("distant",))
        self.assertEqual(work.check_assignee(self.db, "distant", cfg=self.cfg)["assignee"],
                         "distant")

    # -- pas de faux positif ----------------------------------------------

    def test_agent_sain_accepte(self):
        self.jauge("primaire", 10)
        self.jauge("secondaire", 10)
        check = work.check_assignee(self.db, self.name, cfg=self.cfg)
        self.assertEqual(check["assignee"], self.name)
        lot = self.lot(assignee=self.name)
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], self.name)

    def test_agent_arrete_garde_son_avertissement(self):
        # l'arrêt est un geste humain, pas une tenue : le lot est accepté avec
        # l'avertissement d'usage, même si un compte est au seuil
        self.jauge("primaire", 96)
        self.jauge("secondaire", 96)
        self.db.execute("UPDATE agent_registry SET status = 'stopped',"
                        " stop_reason = 'manuel' WHERE name = %s", (self.name,))
        check = work.check_assignee(self.db, self.name, cfg=self.cfg)
        self.assertIn("is stopped", check["warning"])

    def test_cle_absente_du_shell_n_est_pas_une_tenue(self):
        # un compte `api_key_env` : la clé est portée par l'exécuteur, pas par
        # le shell qui attribue — son absence ici ne refuse pas un agent sain
        # (un problème d'authentification se lit `auth_blocked`, pas ici)
        self.cfg = dataclasses.replace(
            self.cfg, accounts={"deepseek": [
                {"name": "cle", "type": "api_key_env", "key_env": "AMEESH_TEST_CLE_1021"}]})
        self.name = "ouvrier-deepseek"
        registry.upsert(self.db, self.name, harness="deepseek", host=self.cfg.host,
                        cwd=self.tmp)
        with mock.patch.dict(os.environ, {"AMEESH_TEST_CLE_1021": ""}):
            check = work.check_assignee(self.db, self.name, cfg=self.cfg)
        self.assertEqual(check["assignee"], self.name)

    def test_agent_critique_en_repli_n_est_pas_refuse(self):
        # L242b (3) : un agent critique au seuil sur son harnais tourne sous un
        # harnais de repli qui a de la marge — le refuser serait un faux positif
        self.cfg = dataclasses.replace(
            self.cfg,
            accounts={"codex": [{"name": "primaire"}, self.secondaire],
                      "claude": self._claude_comptes()},
            critical_agents=(self.name,),
            critical_fallback_harnesses=("claude",))
        self.jauge("primaire", 96)
        self.jauge("secondaire", 96)
        with self._adapters():
            check = work.check_assignee(self.db, self.name, cfg=self.cfg)
        self.assertEqual(check["assignee"], self.name)
        # sans repli déclaré, la tenue reprend
        self.cfg = dataclasses.replace(self.cfg, critical_fallback_harnesses=())
        with self.assertRaises(work.WorkError):
            work.check_assignee(self.db, self.name, cfg=self.cfg)

    def test_plafond_journalier_ne_refuse_pas_si_le_rythme_est_coupe(self):
        # B1 de la R1 (#1021) : garde de rythme coupée (`per_hour = 0`).
        # L'exécuteur (`Runner.budget_ok`) ne juge alors que les règles
        # absolues des comptes (L226b, déjà vues par `accounts.choose`), pas
        # les plafonds payés au token — les appliquer ici refuserait un tour
        # qu'il autorise.
        cfg = dataclasses.replace(
            self.cfg, budget_usd_per_hour=0.0, budget_usd_per_day=10.0,
            budget_explicit=("budget_usd_per_hour", "budget_usd_per_day"),
            accounts={"deepseek": [{"name": "cle", "type": "api_key_env",
                                    "key_env": "AMEESH_TEST_CLE_1021"}]})
        registry.upsert(self.db, "ouvrier-deepseek", harness="deepseek",
                        host=cfg.host, cwd=self.tmp)
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, model, usd, recorded_at)"
            " VALUES ('ouvrier-deepseek', 'deepseek', 'm', 20.0, now())")
        row = registry.get(self.db, "ouvrier-deepseek")
        coupe = budget.Limits(per_hour=0.0, per_hour_source=budget.SOURCE_CONFIG,
                              per_day=10.0, per_day_source=budget.SOURCE_CONFIG)
        with mock.patch.object(budget, "current", return_value=coupe):
            self.assertEqual(
                account_turn.turn_hold(cfg, self.db, "ouvrier-deepseek", row), "")
            self.assertEqual(work.check_assignee(self.db, "ouvrier-deepseek",
                                                 cfg=cfg)["assignee"],
                             "ouvrier-deepseek")
        # garde de rythme active : le plafond journalier du mesh tient
        actif = dataclasses.replace(coupe, per_hour=50.0)
        with mock.patch.object(budget, "current", return_value=actif):
            hold = account_turn.turn_hold(cfg, self.db, "ouvrier-deepseek", row)
        self.assertIn("daily budget", hold)

    def test_tenue_d_authentification_non_levee_par_le_repli(self):
        # B2 de la R1 (#1021) : le seul compte sous son seuil est en échec
        # d'authentification ; l'exécuteur retient ce compte (aucun repli
        # n'est engagé), puis `auth_ready` bloque l'agent. Un repli sain ne
        # doit pas lever cette tenue-là — le lot dormirait.
        self.cfg = dataclasses.replace(
            self.cfg,
            accounts={"codex": [{"name": "primaire"}, self.secondaire],
                      "claude": self._claude_comptes()},
            critical_agents=(self.name,),
            critical_fallback_harnesses=("claude",))
        self.jauge("primaire", 10)       # sous son seuil, mais en panne
        self.jauge("secondaire", 96)     # au seuil
        self._report("codex", "primaire", "failing", source="turn")
        row = registry.get(self.db, self.name)
        with self._adapters():
            hold = account_turn.turn_hold(self.cfg, self.db, self.name, row)
            self.assertIn("no healthy codex account", hold)
            with self.assertRaises(work.WorkError) as ctx:
                work.check_assignee(self.db, self.name, cfg=self.cfg)
        self.assertIn("no healthy codex account", str(ctx.exception))

    # -- l'alerte de sous-utilisation non plus ----------------------------

    def test_alerte_de_sous_utilisation_ne_propose_pas_un_agent_en_tenue(self):
        # N1 de la R1 (#1021) : un agent encore `idle` dont les comptes
        # viennent de franchir le seuil (la pause n'est écrite qu'au sondage
        # suivant de son exécuteur) n'est pas proposé par l'alerte, ni par les
        # briefs d'orchestrateurs qui la reprennent.
        self.jauge("primaire", 96)
        self.jauge("secondaire", 96)
        self.db.execute(
            "UPDATE agent_registry SET status = 'idle', lease_owner = %s,"
            " lease_expires_at = now() + interval '1 hour',"
            " status_since = now() - interval '2 hours',"
            " last_turn_at = now() - interval '2 hours' WHERE name = %s",
            ("runner-%s@h" % self.name, self.name))
        work.add(self.db, title="travail en attente")
        out = _types(exploitation.alerts(self.cfg, self.db, now=time.time(),
                                         **_seuils()), "idle_capacity")
        self.assertEqual(out, [])
        # agent sain : l'alerte revient, et le nomme
        self.jauge("primaire", 10)
        self.jauge("secondaire", 10)
        out = _types(exploitation.alerts(self.cfg, self.db, now=time.time(),
                                         **_seuils()), "idle_capacity")
        self.assertEqual([a["agents"] for a in out], [[self.name]])

    # -- la prise automatique ne prend pas pour un agent en tenue ----------

    def test_auto_take_ne_prend_pas_pour_un_agent_en_tenue(self):
        self.jauge("primaire", 96)
        self.jauge("secondaire", 96)
        # au repos, sans lot, bail vivant : un candidat pour la prise
        self.db.execute(
            "UPDATE agent_registry SET status = 'idle', lease_owner = %s,"
            " lease_expires_at = now() + interval '1 hour', team = 'ameesh',"
            " status_since = now() - interval '2 hours',"
            " last_turn_at = now() - interval '2 hours' WHERE name = %s",
            ("runner-%s@h" % self.name, self.name))
        lot = self.lot(app="ameesh")
        self.db.execute("UPDATE work_items SET created_at = created_at"
                        " - interval '1 hour' WHERE id = %s", (lot["id"],))
        with mock.patch.object(backlog, "_default_budget_check",
                               return_value=lambda agent: ""):
            out = backlog.auto_take(self.cfg, self.db, now=time.time(),
                                    paid={"deepseek"}, orchestras=[])
        self.assertEqual(out, [])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

    def _adapters(self):
        return mock.patch.object(account_turn.adapters, "adapter_for",
                                 return_value=object())
