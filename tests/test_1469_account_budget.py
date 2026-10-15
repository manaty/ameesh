# SPDX-License-Identifier: AGPL-3.0-only
"""#1469 (issue #1101) — shared daily USD envelope per provider billing account.

The seven acceptance tests of the issue: one envelope for every agent and
host; atomic admission under concurrency, providers independent; settlement
once, uncertain outcomes kept; calendar day in Europe/Paris with DST and
midnight; identity independent of key, profile name and host; no bypass by a
critical agent or failover; recovery, audited limit changes, visible pause.
"""
from __future__ import annotations

import dataclasses
import json
import os
import threading
from unittest import mock

from ameesh import account_budget, accounts, registry, storage
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase

OPENAI = "openai-manaty"
DEEPSEEK = "deepseek-manaty"


def _profile(billing, name="k1", harness="deepseek", reserve=2.0):
    return accounts.Profile(harness=harness, name=name, kind="api_key_env", env="K",
                            key_env="K", billing_account=billing, reserve_usd=reserve)


class AccountBudgetTest(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        for table in ("turn_costs", "account_reservations", "account_budgets",
                      "budget_events", "spend_pending"):
            self.db.execute("DELETE FROM %s" % table)
        self.api = storage.of(self.db).budgets
        self.api.account_put(OPENAI, 100.0, actor="human:t")
        self.api.account_put(DEEPSEEK, 100.0, actor="human:t")

    def _spend(self, agent, usd, *, billing=None, harness="deepseek", at="now()"):
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, model, usd, billing_account, recorded_at)"
            " VALUES (%s, %s, 'm', %s, %s, " + at + ")", (agent, harness, usd, billing))

    def _usage(self, account, at=None):
        return self.api.account_usage(account, at=at)[0]

    def _turn(self, db, agent, profile, cost, *, uncertain=False):
        """One full turn: reservation, ledger row, settlement."""
        self.assertEqual(account_budget.reserve(db, agent, profile), "")
        self._spend(agent, cost)  # the ledger row, untagged, as CostBook.record writes it
        account_budget.settle(db, agent, state="kept" if uncertain else "settled")

    # 1 ---------------------------------------------------------------------
    def test_1_un_compte_partage_entre_hotes_un_agent_de_plus_n_ajoute_rien(self):
        host_b = self.connect()
        try:
            self._turn(self.db, "a1", _profile(OPENAI, "cle-hote-a", "codex"), 40.0)
            self._turn(host_b, "a2", _profile(OPENAI, "cle-hote-b", "codex"), 50.0)
            u = self._usage(OPENAI)
            self.assertAlmostEqual(u["settled"], 90.0)
            # a third agent gets no allowance of its own: 90 + 2 > 100 is refused at 8.5
            self._spend("a1", 8.5, billing=OPENAI)
            raison = account_budget.reserve(host_b, "a3", _profile(OPENAI, "k3", "codex"))
            self.assertIn("account openai-manaty 98.50/100.00 $", raison)
        finally:
            host_b.close()

    # 2 ---------------------------------------------------------------------
    def test_2_admissions_concurrentes_pres_du_plafond(self):
        self._spend("x", 90.0, billing=OPENAI)
        results = []
        conns = [self.connect() for _ in range(8)]
        try:
            barrier = threading.Barrier(len(conns))

            def go(i, db):
                barrier.wait()
                results.append(account_budget.reserve(db, "c%d" % i, _profile(OPENAI)))

            threads = [threading.Thread(target=go, args=(i, c)) for i, c in enumerate(conns)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        finally:
            for c in conns:
                c.close()
        self.assertEqual(sum(1 for r in results if r == ""), 5)   # 90 + 5 × 2 = 100
        u = self._usage(OPENAI)
        self.assertAlmostEqual(u["reserved"], 10.0)
        self.assertLessEqual(u["settled"] + u["reserved"], 100.0)
        # the other provider is untouched
        d = self._usage(DEEPSEEK)
        self.assertEqual((d["settled"], d["reserved"]), (0.0, 0.0))
        self.assertEqual(account_budget.reserve(self.db, "d1", _profile(DEEPSEEK)), "")

    # 3 ---------------------------------------------------------------------
    def test_3_reglement_unique_et_issue_incertaine_conservee(self):
        codex_api = _profile(OPENAI, "codex-api", "codex")
        self._turn(self.db, "a1", codex_api, 1.5)
        # a duplicate settlement finds nothing open: counted once
        self.assertIsNone(account_budget.settle(self.db, "a1"))
        self.assertAlmostEqual(self._usage(OPENAI)["settled"], 1.5)
        # uncertain outcome: the 2 $ reservation stays counted with the cost
        self._turn(self.db, "a1", codex_api, 0.5, uncertain=True)
        u = self._usage(OPENAI)
        self.assertAlmostEqual((u["settled"], u["kept"]), (2.0, 2.0))
        # crash: an open reservation is never released by the next turn, it is kept
        self.assertEqual(account_budget.reserve(self.db, "a2", codex_api), "")
        self.assertEqual(account_budget.reserve(self.db, "a2", codex_api), "")
        u = self._usage(OPENAI)
        self.assertAlmostEqual((u["reserved"], u["kept"]), (2.0, 4.0))

    # 4 ---------------------------------------------------------------------
    def test_4_jour_calendaire_paris_dst_et_minuit(self):
        # 25 Oct 2026: back to winter time in Paris, a 25 h day, same cap
        u = self._usage(OPENAI, at="2026-10-25 12:00:00+01")
        self.assertEqual(u["period"], "2026-10-25")
        self.assertEqual(u["reset_ts"] - u["day_start_ts"], 25 * 3600)
        self.assertEqual(u["usd"], 100.0)
        # 23:30 Paris (21:30 UTC in summer) belongs to the day, 00:10 to the next one
        self._spend("a1", 7.0, billing=OPENAI, at="'2026-10-14 23:30:00+02'")
        self._spend("a1", 3.0, billing=OPENAI, at="'2026-10-15 00:10:00+02'")
        self.assertAlmostEqual(self._usage(OPENAI, at="2026-10-14 22:00:00+00")["settled"],
                               3.0)  # 22:00 UTC = 00:00 Paris on the 15th: only the 3 $
        self.assertAlmostEqual(self._usage(OPENAI, at="2026-10-14 21:00:00+00")["settled"],
                               7.0)
        self.assertAlmostEqual(self._usage(OPENAI, at="2026-10-15 10:00:00+02")["settled"],
                               3.0)
        # a reservation taken before midnight keeps counting while it is open
        self.db.execute("INSERT INTO account_reservations (billing_account, agent, usd, period,"
                        " created_at) VALUES (%s, 'a9', 2, current_date - 1,"
                        " now() - interval '1 day')", (OPENAI,))
        self.assertAlmostEqual(self._usage(OPENAI)["reserved"], 2.0)
        account_budget.settle(self.db, "a9")
        self.assertAlmostEqual(self._usage(OPENAI)["reserved"], 0.0)

    # 5 ---------------------------------------------------------------------
    def test_5_recreation_rotation_alias_migration_meme_historique(self):
        self._turn(self.db, "a1", _profile(DEEPSEEK, "cle-2025"), 10.0)
        registry_db = self.connect()   # another host, a new key, another profile name
        try:
            self._turn(registry_db, "a1", _profile(DEEPSEEK, "cle-2026-rotation"), 5.0)
            self._turn(registry_db, "a1-recree", _profile(DEEPSEEK, "alias"), 1.0)
        finally:
            registry_db.close()
        self.assertAlmostEqual(self._usage(DEEPSEEK)["settled"], 16.0)
        parse = accounts.parse({"deepseek": [
            {"name": "k1", "type": "api_key_env", "key_env": "A", "billing_account": DEEPSEEK},
            {"name": "k2", "type": "api_key_env", "key_env": "B", "billing_account": DEEPSEEK,
             "reserve_usd": 0.5}]})["deepseek"]
        self.assertEqual({p.billing_account for p in parse}, {DEEPSEEK})
        self.assertEqual(parse[1].reserve_usd, 0.5)
        with self.assertRaises(accounts.AccountError):
            accounts.parse({"deepseek": [{"name": "k", "type": "api_key_env", "key_env": "A",
                                          "billing_account": "Pas Valide"}]})

    # 6 ---------------------------------------------------------------------
    def test_6_ni_critique_ni_repli_ne_contournent_l_enveloppe(self):
        self._spend("x", 99.0, billing=DEEPSEEK)
        cfg = dataclasses.replace(
            self.cfg, budget_usd_per_hour=1000.0, budget_check_interval=0.0,
            critical_agents=("crit",), critical_fallback_harnesses=(),
            accounts={"deepseek": [{"name": "k1", "type": "api_key_env", "key_env": "DSK",
                                    "billing_account": DEEPSEEK,
                                    "check_credentials": False}]})
        runner = Runner(cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", "crit")
        os.makedirs(cwd, exist_ok=True)
        self.register("crit", "deepseek", cwd=cwd)
        lease = registry.claim(self.db, "crit", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "crit"), lease)
        try:
            with mock.patch.dict(os.environ, {"DSK": "sk-test"}):
                self.assertFalse(worker.budget_ok())
            ligne = registry.get(self.db, "crit")
            self.assertEqual(ligne["status"], "blocked")
            self.assertIn("account deepseek-manaty 99.00/100.00 $", ligne["status_text"])
            # the other provider keeps its own room
            self.assertEqual(account_budget.reserve(self.db, "o1", _profile(OPENAI)), "")
        finally:
            worker.watchdog_stop.set()

    # 7 ---------------------------------------------------------------------
    def test_7_cli_audit_liberation_et_etat_visible(self):
        env = self.env(AMEESH_HUMANS="chef", USER="chef")
        proc = self.mesh("budget", "set", "--account", OPENAI, "--per-day", "100",
                         env=self.env(AGENT_MAIL_NAME="malin"))
        self.assertEqual(proc.returncode, 1)
        proc = self.mesh("budget", "set", "--account", OPENAI, "--per-day", "120",
                         "--tz", "Europe/Paris", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("100.00 $ → 120.00 $", proc.stdout)
        ev = self.api.events(1)[0]
        self.assertEqual((ev["scope"], ev["old_usd"], ev["new_usd"], ev["actor"]),
                         ("account:" + OPENAI, 100.0, 120.0, "human:chef"))
        self._turn(self.db, "a1", _profile(OPENAI), 1.0, uncertain=True)
        kept = self.api.account_kept(OPENAI)[0]
        proc = self.mesh("budget", "--json", env=env)
        data = json.loads(proc.stdout)
        row = {a["billing_account"]: a for a in data["accounts"]}[OPENAI]
        self.assertEqual((row["usd"], row["settled"], row["kept"], row["remaining"]),
                         (120.0, 1.0, 2.0, 117.0))
        proc = self.mesh("budget", env=env)
        self.assertIn("openai-manaty", proc.stdout)
        self.assertIn("Europe/Paris, reset", proc.stdout)
        proc = self.mesh("budget", "release", str(kept["id"]), env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertAlmostEqual(self._usage(OPENAI)["kept"], 0.0)
        proc = self.mesh("budget", "unset", "--account", OPENAI, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([r["billing_account"] for r in self.api.account_usage()], [DEEPSEEK])

    # runner: reservation at launch, settlement at the end of the turn --------
    def test_tour_reel_reserve_puis_regle_et_refuse_a_epuisement(self):
        from .support import FAKEBIN
        cfg = dataclasses.replace(self.cfg, budget_check_interval=0.0)
        runner = Runner(cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", "e2e")
        os.makedirs(cwd, exist_ok=True)
        self.register("e2e", "claude", cwd=cwd)
        lease = registry.claim(self.db, "e2e", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "e2e"), lease)
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                worker._account = _profile(OPENAI, harness="claude")
                self.assertTrue(worker.run_turn({"kind": "prompt", "prompt": "t", "ids": []}))
                rows = self.db.query("select billing_account from turn_costs"
                                     " where agent = 'e2e'")
                self.assertEqual([r["billing_account"] for r in rows], [OPENAI])
                states = [r["state"] for r in self.db.query(
                    "select state from account_reservations where agent = 'e2e'")]
                self.assertEqual(states, ["settled"])
                self._spend("x", 99.5, billing=OPENAI)
                worker._account = _profile(OPENAI, harness="claude")
                self.assertFalse(worker.run_turn({"kind": "prompt", "prompt": "t", "ids": []}))
                self.assertEqual(self.db.query("select count(*)::int as n from turn_costs"
                                               " where agent = 'e2e'")[0]["n"], 1)
                ligne = registry.get(self.db, "e2e")
                self.assertIn("budget : account openai-manaty", ligne["status_text"])
        finally:
            worker.watchdog_stop.set()
