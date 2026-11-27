# SPDX-License-Identifier: AGPL-3.0-only
"""#8347 — le plafond horaire/journalier d'un avatar couvre la dépense de ses clones.

Un plafond posé sur un agent **racine** (non éphémère) compte la dépense
payée de la racine et de tous les éphémères dont le `created_by` y remonte ;
un plafond posé sur un éphémère reste son plafond propre. Le plafond du mesh
et le plafond propre continuent de s'appliquer (le plus strict gagne), un
agent au forfait n'est jamais mis en pause (L49), et une lignée rompue est
refusée plutôt que sommée partiellement.

La vue (`ameesh budget --json` / `GET /api/v1/budget`) publie `lineages` :
dépense propre de la racine, de chaque enfant vivant/échu, totaux 1 h/24 h ;
`ameesh cost lineage` groupe les coûts de tour par racine. Les deux lisent la
lignée en une requête et chaque fenêtre en une requête (pas de N+1).

Design cité par le demandeur : docs/design/5322-avatar-humain.md §3.6, Q6
(#213955, option a : l'enfant est payé par le budget/compte du parent).
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import time
import unittest
from unittest import mock

from ameesh import budget, cost, mesh_cli, registry, relay, storage
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase

PAID = ("deepseek",)


class LigneeBudgetTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM turn_costs")
        self.db.execute("DELETE FROM budget_limits")
        self.db.execute("DELETE FROM budget_events")

    # -- fixtures ----------------------------------------------------------
    def _agent(self, name: str, *, harness: str = "deepseek",
               creator: str | None = None, expires: str = "1 hour",
               status: str = "idle") -> None:
        registry.upsert(self.db, name, harness=harness, host="h1", cwd=self.tmp,
                        status=status)
        if creator is not None:
            self.db.execute(
                "UPDATE agent_registry SET ephemeral = true, created_by = %s,"
                " ephemeral_expires_at = now() + make_interval(secs => %s)"
                " WHERE name = %s",
                (creator, 3600.0 if expires == "1 hour" else 0.0, name))

    def _depense(self, agent: str, usd: float, *, harness: str = "deepseek",
                 il_y_a: str = "0 seconds") -> None:
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, model, usd, recorded_at) "
            "VALUES (%s, %s, 'm', %s, now() - %s::interval)",
            (agent, harness, usd, il_y_a))

    def _book(self, *, hourly: float = 100.0, daily: float = 0.0,
              agents: dict | None = None) -> cost.CostBook:
        return cost.CostBook(state_dir=self.state, db=self.db,
                             tools={}, hourly_usd=hourly, daily_usd=daily,
                             agent_limits=agents or {})

    def _cap(self, agent: str, usd: float, window: int = 3600) -> None:
        storage.of(self.db).budgets.put(agent, window, usd, actor="human:test")

    # -- garde -------------------------------------------------------------
    def test_plafond_de_lignee_somme_racine_et_clones(self):
        self._agent("r")
        self._agent("c1", creator="r")
        self._agent("c2", creator="r")
        self._depense("r", 2.0)
        self._depense("c1", 2.0)
        self._depense("c2", 2.0)
        self._cap("r", 5.0)
        book = self._book(agents={"r": {3600: 5.0}})
        for agent in ("r", "c1", "c2"):
            raison = book.over(agent, paid_harnesses=PAID, pace=False)
            self.assertIn("hourly budget of lineage r", raison, agent)
            self.assertIn("6.00 $", raison)
            self.assertIn("cap 5.00 $", raison)
        # la dépense d'un enfant hors lignée ne compte pas
        self._agent("autre")
        self._depense("autre", 10.0)
        self.assertIn("lineage r", book.over("r", paid_harnesses=PAID, pace=False))
        self.assertEqual(book.over("autre", paid_harnesses=PAID, pace=False), "")

    def test_enfant_au_plafond_propre_plus_strict(self):
        self._agent("r")
        self._agent("c1", creator="r")
        self._depense("r", 2.0)
        self._depense("c1", 1.5)
        self._cap("r", 10.0)
        self._cap("c1", 1.0)
        book = self._book(agents={"r": {3600: 10.0}, "c1": {3600: 1.0}})
        raison = book.over("c1", paid_harnesses=PAID, pace=False)
        self.assertIn("hourly budget of agent c1", raison)
        self.assertNotIn("lineage", raison)
        # le plafond de la racine (11.5 < 10 ? non : 3.5 < 10) laisse passer
        self.assertEqual(book.over("r", paid_harnesses=PAID, pace=False), "")

    def test_plafond_du_mesh_reste_le_plus_strict(self):
        self._agent("r")
        self._agent("c1", creator="r")
        self._depense("r", 1.0)
        self._depense("c1", 2.0)
        self._cap("r", 100.0)
        book = self._book(hourly=2.0, agents={"r": {3600: 100.0}})
        raison = book.over("c1", paid_harnesses=PAID, pace=False)
        self.assertIn("hourly budget (pay-per-token)", raison)
        self.assertNotIn("lineage", raison)

    def test_agent_au_forfait_jamais_en_pause(self):
        self._agent("r", harness="claude")
        self._agent("c1", harness="claude", creator="r")
        self._depense("r", 5.0, harness="claude")
        self._depense("c1", 5.0, harness="claude")
        book = self._book(agents={"r": {3600: 1.0}})
        for agent in ("r", "c1"):
            self.assertEqual(book.over(agent, paid_harnesses=PAID, pace=False), "")

    def test_sans_plafond_de_lignee_rien_ne_change(self):
        self._agent("r")
        self._agent("c1", creator="r")
        self._agent("autre")
        self._cap("autre", 1.0)
        self._depense("r", 50.0)
        self._depense("c1", 50.0)
        book = self._book(hourly=1000.0, agents={"autre": {3600: 1.0}})
        self.assertEqual(book.over("r", paid_harnesses=PAID, pace=False), "")
        self.assertEqual(book.over("c1", paid_harnesses=PAID, pace=False), "")

    def test_lignee_incomplete_refusee(self):
        self._agent("r")
        self._cap("r", 5.0)
        self._agent("orphelin")
        self.db.execute(
            "UPDATE agent_registry SET ephemeral = true, created_by = 'fantome',"
            " ephemeral_expires_at = now() + interval '1 hour' WHERE name = 'orphelin'")
        book = self._book(agents={"r": {3600: 5.0}})
        raison = book.over("orphelin", paid_harnesses=PAID, pace=False)
        self.assertIn("lineage budget", raison)
        self.assertIn("created_by lineage", raison)
        self.assertIn("no turn", raison)

    def test_lignee_dune_racine_parmi_dautres(self):
        # `lineages` rend aussi les autres racines semées par leurs enfants :
        # la lignée demandée est celle de SA racine, pas la première de l'ordre.
        self._agent("a")
        self._agent("a1", creator="a")
        self._agent("r")
        self._agent("c1", creator="r")
        data = storage.of(self.db).agents.lineage("c1")
        self.assertEqual(data["root"], "r")
        self.assertEqual({m["agent"] for m in data["members"]}, {"r", "c1"})
        self.assertEqual(storage.of(self.db).agents.lineage("r")["root"], "r")

    def test_pause_du_worker_nomme_la_racine(self):
        self._agent("r")
        self._agent("c1", creator="r")
        self._depense("r", 3.0)
        self._depense("c1", 3.0)
        self._cap("r", 5.0)
        cfg = dataclasses.replace(self.cfg, budget_usd_per_hour=100.0,
                                  budget_check_interval=0.0)
        runner = Runner(cfg, self.db, once=True)
        lease = registry.claim(self.db, "c1", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "c1"), lease)
        try:
            self.assertFalse(worker.budget_ok())
        finally:
            worker.watchdog_stop.set()
        ligne = registry.get(self.db, "c1")
        self.assertEqual(ligne["status"], "blocked")
        self.assertTrue(ligne["status_text"].startswith(budget.PAUSE_PREFIX))
        self.assertIn("lineage r", ligne["status_text"])

    def test_relais_compte_la_depense_non_ecrite_des_clones(self):
        # #8347 : le plafond de la racine couvre la lignée jusque dans la
        # dépense gardée en mémoire par le relais avant écriture.
        self._agent("r")
        self._agent("c1", creator="r")
        self._depense("r", 1.0)
        self._depense("c1", 1.0)
        book = self._book(hourly=10.0, agents={"r": {3600: 3.0}})
        ident = relay.Identity(executor="e", host="h", agent="r", lease_owner="e",
                               lease_epoch=1)
        stub = mock.Mock(_unrecorded=[{"agent": "c1", "usd": 2.0}])
        self.assertIn("budget de la lignée r atteint",
                      relay.Relay._over_with_unrecorded(stub, book, ident, PAID))
        # sans la dépense du clone, 2 $ restent sous le plafond de 3 $
        stub._unrecorded = []
        self.assertEqual(relay.Relay._over_with_unrecorded(stub, book, ident, PAID), "")

    # -- vue budget --------------------------------------------------------
    def test_budget_json_racine_enfants_et_totaux(self):
        self._agent("r")
        self._agent("c1", creator="r")
        self._agent("c2", creator="r")
        self.db.execute(
            "UPDATE agent_registry SET ephemeral_expires_at = now() - interval '1 hour'"
            " WHERE name = 'c2'")
        self._cap("r", 50.0)
        self._depense("r", 1.0)
        self._depense("c1", 2.0)
        self._depense("c2", 3.0, il_y_a="2 hours")
        data = budget.status(self.cfg, self.db)
        lignees = {entry["root"]: entry for entry in data["lineages"]}
        self.assertIn("r", lignees)
        lignee = lignees["r"]
        self.assertEqual(lignee["caps"], {"per_hour_usd": 50.0, "per_day_usd": None})
        self.assertEqual(lignee["own"], {"1h_usd": 1.0, "24h_usd": 1.0})
        enfants = {child["agent"]: child for child in lignee["children"]}
        self.assertEqual(set(enfants), {"c1", "c2"})
        self.assertEqual(enfants["c1"]["state"], "alive")
        self.assertEqual(enfants["c1"]["1h_usd"], 2.0)
        self.assertEqual(enfants["c1"]["24h_usd"], 2.0)
        self.assertEqual(enfants["c2"]["state"], "expired")
        self.assertEqual(enfants["c2"]["1h_usd"], 0.0)
        self.assertEqual(enfants["c2"]["24h_usd"], 3.0)
        self.assertEqual(lignee["total"], {"1h_usd": 3.0, "24h_usd": 6.0})
        self.assertIn("lineages", budget.format_status(data))

    def test_route_budget_sert_la_meme_vue(self):
        # `GET /api/v1/budget` appelle `budget.status` : la vue de lignée y est.
        from ameesh import serve
        self._agent("r")
        self._agent("c1", creator="r")
        self._cap("r", 5.0)
        self._depense("c1", 2.0)
        ctx = type("Ctx", (), {"cfg": self.cfg})()
        data = serve._budget(ctx, self.db, {}, {})
        self.assertEqual(data["lineages"][0]["root"], "r")
        self.assertEqual(data["lineages"][0]["total"]["1h_usd"], 2.0)

    # -- coût par lignée ---------------------------------------------------
    def test_cost_lineage_groupe_par_racine(self):
        self._agent("r")
        self._agent("c1", creator="r")
        self._agent("c2", creator="r")
        self._agent("x")
        self._depense("r", 1.0)
        self._depense("c1", 2.0)
        self._depense("c2", 0.5, il_y_a="2 hours")
        self._depense("x", 4.0)
        rows = storage.of(self.db).turn_costs.by_agent(time.time() - 86400, time.time())
        self.assertEqual({row["agent"]: round(row["usd"], 6) for row in rows},
                         {"r": 1.0, "c1": 2.0, "c2": 0.5, "x": 4.0})
        args = argparse.Namespace(since="24h", until=None, agent=None, json=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mesh_cli._cost_lineage(self.db, args), 0)
        data = json.loads(out.getvalue())
        lignees = {entry["root"]: entry for entry in data["lineages"]}
        self.assertEqual(lignees["r"]["usd"], 3.5)
        self.assertEqual(lignees["r"]["turns"], 3)
        self.assertEqual({a["agent"] for a in lignees["r"]["agents"]},
                         {"r", "c1", "c2"})
        self.assertEqual(lignees["x"]["usd"], 4.0)
        # --agent c1 résout la lignée de la racine
        args = argparse.Namespace(since="24h", until=None, agent="c1", json=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mesh_cli._cost_lineage(self.db, args), 0)
        data = json.loads(out.getvalue())
        self.assertEqual([entry["root"] for entry in data["lineages"]], ["r"])

    # -- pas de N+1 --------------------------------------------------------
    def test_vue_budget_sans_requete_par_enfant(self):
        self._agent("r")
        self._cap("r", 100.0)
        self._depense("r", 1.0)
        for index in range(2):
            self._agent("c%d" % index, creator="r")
            self._depense("c%d" % index, 1.0)

        original = self.db.query
        counted = []

        def capture(sql, *args, **kwargs):
            if "turn_costs" in str(sql):
                counted.append(str(sql))
            return original(sql, *args, **kwargs)

        def measure(call) -> int:
            counted.clear()
            self.db.query = capture
            try:
                call()
            finally:
                self.db.query = original
            return len(counted)

        limites = budget.current(self.cfg, self.db)
        book = self._book(hourly=100.0)
        avant = measure(lambda: budget._lineage_view(self.db, limites, book, PAID))
        avant_status = measure(lambda: budget.status(self.cfg, self.db))
        for index in range(2, 12):
            self._agent("c%d" % index, creator="r")
            self._depense("c%d" % index, 1.0)
        apres = measure(lambda: budget._lineage_view(self.db, limites, book, PAID))
        apres_status = measure(lambda: budget.status(self.cfg, self.db))
        self.assertEqual(avant, apres)
        self.assertEqual(avant_status, apres_status,
                         "la vue budget relit le grand livre par enfant : %d requêtes "
                         "pour 2 enfants, %d pour 12" % (avant_status, apres_status))
        # la vue de lignée ne fait que 2 lectures turn_costs : une somme par
        # fenêtre, quel que soit le nombre d'enfants.
        self.assertEqual(apres, 2,
                         "la vue de lignée doit lire 2 fois turn_costs (1 h, 24 h), "
                         "pas %d" % apres)
        self.assertLessEqual(apres_status, 20,
                             "trop de lectures turn_costs pour la vue : %d" % apres_status)

    # -- spawn -------------------------------------------------------------
    def test_avertissement_de_spawn(self):
        self._agent("r")
        self._agent("c0", creator="r")
        self._cap("r", 2.0)
        self._depense("r", 3.0)
        alerte = budget.lineage_warning(self.cfg, self.db, "r")
        self.assertIn("lineage r", alerte)
        self._agent("sans_plafond")
        self.assertEqual(budget.lineage_warning(self.cfg, self.db, "sans_plafond"), "",
                         "lignée de sans_plafond : %r ; agents : %r" % (
                             storage.of(self.db).agents.lineage("sans_plafond"),
                             [row["name"] for row in storage.of(self.db).agents.overview()]))
        # un créateur au forfait (L49) ne reçoit pas l'avertissement payé au token
        self._agent("claudeur", harness="claude")
        self._cap("claudeur", 1.0)
        self._depense("claudeur", 5.0, harness="claude")
        self.assertEqual(budget.lineage_warning(self.cfg, self.db, "claudeur"), "")


if __name__ == "__main__":
    unittest.main()
