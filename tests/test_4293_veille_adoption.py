# SPDX-License-Identifier: AGPL-3.0-only
"""#4293 (#3264 C) : adoption d'une version de harnais ou de modèle.

La règle du §4 est déterministe ; les gestes sont simulés sur une base de test :
le canari nomme un agent, les mesures viennent de `turn_costs`, la bascule
pose `agent_registry.harness_bin` (harnais) ou `model` (modèle). Aucun binaire
n'est lancé ici : l'exécuteur est couvert par la résolution d'adaptateur.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import adapters, cost, registry, harness_adoption as veille, work
from ameesh.adapters import HarnessMissing

from .support import PgTestCase


def report(**overrides) -> dict:
    data = {
        "schema": veille.REPORT_SCHEMA,
        "evaluation_lot": 1,
        "harness": "deepseek",
        "object": "harness",
        "from_version": "0.2.0-rc.2",
        "to_version": "0.2.1-rc.4",
        "model": "deepseek-flash",
        "provider": "deepseek",
        "effort": "max",
        "conclusion": "equivalent",
        "quality": {"regression": False, "note": 7.0, "defects": []},
        "price": {
            "kind": "equal",
            "from": {"input": 0.30, "cached_input": 0.006, "output": 1.20},
            "to": {"input": 0.30, "cached_input": 0.006, "output": 1.20},
            "projected_7d_usd": 4.2,
        },
        "bounds": {"cost_usd": [0.005, 0.02], "duration_s": [5, 20]},
        "bench": [{"lot": 1, "class": "light"}],
    }
    data.update(overrides)
    return data


class Rules:
    def __init__(self, seconds: float):
        self.seconds = float(seconds)

    def next_cutoff(self, now: float) -> float:
        return float(now) + self.seconds


def b_report(**overrides) -> dict:
    """Une sortie minimale mais réelle de `ameesh.replay` (#4292)."""
    data = {
        "schema": "ameesh-replay-report/1",
        "harness": "deepseek",
        "evaluated": {
            "kind": "harness",
            "old": {"label": "0.2.0-rc.2", "version": "0.2.0-rc.2",
                    "binary": "/opt/old/dsh", "model": "deepseek-flash"},
            "new": {"label": "0.2.1-rc.4", "version": "0.2.1-rc.4",
                    "binary": "/opt/new/dsh", "model": "deepseek-flash"},
        },
        "constants": {"harness": "deepseek", "model": "deepseek-flash",
                      "effort": "max", "turn_cap_s": 1800},
        "model": "deepseek-flash", "effort": "max",
        "conclusion": "better",
        "lots": [{
            "lot": 1, "class": "light", "quality": "complete",
            "verdict": "better",
            "runs": {"old": {"cost_usd": 0.01, "duration_s": 10.0},
                     "new": {"cost_usd": 0.009, "duration_s": 9.0}},
            "reviews": {"old": {"score": 7.0}, "new": {"score": 8.0}},
        }],
        "report_path": "/opt/replay/report.json",
    }
    data.update(overrides)
    return data


class ReportRuleTest(unittest.TestCase):
    def test_regression_refuses_without_any_gesture(self):
        decision = veille.decide(veille.Report(report(conclusion="regression")))
        self.assertEqual(decision.action, "refused")

    def test_quality_regression_refuses(self):
        decision = veille.decide(veille.Report(
            report(quality={"regression": True, "note": 5.0})))
        self.assertEqual(decision.action, "refused")

    def test_equal_price_without_regression_is_automatic(self):
        decision = veille.decide(veille.Report(report()))
        self.assertEqual(decision.action, "automatic")
        self.assertEqual(decision.price, "equal")

    def test_higher_price_requires_the_owner(self):
        decision = veille.decide(veille.Report(
            report(price={"kind": "higher", "projected_7d_usd": 12.0,
                          "from": {"output": 1.0}, "to": {"output": 2.0}})))
        self.assertEqual(decision.action, "owner")
        self.assertEqual(decision.price, "higher")

    def test_equal_kind_with_a_higher_rate_is_not_trusted(self):
        decision = veille.decide(veille.Report(
            report(price={"kind": "equal", "projected_7d_usd": 1.0,
                          "from": {"output": 1.0}, "to": {"output": 3.0}})))
        self.assertEqual(decision.action, "owner")
        self.assertEqual(decision.price, "higher")

    def test_new_provider_and_plan_change_require_the_owner(self):
        for kind in ("new_provider", "plan_change"):
            decision = veille.decide(veille.Report(
                report(price={"kind": kind, "projected_7d_usd": 1.0})))
            self.assertEqual(decision.action, "owner", kind)

    def test_b_replay_model_report_uses_the_same_price_rule(self):
        raw = b_report(
            evaluated={"kind": "model",
                       "old": {"label": "deepseek-flash", "model": "deepseek-flash",
                               "binary": "/opt/dsh", "version": "0.2.0-rc.2"},
                       "new": {"label": "deepseek-pro", "model": "deepseek-pro",
                               "binary": "/opt/dsh", "version": "0.2.0-rc.2"}},
            constants={"harness": "deepseek", "model": None, "effort": "max"},
            conclusion="equal")
        report = veille.from_b_replay(raw, 7, projected_7d_usd=12.5)
        decision = veille.decide(report, prices=cost.DEFAULT_PRICES)
        self.assertEqual(decision.action, "owner")
        self.assertEqual(decision.price, "higher")
        self.assertEqual(report.evaluation_lot, 7)
        self.assertEqual(report.to_version, "deepseek-pro")

    def test_b_replay_harness_report_derives_canary_bounds(self):
        report = veille.from_b_replay(b_report(), 7)
        self.assertEqual(report.conclusion, "better")
        self.assertEqual(report.from_version, "0.2.0-rc.2")
        self.assertEqual(report.new_binary, "/opt/new/dsh")
        self.assertAlmostEqual(report.bounds["cost_usd"][0], 0.009)
        self.assertAlmostEqual(report.bounds["cost_usd"][1], 0.011)
        self.assertEqual(report.bounds["duration_s"], [9.0, 11.0])
        # la projection 7 jours est dérivée : elle ne change pas l'identité
        self.assertEqual(veille.from_b_replay(b_report(), 7,
                                              projected_7d_usd=1.0).digest,
                         veille.from_b_replay(b_report(), 7,
                                              projected_7d_usd=2.0).digest)

    def test_a_more_expensive_model_from_the_schedule_is_not_equal(self):
        """deepseek-flash -> deepseek-pro : le barème de cost.py tranche,
        même si le rapport écrit « equal » et des tarifs égaux."""
        data = report(object="model", from_version="deepseek-flash",
                      to_version="deepseek-pro")
        decision = veille.decide(veille.Report(data), prices=cost.DEFAULT_PRICES)
        self.assertEqual(decision.action, "owner")
        self.assertEqual(decision.price, "higher")

    def test_an_unknown_model_price_requires_the_owner(self):
        data = report(object="model", from_version="model-inconnu-a",
                      to_version="model-inconnu-b",
                      price={"kind": "equal", "projected_7d_usd": 1.0})
        decision = veille.decide(veille.Report(data), prices=cost.DEFAULT_PRICES)
        self.assertEqual(decision.action, "owner")
        self.assertEqual(decision.price, "unknown")
        # avis auditeur e7a7 : le barème ignore le modèle cible, le rapport
        # donne des tarifs COMPLETS et égaux — ce n'est pas « égal » pour
        # autant : le barème du poste fait foi.
        rates = {"input": 0.30, "cached_input": 0.006, "output": 1.20}
        half_known = report(
            object="model", from_version="deepseek-flash",
            to_version="modele-absent-du-bareme",
            price={"kind": "equal", "projected_7d_usd": 1.0,
                   "from": dict(rates), "to": dict(rates)})
        decision = veille.decide(veille.Report(half_known),
                                 prices=cost.DEFAULT_PRICES)
        self.assertEqual(decision.action, "owner")
        self.assertEqual(decision.price, "unknown")

    def test_disjoint_rates_are_not_equal(self):
        data = report(price={"kind": "equal", "projected_7d_usd": 1.0,
                             "from": {"input": 0.3},
                             "to": {"output": 1.2}})
        decision = veille.decide(veille.Report(data), prices=cost.DEFAULT_PRICES)
        # le modèle (deepseek-flash) reste connu et égal ; les tarifs
        # incomplets ne changent donc pas la décision pour un harnais
        self.assertEqual(decision.action, "automatic")
        unknown = report(object="model", from_version="model-inconnu-a",
                         to_version="model-inconnu-b",
                         price={"kind": "equal", "projected_7d_usd": 1.0,
                                "from": {"input": 0.3},
                                "to": {"output": 1.2}})
        decision = veille.decide(veille.Report(unknown), prices=cost.DEFAULT_PRICES)
        self.assertEqual(decision.action, "owner")
        self.assertEqual(decision.price, "unknown")

    def test_owner_decision_needs_the_seven_day_projection(self):
        decision = veille.decide(veille.Report(
            report(price={"kind": "higher", "from": {"output": 1.0},
                          "to": {"output": 2.0}})))
        self.assertEqual(decision.action, "blocked")
        self.assertIn("projected_7d_usd", decision.reason)

    def test_canary_verdict_uses_the_replay_bounds(self):
        parsed = veille.Report(report())
        ok, why = veille.canary_verdict(
            parsed, {"turns": 10, "cost_usd": 0.01, "duration_s": 12.0})
        self.assertTrue(ok, why)
        ok, why = veille.canary_verdict(
            parsed, {"turns": 10, "cost_usd": 0.5, "duration_s": 12.0})
        self.assertFalse(ok)
        self.assertIn("cost_usd", why[0])

    def test_report_round_trips_from_note_and_fence(self):
        parsed = veille.parse_report("veille-report: " + json.dumps(report()))
        self.assertEqual(parsed.to_version, "0.2.1-rc.4")
        parsed = veille.parse_report(
            "note\n```json\n" + json.dumps(report()) + "\n```\n")
        self.assertEqual(parsed.harness, "deepseek")

    def test_report_refuses_a_wrong_schema_or_a_missing_evaluation_lot(self):
        with self.assertRaises(veille.VeilleError):
            veille.Report(report(schema="other/1"))
        with self.assertRaises(veille.VeilleError):
            veille.Report(report(evaluation_lot=0))

    def test_identical_versions_are_refused(self):
        with self.assertRaises(veille.VeilleError):
            veille.Report(report(to_version="0.2.0-rc.2"))

    def test_bounds_must_carry_a_measurable_metric(self):
        with self.assertRaises(veille.VeilleError):
            veille.Report(report(bounds={"quality_note": [6, 8]}))


class AdoptionTest(PgTestCase):
    def setUp(self):
        super().setUp()
        # #4312 : les binaires de test vivent dans le bac à sable ; déclarer
        # ce préfixe est le geste explicite que la production demande.
        prefixes = mock.patch.dict(os.environ, {veille.BINS_PREFIXES_ENV: self.tmp})
        prefixes.start()
        self.addCleanup(prefixes.stop)
        for name, host in (("canary", "h1"), ("impl", "h1"), ("other", "h2")):
            registry.upsert(
                self.db, name, chantier="ameesh", harness="deepseek", host=host,
                cwd=self.tmp, session_id=None, status="idle", status_text="",
                model="deepseek-flash", budget_usd=None)
        self.old_bin = os.path.join(self.tmp, "old-dsh")
        self.new_bin = os.path.join(self.tmp, "new-dsh")
        for path in (self.old_bin, self.new_bin):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(path, 0o755)
        self.db.execute(
            "INSERT INTO harness_versions (host, harness, path, version) VALUES"
            " ('h1', 'deepseek', %s, '0.2.0-rc.2'),"
            " ('h2', 'deepseek', %s, '0.2.0-rc.2')",
            (self.old_bin, self.old_bin))
        self.rules = Rules(30 * 60)
        self.item = work.add(self.db, title="Évaluer dsh 0.2.1-rc.4", app="ameesh",
                             assignee="canary")
        self.lot = int(self.item["id"])
        self.bins = {"h1": self.new_bin, "h2": self.new_bin}

    def _report(self, **over):
        data = report(**over)
        data["evaluation_lot"] = self.lot
        data.setdefault("old_binary", self.old_bin)
        data.setdefault("new_binary", self.new_bin)
        return veille.Report(data)

    def _turn(self, agent, usd, duration, at):
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, turn, model, session, usd,"
            " input_tokens, cached_input_tokens, output_tokens, started_at,"
            " recorded_at) VALUES (%s, 'deepseek', 't', 'm', 's', %s, 0, 0, 0,"
            " to_timestamp(%s), to_timestamp(%s))",
            (agent, usd, float(at), float(at) + float(duration)))

    def _bins(self):
        return {name: registry.get(self.db, name)["harness_bin"]
                for name in ("canary", "impl", "other")}

    def _b_note(self, path, first=None):
        """La note de B (#4292) avec l'empreinte du fichier (#4312)."""
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        return ("%s\nBanc : #1 (light) ; coût, durée, tests du lot et note "
                "aveugle dans le rapport.\nRapport : %s\nEmpreinte : %s"
                % (first or "Rejeu comparatif deepseek 0.2.0-rc.2 -> "
                             "0.2.1-rc.4 : meilleure.", path, digest))

    def _owner_adoption(self, bins=None):
        """Une adoption plus chère : décision owner #91 en attente, canari."""
        higher = self._report(price={
            "kind": "higher", "projected_7d_usd": 12.5,
            "from": {"output": 1.20}, "to": {"output": 1.60}})
        with mock.patch.object(veille.decisions, "ask",
                               return_value={"id": 91, "state": "pending"}):
            veille.adopt(self.cfg, self.db, self.lot, higher,
                         bins=self.bins if bins is None else bins,
                         now=1000.0, rules=self.rules)

    def test_automatic_adoption_goes_canary_then_host_by_host(self):
        result = veille.adopt(self.cfg, self.db, self.lot, self._report(),
                              bins=self.bins, now=1000.0, rules=self.rules)
        self.assertEqual(result["result"], "canary")
        self.assertEqual(self._bins(), {
            "canary": self.bins["h1"], "impl": None, "other": None})
        state = veille.load(self.db, self.lot)["state"]
        self.assertEqual(state["stage"], "canary")
        self.assertEqual(state["target_agent"], "canary")

        # pas encore de mesure : on attend (24 h pas écoulées)
        out = veille.tick(self.cfg, self.db, now=1000.0 + 3600,
                          rules=Rules(30 * 60))
        self.assertEqual(out["adoptions"][0]["result"], "canary")
        for _ in range(10):
            self._turn("canary", 0.01, 10.0, 1000.0 + 4000)
        out = veille.tick(self.cfg, self.db, now=1000.0 + 5000,
                          rules=Rules(30 * 60))
        self.assertEqual(out["adoptions"][0]["result"], "rollout")
        self.assertEqual(veille.load(self.db, self.lot)["state"]["stage"], "rollout")

        # hôte h1 : le canari y est déjà, les autres agents passent aussi
        out = veille.tick(self.cfg, self.db, now=1000.0 + 5100,
                          rules=Rules(30 * 60))
        self.assertEqual(out["adoptions"][0]["result"], "rollout")
        self.assertEqual(self._bins()["impl"], self.bins["h1"])
        self.assertIsNone(self._bins()["other"])
        # hôte h2 : une coupure est trop proche, rien ne bouge
        out = veille.tick(self.cfg, self.db, now=1000.0 + 5200,
                          rules=Rules(10 * 60))
        self.assertEqual(out["adoptions"][0]["result"], "waiting_cutoff")
        self.assertIsNone(self._bins()["other"])
        # puis entre deux tours, hors marge
        out = veille.tick(self.cfg, self.db, now=1000.0 + 5300,
                          rules=Rules(30 * 60))
        self.assertEqual(out["adoptions"][0]["result"], "rollout")
        self.assertEqual(self._bins()["other"], self.bins["h2"])
        out = veille.tick(self.cfg, self.db, now=1000.0 + 5400,
                          rules=Rules(30 * 60))
        self.assertEqual(out["adoptions"][0]["result"], "done")
        self.assertEqual(veille.load(self.db, self.lot)["state"]["stage"], "done")

    def test_a_running_turn_holds_the_host_switch(self):
        veille.adopt(self.cfg, self.db, self.lot, self._report(), bins=self.bins,
                     now=1000.0, rules=self.rules)
        for _ in range(10):
            self._turn("canary", 0.01, 10.0, 1000.0 + 4000)
        veille.tick(self.cfg, self.db, now=1000.0 + 5000, rules=Rules(30 * 60))
        self.db.execute("UPDATE agent_registry SET status = 'running' "
                        "WHERE name = 'impl'")
        out = veille.tick(self.cfg, self.db, now=1000.0 + 5100,
                          rules=Rules(30 * 60))
        self.assertEqual(out["adoptions"][0]["result"], "waiting_turn")
        self.assertIsNone(self._bins()["impl"])

    def test_canary_waits_for_a_cutoff_and_resumes(self):
        """La garde des 20 minutes couvre le démarrage du canari aussi."""
        blocked = veille.adopt(self.cfg, self.db, self.lot, self._report(),
                               bins=self.bins, now=1000.0,
                               rules=Rules(5 * 60))
        self.assertEqual(blocked["result"], "blocked")
        self.assertEqual(self._bins(), {"canary": None, "impl": None, "other": None})
        out = veille.tick(self.cfg, self.db, now=1000.0 + 60, bins=self.bins,
                          rules=Rules(30 * 60))
        self.assertEqual(out["adoptions"][0]["result"], "canary")
        self.assertEqual(self._bins()["canary"], self.bins["h1"])

    def test_inherited_model_follows_the_runner_precedence(self):
        """#4312 (N de l'auditeur sur #4293) : `model` NULL suit la précédence
        du runner — état local de l'agent, puis défaut du descripteur. Un
        agent distant (état illisible) est exclu, jamais supposé au défaut du
        descripteur lu sur l'hôte du tick."""
        self.db.execute("UPDATE agent_registry SET model = NULL "
                        "WHERE name IN ('canary', 'impl', 'other')")
        flash = self._report(object="model", from_version="deepseek-flash",
                             to_version="model-new")
        self.assertEqual(veille._target_agents(self.db, flash, cfg=self.cfg), [],
                         "un agent distant sans état lisible n'est pas une cible")
        # l'agent local, lui, est résolu comme le runner : sans fichier
        # d'état, le défaut du descripteur (deepseek-flash) s'applique.
        self.db.execute("UPDATE agent_registry SET host = %s WHERE name = 'canary'",
                        (self.cfg.host,))
        self.assertEqual(
            {row["name"] for row in veille._target_agents(self.db, flash, cfg=self.cfg)},
            {"canary"})
        # un fichier d'état local différent du défaut l'emporte.
        local_dir = self.cfg.agent_dir("canary")
        os.makedirs(local_dir, exist_ok=True)
        state_model = os.path.join(local_dir, "model")
        with open(state_model, "w", encoding="utf-8") as fh:
            fh.write("model-old\n")
        self.assertEqual(veille._target_agents(self.db, flash, cfg=self.cfg), [])
        # et celui qui vaut from_version le rend cible.
        with open(state_model, "w", encoding="utf-8") as fh:
            fh.write("deepseek-flash\n")
        self.assertEqual(
            {row["name"] for row in veille._target_agents(self.db, flash, cfg=self.cfg)},
            {"canary"})

    def test_canary_regression_restores_the_previous_version(self):
        veille.adopt(self.cfg, self.db, self.lot, self._report(), bins=self.bins,
                     now=1000.0, rules=self.rules)
        for _ in range(10):
            self._turn("canary", 0.5, 10.0, 1000.0 + 4000)
        out = veille.tick(self.cfg, self.db, now=1000.0 + 5000,
                          rules=Rules(30 * 60))
        self.assertEqual(out["adoptions"][0]["result"], "refused")
        self.assertIsNone(self._bins()["canary"])
        state = veille.load(self.db, self.lot)["state"]
        self.assertEqual(state["stage"], "refused")
        self.assertEqual(state["inverse"]["version"], "0.2.0-rc.2")
        self.assertIn("cost_usd", state["reason"])

    def test_canary_with_one_turn_at_the_deadline_is_refused(self):
        """#4312 (N de l'auditeur sur #4293) : à 24 h, un seul tour ne fait
        pas un canari concluant ; l'agent est remis sur l'ancienne version."""
        veille.adopt(self.cfg, self.db, self.lot, self._report(), bins=self.bins,
                     now=1000.0, rules=self.rules)
        self._turn("canary", 0.01, 10.0, 1000.0 + 4000)
        out = veille.tick(
            self.cfg, self.db,
            now=1000.0 + veille.CANARY_SECONDS + 1, rules=Rules(30 * 60))
        self.assertEqual(out["adoptions"][0]["result"], "refused")
        self.assertIn("turn", out["adoptions"][0]["reason"])
        self.assertIsNone(self._bins()["canary"])
        state = veille.load(self.db, self.lot)["state"]
        self.assertEqual(state["stage"], "refused")
        self.assertEqual(state["inverse"]["version"], "0.2.0-rc.2")

    def test_report_regression_changes_no_host(self):
        result = veille.adopt(self.cfg, self.db, self.lot,
                              self._report(conclusion="regression"),
                              bins=self.bins, now=1000.0, rules=self.rules)
        self.assertEqual(result["result"], "refused")
        self.assertEqual(self._bins(), {"canary": None, "impl": None, "other": None})
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM harness_adoptions")[0]["n"], 1)

    def test_a_second_report_on_the_same_lot_is_refused(self):
        veille.adopt(self.cfg, self.db, self.lot, self._report(), bins=self.bins,
                     now=1000.0, rules=self.rules)
        second = self._report(to_version="0.2.2-rc.1")
        with self.assertRaises(veille.VeilleError):
            veille.adopt(self.cfg, self.db, self.lot, second, bins=self.bins,
                         now=1000.0, rules=self.rules)

    def test_higher_price_opens_the_owner_decision_without_a_gesture(self):
        higher = self._report(price={
            "kind": "higher", "projected_7d_usd": 12.5,
            "from": {"input": 0.30, "output": 1.20},
            "to": {"input": 0.40, "output": 1.60}})
        with mock.patch.object(veille.decisions, "ask",
                               return_value={"id": 91, "state": "pending"}) as ask:
            result = veille.adopt(self.cfg, self.db, self.lot, higher,
                                  bins=self.bins, now=1000.0, rules=self.rules)
        self.assertEqual(result["result"], "waiting_owner")
        self.assertEqual(self._bins(), {"canary": None, "impl": None, "other": None})
        question = ask.call_args.kwargs["question"]
        self.assertIn("12.50", question)
        self.assertEqual(ask.call_args.kwargs["recommend"], "adopt")
        self.assertTrue(ask.call_args.kwargs["needs_signature"])
        state = veille.load(self.db, self.lot)["state"]
        self.assertEqual(state["decision_id"], 91)

    def test_owner_approval_starts_the_canary_owner_refusal_changes_nothing(self):
        higher = self._report(price={
            "kind": "higher", "projected_7d_usd": 12.5,
            "from": {"output": 1.20}, "to": {"output": 1.60}})
        with mock.patch.object(veille.decisions, "ask",
                               return_value={"id": 91, "state": "pending"}):
            veille.adopt(self.cfg, self.db, self.lot, higher, bins=self.bins,
                         now=1000.0, rules=self.rules)
        with mock.patch.object(veille.decisions, "get",
                               return_value={"id": 91, "state": "answered",
                                             "answer": {"option": "refuse"}}):
            out = veille.tick(self.cfg, self.db, now=1001.0, rules=Rules(1800))
        self.assertEqual(out["adoptions"][0]["result"], "refused")
        self.assertEqual(self._bins(), {"canary": None, "impl": None, "other": None})

    def test_owner_decision_closed_without_an_answer_says_so(self):
        """N dev1 du 29/10 sur #4601 : une enveloppe close sans réponse n'est
        pas un refus de l'owner ; le motif doit le dire, sans « free text »."""
        self._owner_adoption()
        with mock.patch.object(veille.decisions, "get",
                               return_value={"id": 91, "state": "closed"}):
            out = veille.tick(self.cfg, self.db, now=1001.0, rules=Rules(1800))
        self.assertEqual(out["adoptions"][0]["result"], "refused")
        self.assertIn("closed without an answer or withdrawal",
                      out["adoptions"][0]["reason"])
        self.assertNotIn("did not approve", out["adoptions"][0]["reason"])
        self.assertEqual(self._bins(), {"canary": None, "impl": None, "other": None})
        self.assertEqual(veille.load(self.db, self.lot)["state"]["stage"], "refused")

    def test_owner_approval_starts_the_canary(self):
        higher = self._report(price={
            "kind": "higher", "projected_7d_usd": 12.5,
            "from": {"output": 1.20}, "to": {"output": 1.60}})
        with mock.patch.object(veille.decisions, "ask",
                               return_value={"id": 91, "state": "pending"}):
            veille.adopt(self.cfg, self.db, self.lot, higher, bins=self.bins,
                         now=1000.0, rules=self.rules)
        with mock.patch.object(veille.decisions, "get",
                               return_value={"id": 91, "state": "answered",
                                             "answer": {"option": "adopt"}}), \
             mock.patch.object(veille.decisions, "verify_proof",
                               return_value=(True, "signed owner key")):
            out = veille.tick(self.cfg, self.db, now=1001.0, rules=Rules(1800))
        self.assertEqual(out["adoptions"][0]["result"], "canary")
        self.assertEqual(self._bins()["canary"], self.bins["h1"])

    def test_owner_proof_revoked_after_canary_refuses_before_any_host(self):
        """Avis B cx1 17:05 : la preuve est revérifiée à chaque hôte. Révoquée
        après le canari, elle arrête la bascule sans toucher un autre agent."""
        self._owner_adoption()
        answered = {"id": 91, "state": "answered", "answer": {"option": "adopt"}}
        with mock.patch.object(veille.decisions, "get", return_value=answered), \
             mock.patch.object(veille.decisions, "verify_proof",
                               return_value=(True, "signed owner key")) as proof:
            out = veille.tick(self.cfg, self.db, now=1001.0, rules=Rules(1800))
            self.assertEqual(out["adoptions"][0]["result"], "canary")
            for _ in range(10):
                self._turn("canary", 0.01, 10.0, 1000.0 + 4000)
            out = veille.tick(self.cfg, self.db, now=1000.0 + 5000,
                              rules=Rules(30 * 60))
            self.assertEqual(out["adoptions"][0]["result"], "rollout")
            proof.return_value = (False, "key of owner revoked since")
            out = veille.tick(self.cfg, self.db, now=1000.0 + 5100,
                              rules=Rules(30 * 60))
        self.assertEqual(out["adoptions"][0]["result"], "refused")
        self.assertIn("proof", out["adoptions"][0]["reason"])
        self.assertEqual(self._bins(), {"canary": None, "impl": None,
                                        "other": None})
        self.assertEqual(veille.load(self.db, self.lot)["state"]["stage"],
                         "refused")

    def test_owner_proof_revoked_between_hosts_restores_the_first_host(self):
        """Avis B cx1 17:05 : révoquée entre deux hôtes, la preuve arrête la
        suite et rend aux agents déjà basculés leur valeur d'avant."""
        self._owner_adoption()
        answered = {"id": 91, "state": "answered", "answer": {"option": "adopt"}}
        with mock.patch.object(veille.decisions, "get", return_value=answered), \
             mock.patch.object(veille.decisions, "verify_proof",
                               return_value=(True, "signed owner key")) as proof:
            veille.tick(self.cfg, self.db, now=1001.0, rules=Rules(1800))
            for _ in range(10):
                self._turn("canary", 0.01, 10.0, 1000.0 + 4000)
            veille.tick(self.cfg, self.db, now=1000.0 + 5000,
                        rules=Rules(30 * 60))
            out = veille.tick(self.cfg, self.db, now=1000.0 + 5100,
                              rules=Rules(30 * 60))
            self.assertEqual(out["adoptions"][0]["result"], "rollout")
            self.assertEqual(self._bins()["impl"], self.bins["h1"])
            self.assertIsNone(self._bins()["other"])
            proof.return_value = (False, "approval expired")
            out = veille.tick(self.cfg, self.db, now=1000.0 + 5200,
                              rules=Rules(30 * 60))
        self.assertEqual(out["adoptions"][0]["result"], "refused")
        self.assertIn("proof", out["adoptions"][0]["reason"])
        self.assertEqual(self._bins(), {"canary": None, "impl": None,
                                        "other": None})

    def test_blocked_rollout_resume_rechecks_the_owner_proof(self):
        """Avis B cx1 17:05 : la reprise d'une bascule bloquée repasse par la
        preuve ; sans elle, elle refuse et restaure l'hôte déjà fait."""
        self._owner_adoption(bins={"h1": self.new_bin})
        answered = {"id": 91, "state": "answered", "answer": {"option": "adopt"}}
        with mock.patch.object(veille.decisions, "get", return_value=answered), \
             mock.patch.object(veille.decisions, "verify_proof",
                               return_value=(True, "signed owner key")) as proof:
            veille.tick(self.cfg, self.db, now=1001.0, rules=Rules(1800))
            for _ in range(10):
                self._turn("canary", 0.01, 10.0, 1000.0 + 4000)
            veille.tick(self.cfg, self.db, now=1000.0 + 5000,
                        rules=Rules(30 * 60))
            veille.tick(self.cfg, self.db, now=1000.0 + 5100,
                        rules=Rules(30 * 60))
            out = veille.tick(self.cfg, self.db, now=1000.0 + 5200,
                              rules=Rules(30 * 60))
            self.assertEqual(out["adoptions"][0]["result"], "blocked")
            state = veille.load(self.db, self.lot)["state"]
            self.assertEqual(state["blocked_from"], "rollout")
            proof.return_value = (False, "key of owner revoked since")
            out = veille.tick(self.cfg, self.db, now=1000.0 + 5300,
                              rules=Rules(30 * 60))
        self.assertEqual(out["adoptions"][0]["result"], "refused")
        self.assertIn("proof", out["adoptions"][0]["reason"])
        self.assertEqual(self._bins(), {"canary": None, "impl": None,
                                        "other": None})

    def test_rollback_reads_the_state_under_the_lock(self):
        """Avis B cx2 17:16 : un tick concurrent peut basculer un hôte entre
        la lecture et le verrou ; rollback relit SOUS le verrou et n'écrase ni
        la bascule ni son inverse."""
        veille.adopt(self.cfg, self.db, self.lot, self._report(), bins=self.bins,
                     now=1000.0, rules=self.rules)
        for _ in range(10):
            self._turn("canary", 0.01, 10.0, 1000.0 + 4000)
        veille.tick(self.cfg, self.db, now=1000.0 + 5000, rules=Rules(30 * 60))
        out = veille.tick(self.cfg, self.db, now=1000.0 + 5100,
                          rules=Rules(30 * 60))
        self.assertEqual(out["adoptions"][0]["result"], "rollout")
        self.assertEqual(self._bins()["impl"], self.bins["h1"])
        self.assertIsNone(self._bins()["other"])

        def switch_h2_as_a_tick(db):
            row = veille.load(db, self.lot)
            row["_db"] = db
            veille._advance_rollout(
                self.cfg, db, row, row["state"], veille.Report(row["report"]),
                now=1000.0 + 5200, actor="tick", rules=Rules(30 * 60),
                dry_run=False)

        real_locked = veille._locked

        @contextlib.contextmanager
        def lock_after_the_concurrent_tick(db):
            # le tick concurrent bascule h2 ENTRE la lecture de l'état et la
            # prise du verrou, puis rend la main au rollback.
            switch_h2_as_a_tick(db)
            with real_locked(db) as tx:
                yield tx

        with mock.patch.object(veille, "_locked",
                               lock_after_the_concurrent_tick):
            result = veille.rollback(self.cfg, self.db, self.lot,
                                     reason="régression constatée",
                                     now=1000.0 + 5300)
        self.assertEqual(result["result"], "rolled_back")
        self.assertEqual(self._bins(), {"canary": None, "impl": None,
                                        "other": None})
        state = veille.load(self.db, self.lot)["state"]
        restored = {item["agent"] for item in state["inverse"]["restore"]}
        self.assertEqual(restored, {"canary", "impl", "other"})

    def test_lock_is_transactional_across_sessions(self):
        """#4312 : le verrou doit être visible d'une AUTRE session pendant le
        geste (verrou de transaction), et rendu à la sortie. Un verrou de
        session avec psql ne survivrait pas à la requête suivante."""
        with veille._locked(self.db) as tx:
            self.assertIsNotNone(tx)
            other = self.connect()
            try:
                rows = other.query(
                    "SELECT pg_try_advisory_xact_lock("
                    "hashtextextended(current_schema() || %s, 0)) AS ok",
                    (veille.LOCK_KEY,))
            finally:
                other.close()
            self.assertFalse(rows[0]["ok"], "la seconde session ne prend pas le verrou")
        with veille._locked(self.db) as tx:
            self.assertIsNotNone(tx, "le verrou est rendu à la fin du geste")

    def test_rollback_releases_the_lock_when_the_lot_is_absent(self):
        # #4312 : le verrou est de transaction ; l'erreur doit le rendre avec
        # la transaction (sinon le tick suivant resterait « busy »).
        with self.assertRaises(veille.VeilleError):
            veille.rollback(self.cfg, self.db, 999999,
                            reason="lot absent", now=1000.0)
        with veille._locked(self.db) as tx:
            self.assertIsNotNone(tx, "le verrou a été rendu par rollback")

    def _blocked_owner_canary(self):
        """Adoption owner approuvée, canari bloqué faute de carte de binaires."""
        self._owner_adoption(bins={})
        answered = {"id": 91, "state": "answered", "answer": {"option": "adopt"}}
        with mock.patch.object(veille.decisions, "get", return_value=answered), \
             mock.patch.object(veille.decisions, "verify_proof",
                               return_value=(True, "signed owner key")):
            out = veille.tick(self.cfg, self.db, now=1001.0, bins={},
                              rules=Rules(1800))
        self.assertEqual(out["adoptions"][0]["result"], "blocked")
        state = veille.load(self.db, self.lot)["state"]
        self.assertEqual(state["blocked_from"], "waiting_owner_approved")

    def test_blocked_canary_resume_with_a_withdrawn_decision_is_refused(self):
        """#4312 (N de l'auditeur sur #4293 dc0d) : la reprise d'un canari
        bloqué après approbation ne contourne pas une décision retirée."""
        self._blocked_owner_canary()
        with mock.patch.object(veille.decisions, "get",
                               return_value={"id": 91, "state": "withdrawn"}):
            out = veille.tick(self.cfg, self.db, now=1002.0, bins=self.bins,
                              rules=Rules(1800))
        self.assertEqual(out["adoptions"][0]["result"], "refused")
        self.assertIn("decision", out["adoptions"][0]["reason"])
        self.assertEqual(self._bins(), {"canary": None, "impl": None, "other": None})
        self.assertEqual(veille.load(self.db, self.lot)["state"]["stage"], "refused")

    def test_blocked_canary_resume_with_an_invalid_proof_is_refused(self):
        """#4312 : même reprise avec une preuve invalide (clé révoquée)."""
        self._blocked_owner_canary()
        answered = {"id": 91, "state": "answered", "answer": {"option": "adopt"}}
        with mock.patch.object(veille.decisions, "get", return_value=answered), \
             mock.patch.object(veille.decisions, "verify_proof",
                               return_value=(False, "key revoked since")):
            out = veille.tick(self.cfg, self.db, now=1002.0, bins=self.bins,
                              rules=Rules(1800))
        self.assertEqual(out["adoptions"][0]["result"], "refused")
        self.assertIn("proof", out["adoptions"][0]["reason"])
        self.assertEqual(self._bins(), {"canary": None, "impl": None, "other": None})
        self.assertEqual(veille.load(self.db, self.lot)["state"]["stage"], "refused")

    def test_blocked_canary_restart_appends_to_applied(self):
        """#4312 (N de l'auditeur a00fa) : une reprise de canari AJOUTE à
        `applied` ; elle n'écrase pas l'inverse déjà consigné."""
        veille.adopt(self.cfg, self.db, self.lot, self._report(), bins=self.bins,
                     now=1000.0, rules=self.rules)
        row = veille.load(self.db, self.lot)
        state = row["state"]
        state.update({"stage": "blocked", "blocked_from": "canary",
                      "reason": "no idle agent"})
        row["_db"] = self.db
        veille.save(self.db, row, state)
        out = veille.tick(self.cfg, self.db, now=1001.0, bins=self.bins,
                          rules=Rules(1800))
        self.assertEqual(out["adoptions"][0]["result"], "canary")
        state = veille.load(self.db, self.lot)["state"]
        self.assertEqual(len(state["applied"]), 2,
                         "la reprise ajoute son geste, ne remplace pas l'ancien")
        self.assertEqual(state["applied"][0]["agents"][0]["name"], "canary")
        self.assertEqual(state["applied"][1]["agents"][0]["name"], "impl")

    def test_owner_approval_without_a_valid_proof_is_refused(self):
        higher = self._report(price={
            "kind": "higher", "projected_7d_usd": 12.5,
            "from": {"output": 1.20}, "to": {"output": 1.60}})
        with mock.patch.object(veille.decisions, "ask",
                               return_value={"id": 91, "state": "pending"}):
            veille.adopt(self.cfg, self.db, self.lot, higher, bins=self.bins,
                         now=1000.0, rules=self.rules)
        with mock.patch.object(veille.decisions, "get",
                               return_value={"id": 91, "state": "answered",
                                             "answer": {"option": "adopt"}}), \
             mock.patch.object(veille.decisions, "verify_proof",
                               return_value=(False, "unsigned answer")):
            out = veille.tick(self.cfg, self.db, now=1001.0, rules=Rules(1800))
        self.assertEqual(out["adoptions"][0]["result"], "refused")
        self.assertIn("proof", out["adoptions"][0]["reason"])
        self.assertEqual(self._bins(), {"canary": None, "impl": None, "other": None})

    def test_model_adoption_switches_the_model_setting(self):
        prices = {"model-old": [0.30, 0.006, 1.20],
                  "model-new": [0.30, 0.006, 1.20]}
        self.db.execute("UPDATE agent_registry SET model = 'model-old' "
                        "WHERE name IN ('canary', 'impl', 'other')")
        model_report = self._report(object="model", from_version="model-old",
                                    to_version="model-new")
        result = veille.adopt(self.cfg, self.db, self.lot, model_report,
                              prices=prices, now=1000.0, rules=self.rules)
        self.assertEqual(result["result"], "canary")
        self.assertEqual(registry.get(self.db, "canary")["model"], "model-new")
        self.assertIsNone(registry.get(self.db, "impl")["harness_bin"])
        self.assertIn("harness_bin", registry.get(self.db, "canary"))

    def test_origin_model_is_the_only_target(self):
        """Un agent déjà sur un autre modèle n'est ni canari ni basculé."""
        prices = {"model-old": [0.30, 0.006, 1.20],
                  "model-new": [0.30, 0.006, 1.20]}
        self.db.execute("UPDATE agent_registry SET model = 'model-old' "
                        "WHERE name IN ('canary', 'impl', 'other')")
        registry.upsert(self.db, "expensive", chantier="ameesh",
                        harness="deepseek", host="h2", cwd=self.tmp,
                        session_id=None, status="idle", status_text="",
                        model="expensive-other", budget_usd=None)
        model_report = self._report(object="model", from_version="model-old",
                                    to_version="model-new")
        with self.assertRaises(veille.VeilleError):
            veille._pick_canary(self.db, model_report, bins=None,
                                agent="expensive")
        result = veille.adopt(self.cfg, self.db, self.lot, model_report,
                              prices=prices, now=1000.0, rules=self.rules)
        self.assertEqual(result["result"], "canary")
        self.assertEqual(veille.load(self.db, self.lot)["state"]["target_agent"],
                         "canary")
        for _ in range(10):
            self._turn("canary", 0.01, 10.0, 1000.0 + 4000)
        veille.tick(self.cfg, self.db, now=1000.0 + 5000, rules=Rules(30 * 60))
        veille.tick(self.cfg, self.db, now=1000.0 + 5100, rules=Rules(30 * 60))
        veille.tick(self.cfg, self.db, now=1000.0 + 5200, rules=Rules(30 * 60))
        self.assertEqual(registry.get(self.db, "expensive")["model"],
                         "expensive-other")
        self.assertEqual(registry.get(self.db, "canary")["model"], "model-new")

    def test_rollback_restores_the_previous_values(self):
        self.db.execute("UPDATE agent_registry SET harness_bin = %s "
                        "WHERE name = 'impl'", (self.old_bin,))
        veille.adopt(self.cfg, self.db, self.lot, self._report(), bins=self.bins,
                     now=1000.0, rules=self.rules)
        for _ in range(10):
            self._turn("canary", 0.01, 10.0, 1000.0 + 4000)
        veille.tick(self.cfg, self.db, now=1000.0 + 5000, rules=Rules(30 * 60))
        veille.tick(self.cfg, self.db, now=1000.0 + 5100, rules=Rules(30 * 60))
        self.assertEqual(self._bins()["impl"], self.bins["h1"])
        out = veille.rollback(self.cfg, self.db, self.lot,
                              reason="regression observed on the host",
                              now=1000.0 + 5200)
        self.assertEqual(out["result"], "rolled_back")
        self.assertEqual(self._bins(), {"canary": None, "impl": self.old_bin,
                                        "other": None})
        state = veille.load(self.db, self.lot)["state"]
        self.assertEqual(state["stage"], "rolled_back")
        self.assertEqual(state["inverse"]["version"], "0.2.0-rc.2")

    def test_adopt_is_idempotent_for_the_same_report(self):
        first = veille.adopt(self.cfg, self.db, self.lot, self._report(),
                             bins=self.bins, now=1000.0, rules=self.rules)
        second = veille.adopt(self.cfg, self.db, self.lot, self._report(),
                              bins=self.bins, now=2000.0, rules=self.rules)
        self.assertEqual(second["result"], "unchanged")
        self.assertEqual(first["state"]["stage"], second["state"]["stage"])

    def test_dry_run_writes_nothing(self):
        result = veille.adopt(self.cfg, self.db, self.lot, self._report(),
                              bins=self.bins, now=1000.0, dry_run=True)
        self.assertEqual(result["result"], "dry-run")
        self.assertIsNone(veille.load(self.db, self.lot))
        self.assertEqual(self._bins(), {"canary": None, "impl": None, "other": None})

    def test_report_note_is_read_back(self):
        work.note(self.db, self.lot, "veille-report: " + json.dumps(
            dict(report(), evaluation_lot=self.lot)), actor="canary")
        parsed = veille.report_from_lot(self.db, self.lot)
        self.assertEqual(parsed.evaluation_lot, self.lot)
        self.assertEqual(parsed.harness, "deepseek")

    def test_tick_discovers_a_report_note_and_starts_the_canary(self):
        work.note(self.db, self.lot, "veille-report: " + json.dumps(
            dict(report(), evaluation_lot=self.lot)), actor="canary")
        out = veille.tick(self.cfg, self.db, now=1000.0, bins=self.bins,
                          rules=Rules(30 * 60))
        self.assertEqual(out["discovered"][0]["result"], "canary")
        state = veille.load(self.db, self.lot)["state"]
        self.assertEqual(state["stage"], "canary")
        self.assertEqual(self._bins()["canary"], self.bins["h1"])

    def test_b_real_report_path_is_discovered_without_bin(self):
        """La sortie réelle de B (#4292), canari sur l'hôte du tick : une note
        de trois lignes et un fichier JSON ; le timer n'a pas de --bin, le
        binaire du rapport sert sur place."""
        path = os.path.join(self.tmp, "b-report.json")
        raw = b_report()
        raw["evaluated"]["old"]["binary"] = self.old_bin
        raw["evaluated"]["new"]["binary"] = self.new_bin
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        work.note(self.db, self.lot, self._b_note(path), actor="canary")
        local_cfg = dataclasses.replace(self.cfg, host="h1")
        out = veille.tick(local_cfg, self.db, now=1000.0, bins={},
                          rules=Rules(30 * 60))
        self.assertEqual(out["discovered"][0]["result"], "canary")
        state = veille.load(self.db, self.lot)["state"]
        self.assertEqual(state["stage"], "canary")
        self.assertEqual(state["canary_bin"], self.new_bin)
        self.assertEqual(self._bins()["canary"], self.new_bin)

    def test_b_note_requires_the_report_hash_and_rejects_a_changed_file(self):
        """#4312 (N de l'auditeur sur #4293 dc0d) : la note de B doit figer
        le fichier par son sha256 ; un fichier changé après la note est
        refusé (TOCTOU)."""
        path = os.path.join(self.tmp, "b-report.json")
        raw = b_report()
        raw["evaluated"]["old"]["binary"] = self.old_bin
        raw["evaluated"]["new"]["binary"] = self.new_bin
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        work.note(self.db, self.lot,
                  "Rejeu comparatif deepseek : meilleure.\n"
                  "Banc : #1 (light).\nRapport : %s" % path, actor="canary")
        with self.assertRaises(veille.VeilleError) as caught:
            veille.report_from_lot(self.db, self.lot)
        self.assertIn("sha256", str(caught.exception))

        work.note(self.db, self.lot,
                  self._b_note(path, "Rejeu comparatif deepseek : meilleure."),
                  actor="canary")
        self.assertEqual(veille.report_from_lot(self.db, self.lot).evaluation_lot,
                         self.lot)

        with open(path, "a", encoding="utf-8") as fh:
            fh.write(" ")
        with self.assertRaises(veille.VeilleError) as caught:
            veille.report_from_lot(self.db, self.lot)
        self.assertIn("changed", str(caught.exception))

    def test_b_report_binary_is_refused_on_another_host(self):
        """new_binary est local au tick : un canari distant sans carte
        explicite est bloqué, aucun harness_bin ne change (avis B 16:54)."""
        path = os.path.join(self.tmp, "b-report.json")
        raw = b_report()
        raw["evaluated"]["old"]["binary"] = self.old_bin
        raw["evaluated"]["new"]["binary"] = self.new_bin
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        work.note(self.db, self.lot, self._b_note(
            path, "Rejeu comparatif deepseek : meilleure."), actor="canary")
        out = veille.tick(self.cfg, self.db, now=1000.0, bins={},
                          rules=Rules(30 * 60))
        self.assertEqual(out["discovered"][0]["result"], "blocked")
        state = veille.load(self.db, self.lot)["state"]
        self.assertEqual(state["stage"], "blocked")
        self.assertIn("explicit --bin", state["reason"])
        self.assertEqual(self._bins(), {"canary": None, "impl": None,
                                        "other": None})

    def test_b_report_binary_is_refused_for_an_explicit_remote_canary(self):
        """Le garde de start_canary tient aussi quand l'agent est nommé : le
        chemin du rapport ne traverse pas un hôte distant."""
        result = veille.adopt(self.cfg, self.db, self.lot, self._report(),
                              bins={}, agent="other", now=1000.0,
                              rules=self.rules)
        self.assertEqual(result["result"], "blocked")
        self.assertIn("explicit map", result["state"]["reason"])
        self.assertEqual(self._bins(), {"canary": None, "impl": None,
                                        "other": None})

    def test_bin_outside_the_controlled_prefix_is_refused(self):
        """#4312 : un chemin absolu exécutable hors des racines autorisées ne
        doit pas devenir `harness_bin` (défense en profondeur)."""
        outside = os.path.join("/var/tmp", "ameesh-evil-dsh")
        with mock.patch.dict(os.environ, {veille.BINS_PREFIXES_ENV: ""}):
            with self.assertRaises(veille.VeilleError) as caught:
                veille._check_bin(outside)
        self.assertIn("controlled prefixes", str(caught.exception))
        # le chemin autorisé passe
        self.assertEqual(veille._check_bin(self.new_bin), self.new_bin)

    def test_explicit_bin_for_a_remote_canary_is_not_probed_here(self):
        """Un chemin explicite vise l'hôte distant : sa forme est vérifiée,
        pas son exécution sur le tick (X_OK ne vaut que sur place)."""
        remote_bin = os.path.join(self.tmp, "remote-dsh")
        with open(remote_bin, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(remote_bin, 0o644)
        result = veille.adopt(self.cfg, self.db, self.lot, self._report(),
                              bins={"h1": remote_bin}, now=1000.0,
                              rules=self.rules)
        self.assertEqual(result["result"], "canary")
        self.assertEqual(self._bins()["canary"], remote_bin)

    def test_explicit_bins_map_from_the_timer_switches_every_host(self):
        path = os.path.join(self.tmp, "b-report.json")
        raw = b_report()
        raw["evaluated"]["old"]["binary"] = self.old_bin
        raw["evaluated"]["new"]["binary"] = self.new_bin
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        work.note(self.db, self.lot, self._b_note(
            path, "Rejeu comparatif deepseek : meilleure."), actor="canary")
        bins_path = os.path.join(self.tmp, "bins.json")
        with open(bins_path, "w", encoding="utf-8") as fh:
            json.dump({"bins": {"deepseek": {"0.2.1-rc.4": {
                "h1": self.new_bin, "h2": self.new_bin}}}}, fh)
        with mock.patch.dict(os.environ, {veille.BINS_ENV: bins_path}):
            veille.tick(self.cfg, self.db, now=1000.0, bins={},
                        rules=Rules(30 * 60))
            for _ in range(10):
                self._turn("canary", 0.01, 10.0, 1000.0 + 4000)
            veille.tick(self.cfg, self.db, now=1000.0 + 5000, bins={},
                        rules=Rules(30 * 60))
            veille.tick(self.cfg, self.db, now=1000.0 + 5100, bins={},
                        rules=Rules(30 * 60))
            veille.tick(self.cfg, self.db, now=1000.0 + 5200, bins={},
                        rules=Rules(30 * 60))
            veille.tick(self.cfg, self.db, now=1000.0 + 5300, bins={},
                        rules=Rules(30 * 60))
        self.assertEqual(veille.load(self.db, self.lot)["state"]["stage"], "done")
        self.assertEqual(self._bins(), {"canary": self.new_bin,
                                        "impl": self.new_bin,
                                        "other": self.new_bin})

    def test_a_nonexistent_bin_is_refused_before_any_gesture(self):
        # h1 est l'hôte du tick : le chemin explicite y est éprouvé (X_OK)
        # avant toute écriture.
        local_cfg = dataclasses.replace(self.cfg, host="h1")
        result = veille.adopt(local_cfg, self.db, self.lot, self._report(),
                              bins={"h1": "/nonexistent/dsh",
                                    "h2": self.new_bin},
                              now=1000.0, rules=self.rules)
        self.assertEqual(result["result"], "blocked")
        self.assertEqual(self._bins(), {"canary": None, "impl": None, "other": None})

    def test_discovery_ignores_a_report_written_by_another_agent(self):
        work.note(self.db, self.lot, "veille-report: " + json.dumps(
            dict(report(), evaluation_lot=self.lot)), actor="intruder")
        out = veille.tick(self.cfg, self.db, now=1000.0, bins=self.bins,
                          rules=Rules(30 * 60))
        self.assertEqual(out["discovered"][0]["result"], "ignored")
        self.assertIsNone(veille.load(self.db, self.lot))
        # le rapport du bon auteur, lui, est adopté
        work.note(self.db, self.lot, "veille-report: " + json.dumps(
            dict(report(), evaluation_lot=self.lot)), actor="canary")
        out = veille.tick(self.cfg, self.db, now=1001.0, bins=self.bins,
                          rules=Rules(30 * 60))
        self.assertEqual(out["discovered"][0]["result"], "canary")
        self.assertEqual(veille.load(self.db, self.lot)["state"]["stage"], "canary")

    def test_cli_adopt_and_show(self):
        path = os.path.join(self.tmp, "report-4293.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(dict(report(), evaluation_lot=self.lot,
                           conclusion="regression"), fh)
        done = self.mesh("harness", "adopt", str(self.lot), "--report", path,
                         "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout)
        self.assertEqual(payload["result"], "refused", payload)
        show = self.mesh("harness", "show-adoption", str(self.lot), "--json")
        self.assertEqual(show.returncode, 0, show.stderr)
        payload = json.loads(show.stdout)
        self.assertEqual(payload["state"]["stage"], "refused")
        self.assertEqual(self._bins(), {"canary": None, "impl": None, "other": None})

    def test_runner_adapter_uses_the_agent_harness_bin(self):
        from ameesh.runner import AgentWorker
        binary = os.path.join(self.tmp, "dsh")
        with open(binary, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(binary, 0o755)
        worker = AgentWorker.__new__(AgentWorker)
        threading.Thread.__init__(worker, name="canary", daemon=True)
        worker.cfg = SimpleNamespace(host="h1")
        worker.agent = {"name": "canary", "harness": "deepseek",
                        "harness_bin": binary}
        worker.descriptor_seen = None
        worker.resolution_seen = ""
        worker.turn_harness = lambda: "deepseek"
        # #4488 : le binaire épinglé est déclaré sur l'hôte (harness_bins).
        with mock.patch.dict(adapters._HARNESS_BINS, {"deepseek": binary}):
            adapter = worker._adapter()
        self.assertEqual(adapter.binary, binary)
        # un chemin déclaré mais absent est une erreur d'hôte, jamais un
        # repli silencieux
        absent = os.path.join(self.tmp, "absent")
        worker.agent = {"name": "canary", "harness": "deepseek",
                        "harness_bin": absent}
        with mock.patch.dict(adapters._HARNESS_BINS, {"deepseek": absent}):
            with self.assertRaises(HarnessMissing) as ctx:
                worker._adapter()
        self.assertIn("not found or not executable", str(ctx.exception))
        # #4488 : un chemin NON déclaré est refusé avant toute résolution.
        with mock.patch.dict(adapters._HARNESS_BINS, {"deepseek": binary}):
            with self.assertRaises(HarnessMissing) as ctx:
                worker._adapter()
        self.assertIn("not a binary declared", str(ctx.exception))

    def test_ameesh_set_accepts_an_absolute_harness_bin(self):
        binary = os.path.join(self.tmp, "dsh")
        with open(binary, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(binary, 0o755)
        done = self.mesh("set", "canary", "harness_bin=" + binary)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(registry.get(self.db, "canary")["harness_bin"], binary)
        done = self.mesh("set", "canary", "harness_bin=relative/dsh")
        self.assertEqual(done.returncode, 2)
        done = self.mesh("set", "canary", "harness_bin=")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIsNone(registry.get(self.db, "canary")["harness_bin"])
