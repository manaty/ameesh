# SPDX-License-Identifier: AGPL-3.0-only
"""L95 — une page d'avancement et un budget justes.

* dépense payée au token et valeur consommée sur les forfaits séparées
  (instantané, texte, page, `cost report`) ;
* dépense réelle tirée des soldes, face à l'estimation, écart signalé ;
* modèle Codex lu dans le journal du fil, ou dans sa configuration ;
* jetons d'entrée et de cache relu, tours en échec à part ;
* toutes les fenêtres de tous les comptes déclarés ;
* correction tracée du grand livre (`ameesh cost correct`), essai par défaut.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import tempfile
import time
import unittest
import uuid

from ameesh import balance, cost, grand_livre, progress, storage

from .support import PgTestCase
from .test_l30_accounts import _Base
from .test_l74_comptes_a_echeance import _codex_deux_fenetres

NOW = 1_791_200_000.0
HEURE = 3600.0
JOUR = 86400.0


def _row(agent, harness, usd, **extra):
    row = {"agent": agent, "harness": harness, "model": extra.pop("model", ""),
           "usd_window": usd, "usd_1h": usd, "usd_24h": usd, "turns": 1,
           "failed_turns": 0, "input_tokens": 0, "cached_input_tokens": 0,
           "output_tokens": 0}
    row.update(extra)
    return row


def _rollout(root: str, thread: str, *, model: str | None = "gpt-test",
             totals: list | None = None) -> str:
    """Un journal de session Codex : un `turn_context` (modèle) puis, pour
    chaque tour, `task_started` et un `token_count` cumulé."""
    folder = os.path.join(root, "2026", "10", "04")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "rollout-2026-10-04T00-00-00-%s.jsonl" % thread)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n")
        for total in totals or []:
            fh.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}})
                     + "\n")
            if model:
                fh.write(json.dumps({"type": "turn_context",
                                     "payload": {"model": model, "effort": "low"}}) + "\n")
            fh.write(json.dumps({"type": "event_msg", "payload": {
                "type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": total[0], "cached_input_tokens": total[1],
                    "output_tokens": total[2]}}}}) + "\n")
        if model and not totals:
            fh.write(json.dumps({"type": "turn_context", "payload": {"model": model}}) + "\n")
    return path


# --------------------------------------------------------------------------
# 1, 4, 6 : instantané et rendus (sans base)
# --------------------------------------------------------------------------

class BudgetSepareTest(unittest.TestCase):

    def test_paye_et_forfaits_jamais_additionnes(self):
        rows = [_row("d1", "deepseek", 2.0), _row("c1", "claude", 70.0),
                _row("x1", "codex", 5.0)]
        b = progress.build_budget(rows, [], NOW, paid_harnesses=["deepseek"])
        self.assertEqual(b["spend"]["24h"]["paid_usd"], 2.0)
        self.assertEqual(b["spend"]["24h"]["plan_value_usd"], 75.0)
        text = progress.format_text(_snap_avec(b), width=100)
        self.assertIn("dépensé (payé au token, estimé) : 1 h 2.00", text)
        self.assertIn("valeur consommée sur les forfaits", text)
        self.assertNotIn("total estimé", text)

    def test_tours_en_echec_et_jetons(self):
        rows = [_row("codex1", "codex", 0.0, turns=5, failed_turns=5),
                _row("codex2", "codex", 1.0, turns=3, failed_turns=1,
                     input_tokens=1_500_000, cached_input_tokens=1_200_000,
                     output_tokens=4_000),
                _row("d1", "deepseek", 0.5, turns=2, input_tokens=100,
                     cached_input_tokens=9_000_000, output_tokens=300)]
        b = progress.build_budget(rows, [], NOW, paid_harnesses=["deepseek"])
        agents = {r["agent"]: r for r in b["by_agent"]}
        # codex1 : 5 tours à 0 $, tous en échec — il reste listé, sans travail
        self.assertEqual((agents["codex1"]["turns"], agents["codex1"]["failed_turns"]), (0, 5))
        self.assertEqual((agents["codex2"]["turns"], agents["codex2"]["failed_turns"]), (2, 1))
        # Codex compte le cache dans l'entrée : l'entrée fraîche est la différence
        self.assertEqual(agents["codex2"]["fresh_input_tokens"], 300_000)
        self.assertEqual(agents["d1"]["fresh_input_tokens"], 100)
        text = progress.format_text(_snap_avec(b), width=100)
        self.assertIn("codex1 (codex) : 0 tour(s) + 5 en échec", text)
        self.assertIn("cache relu 9,00 M", text)

    def test_page_colonnes_et_totaux(self):
        b = progress.build_budget([_row("d1", "deepseek", 1.0)], [], NOW,
                                  paid_harnesses=["deepseek"])
        html = progress.render_html(_snap_avec(b))
        for text in ("valeur consommée sur les forfaits", "dépensé (payé au token)",
                     '"cache relu"', '"entrée"', '"en échec"', "prochain choix",
                     "perdu à la remise à zéro"):
            self.assertIn(text, html)
        self.assertNotIn("total estimé", html)

    def test_comptes_toutes_fenetres(self):
        compte = {"harness": "claude", "account": "tertiaire", "active": False,
                  "forced": False, "ok": True, "reason": "", "next": True,
                  "why": "five_hour expire dans 40 min", "expires_in_s": 2400,
                  "gauges": [
                      {"key": "five_hour", "used": 0.1, "last_used": 0.1, "cap": 0.5,
                       "elapsed": 0.4, "resets_at": NOW + 2400, "reset_passed": False},
                      {"key": "seven_day", "used": 0.3, "last_used": 0.3, "cap": 0.9,
                       "elapsed": 0.8, "resets_at": NOW + 2 * JOUR, "reset_passed": False}],
                  "losses": [{"key": "five_hour", "resets_at": NOW + 2400, "in_s": 2400,
                              "lost": 0.9}], "_gauges": []}
        b = progress.build_budget([], [cost.Gauge("claude", "five_hour", 0.5, None, 18000)],
                                  NOW, paid_harnesses=[], accounts=[compte])
        acc = b["accounts"][0]
        self.assertEqual([g["key"] for g in acc["gauges"]], ["five_hour", "seven_day"])
        self.assertTrue(acc["next"])
        self.assertNotIn("_gauges", acc)
        text = progress.format_text(_snap_avec(b), width=100)
        self.assertIn("compte claude/tertiaire — prochain choix", text)
        self.assertIn("five_hour 90", text)
        # la jauge de la machine n'est pas répétée pour un harnais à comptes
        self.assertNotIn("forfait claude five_hour", text)


def _snap_avec(budget: dict) -> dict:
    return {"schema": progress.SCHEMA, "generated_ts": NOW, "generated_at": "x",
            "host": "h", "project": None, "window": {"from_ts": NOW - JOUR, "to_ts": NOW},
            "lots": [], "agents": [], "milestones": [], "epics": [], "actions": [],
            "budget": dict(budget, limits=None), "truncated": {}, "missing": []}


class CostReportTest(unittest.TestCase):

    def test_deux_totaux(self):
        rows = [{"agent": "d1", "harness": "deepseek", "model": "m", "paid": True,
                 "spent_1h": 0.5, "spent_24h": 2.0, "gauges": []},
                {"agent": "c1", "harness": "claude", "model": "m", "paid": False,
                 "spent_1h": 10.0, "spent_24h": 70.0, "gauges": []}]
        self.assertEqual(cost.totals(rows)["paid"], {"1h": 0.5, "24h": 2.0})
        self.assertEqual(cost.totals(rows)["plan_value"], {"1h": 10.0, "24h": 70.0})
        text = cost.format_report(rows)
        self.assertRegex(text, r"d1 +deepseek +m +token")
        self.assertRegex(text, r"c1 +claude +m +forfait")
        self.assertIn("dépensé (payé au token, estimé)  : 1 h 0.5000 $ · 24 h 2.0000 $", text)
        self.assertIn("valeur consommée sur les forfaits : 1 h 10.0000 $ · 24 h 70.0000 $", text)


# --------------------------------------------------------------------------
# 2 : dépense réelle (soldes)
# --------------------------------------------------------------------------

def _solde(total, ts, account=None, provider="deepseek", currency="USD"):
    return {"provider": provider, "currency": currency, "total": total,
            "observed_ts": ts, "account": account}


class DepenseReelleTest(unittest.TestCase):

    def test_baisses_recharges_et_depart(self):
        rows = [_solde(100.0, NOW - 2 * JOUR),        # avant la fenêtre : point de départ
                _solde(98.0, NOW - 3 * HEURE),
                _solde(110.0, NOW - 2 * HEURE),       # recharge de 12
                _solde(109.5, NOW - HEURE),
                _solde(50.0, NOW + HEURE)]            # après la fin : ignoré
        [entry] = balance.real_spend(rows, NOW - JOUR, NOW)
        self.assertEqual(entry["spent"], 2.5)
        self.assertEqual(entry["topups"], 12.0)
        self.assertEqual(entry["start_ts"], NOW - 2 * JOUR)
        self.assertEqual(entry["end_ts"], NOW - HEURE)

    def test_comptes_additionnes_par_fournisseur(self):
        rows = [_solde(10.0, NOW - 3 * HEURE, "a"), _solde(9.0, NOW - HEURE, "a"),
                _solde(20.0, NOW - 3 * HEURE, "b"), _solde(19.5, NOW - HEURE, "b")]
        [entry] = balance.real_spend(rows, NOW - JOUR, NOW)
        self.assertEqual(entry["spent"], 1.5)
        self.assertEqual(sorted(entry["accounts"]), ["a", "b"])

    def test_un_seul_releve_ne_dit_rien(self):
        self.assertEqual(balance.real_spend([_solde(10.0, NOW - HEURE)], NOW - JOUR, NOW), [])

    def test_ligne_texte(self):
        text = balance.describe({"provider": "deepseek", "currency": "USD", "period": "24h",
                                 "real_spent": 6.29, "estimated_usd": 12.03,
                                 "gap_usd": 5.74, "alert": True, "topups": 0.0})
        self.assertEqual(text, "dépense réelle deepseek 24 h (soldes) : 6.29 USD · estimée "
                               "12.03 $ · écart +5.74 $ — ÉCART AU-DELÀ DU SEUIL")


class DepenseReellePgTest(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        for table in ("turn_costs", "provider_balances", "turn_cost_corrections"):
            self.db.execute("DELETE FROM %s" % table)

    def _ligne(self, agent, harness, usd, ago_s, **extra):
        tokens = {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}
        tokens.update(extra)
        storage.of(self.db).turn_costs.insert(
            agent=agent, harness=harness, turn="t", model="m", session=None, usd=usd,
            cum_usd=None, cum_input_tokens=None, cum_cached_input_tokens=None,
            cum_output_tokens=None, **tokens)
        self.db.execute("UPDATE turn_costs SET recorded_at = now() - make_interval(secs => %s)"
                        " WHERE id = (SELECT max(id) FROM turn_costs)", (float(ago_s),))

    def _solde(self, total, ago_s):
        self.db.execute(
            "INSERT INTO provider_balances (provider, currency, total, observed_at)"
            " VALUES ('deepseek', 'USD', %s, now() - make_interval(secs => %s))",
            (total, float(ago_s)))

    def test_ecart_signale_et_lignes_ecartees(self):
        self._solde(100.0, 4 * HEURE)
        self._solde(99.0, 30 * 60)
        self._ligne("d1", "deepseek", 3.0, 2 * HEURE)
        self._ligne("d1", "deepseek", 0.5, 3 * HEURE)
        self._ligne("d1", "deepseek", 9.0, 5 * HEURE)          # avant le premier relevé
        self._ligne("c1", "claude", 50.0, 2 * HEURE)           # forfait : jamais comparé
        now = time.time()
        [entry] = balance.compare(self.db, ["deepseek"], now - JOUR, now)
        self.assertEqual(entry["real_spent"], 1.0)
        self.assertAlmostEqual(entry["estimated_usd"], 3.5)
        self.assertAlmostEqual(entry["gap_usd"], 2.5)
        self.assertTrue(entry["alert"])
        # une ligne écartée (0043) ne compte plus, ni ici ni dans la dépense
        self.db.execute("UPDATE turn_costs SET void_reason = 'doublon de #1' WHERE usd = 3.0")
        [entry] = balance.compare(self.db, ["deepseek"], now - JOUR, now)
        self.assertAlmostEqual(entry["estimated_usd"], 0.5)
        self.assertFalse(entry["alert"])                       # 0,50 $ : sous le seuil
        book = cost.CostBook(state_dir=self.state, db=self.db)
        self.assertAlmostEqual(book.spent("all", JOUR, harnesses=["deepseek"]), 9.5)

    def test_tours_en_echec_comptes_a_part(self):
        self._ligne("codex1", "codex", 0.0, 60, input_tokens=0, output_tokens=0)
        self._ligne("codex1", "codex", 0.0, 60, input_tokens=0, output_tokens=0)
        self._ligne("codex1", "codex", 0.2, 60)
        rows = storage.of(self.db).progress.costs(since_ts=time.time() - JOUR, agents=None)
        self.assertEqual((rows[0]["turns"], rows[0]["failed_turns"]), (3, 2))


# --------------------------------------------------------------------------
# 3 : modèle Codex
# --------------------------------------------------------------------------

class ModeleCodexTest(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l95-")
        self.home = os.path.join(self.tmp, "codex-home")
        self.sessions = os.path.join(self.home, "sessions")
        os.makedirs(self.sessions)

    def _book(self, **kw):
        return cost.CostBook(state_dir=os.path.join(self.tmp, "state"),
                             codex_sessions=self.sessions, db=None, **kw)

    def test_modele_lu_dans_le_journal_du_fil(self):
        thread = str(uuid.uuid4())
        _rollout(self.sessions, thread, model="gpt-6.1-sol", totals=[[10, 0, 1]])
        self.assertEqual(self._book().codex_thread_model(thread), "gpt-6.1-sol")

    def test_journal_d_un_compte_declare(self):
        autre = os.path.join(self.tmp, "codex-2")
        thread = str(uuid.uuid4())
        _rollout(os.path.join(autre, "sessions"), thread, model="gpt-compte")
        self.assertEqual(self._book().codex_thread_model(thread), "")
        self.assertEqual(self._book(codex_homes=[autre]).codex_thread_model(thread),
                         "gpt-compte")

    def test_repli_sur_la_configuration(self):
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write('# config\nmodel = "gpt-configure"\nmodel_reasoning_effort = "low"\n'
                     '[profiles.x]\nmodel = "autre"\n')
        thread = str(uuid.uuid4())
        _rollout(self.sessions, thread, model=None, totals=[[10, 0, 1]])
        self.assertEqual(self._book().codex_thread_model(thread), "gpt-configure")
        self.assertEqual(self._book().codex_thread_model("fil-absent"), "gpt-configure")

    def test_tour_codex_au_modele_du_fil(self):
        state = os.path.join(self.tmp, "state", "x1")
        os.makedirs(state)
        thread = str(uuid.uuid4())
        _rollout(self.sessions, thread, model="gpt-6.1-sol", totals=[[1000, 0, 10]])
        with open(os.path.join(state, "events.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "thread.started", "thread_id": thread}) + "\n")
            fh.write(json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 10}}) + "\n")
        usage = self._book(tools={"x1": "codex"}).turn_usage("x1", model="")
        self.assertEqual(usage.model, "gpt-6.1-sol")


# --------------------------------------------------------------------------
# 5 : comptes déclarés dans l'instantané
# --------------------------------------------------------------------------

class ComptesInstantaneTest(_Base):

    def test_toutes_les_fenetres_de_tous_les_comptes(self):
        comptes = self._codex_comptes()
        now = time.time()
        _codex_deux_fenetres(comptes[0]["path"], (10, now + 3 * HEURE), (20, now + 4 * JOUR))
        _codex_deux_fenetres(comptes[1]["path"], (0, now + 52 * 60), (5, now + 6 * JOUR))
        cfg = dataclasses.replace(self.cfg, accounts={"codex": comptes})
        book = cost.CostBook(state_dir=cfg.state_dir, db=self.db)
        snap = progress.snapshot(self.db, cfg, since="24h", book=book, now=now)
        accs = {a["account"]: a for a in snap["budget"]["accounts"]}
        self.assertEqual(set(accs), {"primaire", "secondaire"})
        for acc in accs.values():
            self.assertEqual([g["key"] for g in acc["gauges"]],
                             ["codex-300min", "codex-10080min"])
        self.assertTrue(accs["secondaire"]["next"])
        self.assertFalse(accs["primaire"]["next"])
        self.assertEqual(accs["secondaire"]["losses"][0]["key"], "codex-300min")
        self.assertAlmostEqual(accs["secondaire"]["losses"][0]["lost"], 1.0)
        html = progress.render_html(snap)
        self.assertIn('"account": "secondaire"', html)


# --------------------------------------------------------------------------
# 7 : correction du grand livre
# --------------------------------------------------------------------------

def _ligne(ident, agent, harness, usd, ts, **extra):
    row = {"id": ident, "agent": agent, "harness": harness, "turn": "messages",
           "model": "m", "session": None, "account": None, "usd": usd,
           "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
           "cum_usd": None, "cum_input_tokens": None, "cum_cached_input_tokens": None,
           "cum_output_tokens": None, "recorded_ts": ts, "void_reason": None}
    row.update(extra)
    return row


PRICES = {"deepseek-flash": [0.3, 0.006, 1.2], "deepseek-pro": [1.32, 0.044, 3.96],
          "gpt-6.1-sol": [1.25, 0.125, 10.0]}


class PlanCorrectionTest(unittest.TestCase):

    def _plan(self, rows, **kw):
        kw.setdefault("default_model_of", {"deepseek": "deepseek-flash"}.get)
        return {c["id"]: c for c in grand_livre.plan(rows, prices=PRICES, **kw)}

    def test_cumul_claude_reestime_par_les_autres_tours(self):
        s = "8fbfa9b3"
        rows = [_ligne(514, "claude1", "claude", 66.982122, NOW, session=s,
                       model="claude-x", input_tokens=6, cached_input_tokens=1_555_487,
                       output_tokens=892, cum_usd=66.982122),
                _ligne(518, "claude1", "claude", 0.181149, NOW + 60, session=s,
                       model="claude-x", input_tokens=2, cached_input_tokens=773_527,
                       output_tokens=303, cum_usd=67.163271)]
        plan = self._plan(rows)
        self.assertEqual(list(plan), [514])
        self.assertEqual(plan[514]["kind"], "cumul")
        self.assertAlmostEqual(plan[514]["after"]["usd"], 1_555_493 * 0.181149 / 773_529,
                               places=5)

    def test_premier_tour_legitime_non_touche(self):
        rows = [_ligne(28, "claude3", "claude", 3.17, NOW, session="s", model="claude-x",
                       input_tokens=72, cached_input_tokens=1_949_230, output_tokens=26_239,
                       cum_usd=3.17),
                _ligne(29, "claude3", "claude", 0.06, NOW + 9, session="s", model="claude-x",
                       input_tokens=2, cached_input_tokens=93_361, output_tokens=574,
                       cum_usd=3.23)]
        self.assertEqual(self._plan(rows), {})

    def test_cumul_codex_lu_dans_le_journal(self):
        tmp = tempfile.mkdtemp(prefix="ameesh-l95-")
        thread = str(uuid.uuid4())
        path = _rollout(tmp, thread, model="gpt-6.1-sol",
                        totals=[[100_000_000, 99_000_000, 400_000],
                                [101_000_000, 99_900_000, 401_000]])
        rows = [_ligne(523, "codex2", "codex", 22.7, NOW, session=thread, model="inconnu",
                       input_tokens=101_000_000, cached_input_tokens=99_900_000,
                       output_tokens=401_000, cum_input_tokens=101_000_000,
                       cum_cached_input_tokens=99_900_000, cum_output_tokens=401_000)]
        plan = self._plan(rows, codex_rollout=lambda t: path if t == thread else None,
                          codex_model=lambda t: "gpt-6.1-sol")
        fix = plan[523]
        self.assertEqual(fix["kind"], "cumul+modele")
        self.assertEqual(fix["after"]["tokens"], [1_000_000, 900_000, 1_000])
        self.assertEqual(fix["set"]["model"], "gpt-6.1-sol")
        self.assertAlmostEqual(fix["after"]["usd"],
                               (100_000 * 1.25 + 900_000 * 0.125 + 1_000 * 10.0) / 1e6)

    def test_cumul_codex_sans_journal_ecarte_si_fil_ancien(self):
        # UUID v7 d'un fil créé deux jours avant la ligne
        ms = int((NOW - 2 * JOUR) * 1000)
        thread = str(uuid.UUID(int=(ms << 80) | (0x7 << 76) | (0x2 << 62) | 1))
        rows = [_ligne(517, "codex3", "codex", 35.9, NOW, session=thread, model="gpt-6.1-sol",
                       input_tokens=206_000_000, cached_input_tokens=201_000_000,
                       output_tokens=516_000, cum_input_tokens=206_000_000,
                       cum_cached_input_tokens=201_000_000, cum_output_tokens=516_000)]
        fix = self._plan(rows)[517]
        self.assertTrue(fix["after"]["void"])
        self.assertIn("void_reason", fix["set"])
        # un fil neuf (créé juste avant) : la ligne est juste
        ms = int((NOW - 600) * 1000)
        rows[0]["session"] = str(uuid.UUID(int=(ms << 80) | (0x7 << 76) | (0x2 << 62) | 1))
        self.assertEqual(self._plan(rows), {})

    def test_doublons_et_modele_inconnu(self):
        tok = {"input_tokens": 88_245, "cached_input_tokens": 7_327_232,
               "output_tokens": 67_666}
        rows = [_ligne(557, "deepseek1", "deepseek", 0.706839, NOW, model="inconnu", **tok),
                _ligne(560, "deepseek1", "deepseek", 0.706839, NOW + 36, model="inconnu", **tok),
                # même usage, mais deux heures plus tard : un autre tour
                _ligne(700, "deepseek1", "deepseek", 0.706839, NOW + 7200, model="inconnu",
                       **tok),
                _ligne(701, "deepseek1", "deepseek", 0.1, NOW + 7300, model="deepseek-flash",
                       input_tokens=1)]
        plan = self._plan(rows)
        self.assertEqual(sorted(plan), [557, 560, 700])
        self.assertTrue(plan[560]["after"]["void"])
        self.assertEqual(plan[560]["kind"], "doublon")
        self.assertEqual(plan[557]["kind"], "modele")
        self.assertEqual(plan[557]["set"]["model"], "deepseek-flash")
        self.assertAlmostEqual(plan[557]["after"]["usd"],
                               (88_245 * 0.3 + 7_327_232 * 0.006 + 67_666 * 1.2) / 1e6, places=6)
        self.assertFalse(plan[700]["after"]["void"])

    def test_tour_en_echec_ni_doublon_ni_reprix(self):
        rows = [_ligne(i, "codex1", "codex", 0.0, NOW + i, model="gpt-6.1-sol")
                for i in range(5)]
        self.assertEqual(self._plan(rows), {})


class CorrectionPgTest(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        for table in ("turn_costs", "turn_cost_corrections"):
            self.db.execute("DELETE FROM %s" % table)

    def _insert(self, **row):
        base = dict(agent="deepseek1", harness="deepseek", turn="t", model="inconnu",
                    session=None, usd=0.706839, input_tokens=88_245,
                    cached_input_tokens=7_327_232, output_tokens=67_666, cum_usd=None,
                    cum_input_tokens=None, cum_cached_input_tokens=None,
                    cum_output_tokens=None)
        base.update(row)
        storage.of(self.db).turn_costs.insert(**base)

    def test_essai_puis_application_tracee(self):
        self._insert()
        self._insert()                                   # doublon de réparation
        self._insert(agent="claude1", harness="claude", model="claude-x", session="s1",
                     usd=66.98, input_tokens=6, cached_input_tokens=1_555_487,
                     output_tokens=892, cum_usd=66.98)
        self._insert(agent="claude1", harness="claude", model="claude-x", session="s1",
                     usd=0.18, input_tokens=2, cached_input_tokens=773_527,
                     output_tokens=303, cum_usd=67.16)
        prices = os.path.join(tempfile.mkdtemp(prefix="ameesh-l95-"), "prices.json")
        with open(prices, "w", encoding="utf-8") as fh:
            json.dump(PRICES, fh)
        avant = self.db.query("SELECT id, usd::float8 AS usd FROM turn_costs ORDER BY id")

        corrections, applied = grand_livre.run(self.db, prices_path=prices)
        self.assertIsNone(applied)
        self.assertEqual(len(corrections), 3)
        # l'essai n'a rien écrit
        self.assertEqual(self.db.query("SELECT id, usd::float8 AS usd FROM turn_costs"
                                       " ORDER BY id"), avant)
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM turn_cost_corrections")[0]["n"],
                         0)

        corrections, applied = grand_livre.run(self.db, apply=True, prices_path=prices,
                                               actor="human:test")
        self.assertEqual(applied, 3)
        rows = {r["id"]: r for r in self.db.query(
            "SELECT id, usd::float8 AS usd, model, void_reason FROM turn_costs")}
        self.assertEqual(len(rows), 4)                   # rien n'est supprimé
        ids = sorted(rows)
        self.assertEqual(rows[ids[0]]["model"], "deepseek-flash")
        self.assertTrue(rows[ids[1]]["void_reason"].startswith("doublon de #"))
        self.assertLess(rows[ids[2]]["usd"], 1.0)
        journal = self.db.query("SELECT turn_cost_id, kind, old_row, actor"
                                " FROM turn_cost_corrections ORDER BY id")
        self.assertEqual([j["kind"] for j in journal], ["modele", "doublon", "cumul"])
        old = journal[2]["old_row"]
        old = json.loads(old) if isinstance(old, str) else old
        self.assertAlmostEqual(float(old["usd"]), 66.98)
        self.assertEqual({j["actor"] for j in journal}, {"human:test"})
        # une passe rejouée ne trouve plus rien
        corrections, applied = grand_livre.run(self.db, apply=True, prices_path=prices)
        self.assertEqual((corrections, applied), ([], 0))
        # la ligne écartée ne compte plus dans la dépense
        book = cost.CostBook(state_dir=self.state, db=self.db)
        spent = book.spent("deepseek1", JOUR)
        self.assertAlmostEqual(spent, rows[ids[0]]["usd"])

    def test_cli_essai_par_defaut(self):
        self._insert()
        self._insert()
        proc = self.mesh("cost", "correct")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ESSAI : rien n'a été écrit", proc.stdout)
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM turn_costs"
                                       " WHERE void_reason IS NOT NULL")[0]["n"], 0)
        proc = self.mesh("cost", "correct", "--apply", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["applied"], 2)


if __name__ == "__main__":
    unittest.main()
