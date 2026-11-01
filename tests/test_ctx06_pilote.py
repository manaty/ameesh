# SPDX-License-Identifier: AGPL-3.0-only
"""CTX-06 (#5250) — pilote réversible de la politique de contexte d'un agent.

Réglages par agent, pose et retour atomiques, critères de retour d'e23.
Postgres réel pour l'état ; les faits du contrôle sont passés à la décision.
"""
from __future__ import annotations

import argparse
import unittest
from unittest import mock

from ameesh import context_metrics, context_pilot as pilot, cost, exploitation, registry, storage

from .test_l105_plafonds_du_tour import _Base


def _window(actions, cost):
    return {"actions": actions, "cost": cost}


class CostTest(unittest.TestCase):
    def test_une_fenetre_replie_deux_retour(self):
        self.assertIsNone(pilot.cost_verdict([_window(10, 12)], 1.0))
        self.assertEqual(pilot.cost_verdict([_window(10, 12.1)], 1.0), "fallback")
        self.assertEqual(pilot.cost_verdict([_window(10, 13), _window(10, 13)], 1.0), "return")

    def test_sous_effectif_ni_lu_ni_separateur(self):
        self.assertIsNone(pilot.cost_verdict([_window(9, 900)], 1.0))
        self.assertEqual(pilot.cost_verdict(
            [_window(10, 13), _window(2, 0), _window(10, 13)], 1.0), "return")

    def test_inconnu_ne_decide_rien(self):
        self.assertIsNone(pilot.cost_verdict([_window(10, None)], 1.0))
        self.assertIsNone(pilot.cost_verdict([_window(10, 900)], None))


class WindowsTest(unittest.TestCase):
    def test_heures_d_avant_la_pose_ecartees(self):
        windows = [_window(1, 99), _window(5, 5), _window(5, 6)]
        self.assertEqual(pilot.since_apply(windows, 0.0, 7300.0), windows[-2:])
        self.assertEqual(pilot.since_apply(windows, 0.0, 100.0), [])
        self.assertIsNone(pilot.since_apply(None, 0.0, 7300.0))

    def test_cumul_glissant(self):
        self.assertEqual(pilot.rolling([_window(1, 99)] + [_window(1, 2)] * 24),
                         {"actions": 24, "cost": 48})
        self.assertIsNone(pilot.rolling([_window(1, None)]))
        self.assertIsNone(pilot.rolling([]))

    def test_sortie_illisible_reste_inconnue(self):
        for sortie in ("pas du json", "[1]", "{}"):
            with mock.patch.object(pilot.subprocess, "run",
                                   return_value=mock.Mock(stdout=sortie)):
                self.assertIsNone(pilot._cost_windows("x"), sortie)


class FactsTest(unittest.TestCase):
    STATE = {"baseline_cost": 1.0, "baseline_cost_24h": 1.0, "fallback_turn_budget": 2_000_000, "fallback_ts": None}

    def test_chevauchement_sur_deux_hotes_seulement(self):
        a = {"started_ts": 100, "ended_ts": 300, "host": "a"}
        self.assertTrue(pilot.overlaps([a, {"started_ts": 200, "ended_ts": 400, "host": "b"}]))
        self.assertFalse(pilot.overlaps([a, {"started_ts": 200, "ended_ts": 400, "host": "a"}]))
        # la relève : l'écart d'horloge n'est pas un double bail
        self.assertFalse(pilot.overlaps([a, {"started_ts": 290, "ended_ts": 400, "host": "b"}]))

    def test_long_tour_contenant_un_court(self):
        """Relecture : A[100,1000], A[200,300], B[400,500] se chevauchent."""
        self.assertTrue(pilot.overlaps([
            {"started_ts": 100, "ended_ts": 1000, "host": "a"},
            {"started_ts": 200, "ended_ts": 300, "host": "a"},
            {"started_ts": 400, "ended_ts": 500, "host": "b"}]))

    def test_garde_glissante_24h(self):
        self.assertEqual(pilot.verdict({"rolling_24h": _window(10, 13)}, self.STATE)[0], "return")
        self.assertIsNone(pilot.verdict({"rolling_24h": _window(10, 12)}, self.STATE)[0])
        self.assertIsNone(pilot.verdict({"rolling_24h": _window(9, 900)}, self.STATE)[0])

    def test_motifs(self):
        for facts in ({"guards": ["account_overage"]}, {"double_lease": [("a", "b", 1)]},
                      {"oom": True}, {"monitoring_lost": True},
                      {"cost_windows": [_window(10, 13), _window(10, 13)]}):
            self.assertEqual(pilot.verdict(facts, self.STATE)[0], "return", facts)

    def test_oom_inconnu_ne_ramene_pas(self):
        self.assertEqual(pilot.verdict({"oom": None, "cost_windows": None}, self.STATE)[0], None)

    def test_repli_une_seule_fois(self):
        facts = {"cost_windows": [_window(10, 13)]}
        self.assertEqual(pilot.verdict(facts, self.STATE)[0], "fallback")
        self.assertIsNone(pilot.verdict(facts, dict(self.STATE, fallback_ts=1))[0])


class PilotDbTest(_Base):
    def _args(self, name, **kw):
        return argparse.Namespace(**{**dict(
            agent=name, context_max=1_000_000, session_max=1_000_000,
            turn_budget=1_500_000, session_budget=40_000_000,
            fallback_turn_budget=2_000_000, baseline_cost=1.0, baseline_cost_24h=1.0,
            lot=None, ref="décision test", dry_run=False, actor="test"), **kw})

    def _apply(self, name, **kw):
        with mock.patch.object(pilot, "_tell"):
            return pilot.apply_pilot(self.cfg, self.db, self._args(name, **kw), 1000.0)

    def test_pose_seul_agent_puis_retour_au_profil(self):
        _runner, worker = self._worker("pilote-a", context_max_tokens=400_000)
        _runner2, autre = self._worker("pilote-b", context_max_tokens=400_000)
        legacy = autre.session_max_tokens()
        self.assertEqual(self._apply("pilote-a"), 0)
        row = registry.get(self.db, "pilote-a")
        self.assertEqual((row["context_max_tokens"], row["session_max_tokens"],
                          row["turn_reread_budget"], row["session_reread_budget"]),
                         (1_000_000, 1_000_000, 1_500_000, 40_000_000))
        worker.agent = row
        self.assertEqual(worker.session_max_tokens(), 1_000_000.0)
        self.assertEqual(worker.turn_reread_budget(), 1_500_000)
        self.assertEqual(worker.reread_budget_due(
            {"R_turn": 1_500_000, "R_session": None, "base": None})["measure"], "R_turn")
        # les autres agents gardent leur lecture
        autre.agent = registry.get(self.db, "pilote-b")
        self.assertEqual(autre.session_max_tokens(), legacy)
        self.assertEqual(autre.turn_reread_budget(), 0)

        state = pilot._state_of(self.db, "pilote-a")
        with mock.patch.object(pilot, "_tell"):
            self.assertTrue(pilot._return(self.db, "pilote-a", state, "test", "t", 2000.0))
            # deux contrôles concurrents ne font qu'un retour
            self.assertFalse(pilot._return(self.db, "pilote-a", state, "test", "t", 2001.0))
        row = registry.get(self.db, "pilote-a")
        self.assertEqual((row["context_max_tokens"], row["session_max_tokens"],
                          row["turn_reread_budget"], row["session_reread_budget"]),
                         (400_000, None, None, None))
        self.assertEqual(row["context_pilot"]["return_reason"], "test")

    def test_second_pilote_refuse_puis_reouverture(self):
        self._worker("pilote-c")
        self.assertEqual(self._apply("pilote-c"), 0)
        self.assertEqual(self._apply("pilote-c", context_max=5), 1)
        self.assertEqual(registry.get(self.db, "pilote-c")["context_max_tokens"], 1_000_000)
        state = pilot._state_of(self.db, "pilote-c")
        with mock.patch.object(pilot, "_tell"):
            pilot._return(self.db, "pilote-c", state, "fin", "t", 2000.0)
        self.assertEqual(self._apply("pilote-c"), 0)

    def test_controle_ancien_n_ecrase_pas_un_pilote_rouvert(self):
        self._worker("pilote-g", context_max_tokens=400_000)
        self.assertEqual(self._apply("pilote-g"), 0)
        ancien = pilot._state_of(self.db, "pilote-g")
        with mock.patch.object(pilot, "_tell"):
            self.assertTrue(pilot._return(self.db, "pilote-g", ancien, "fin", "t", 2000.0))
            self.assertEqual(self._apply("pilote-g", context_max=2_000_000), 0)
            self.assertFalse(pilot._return(self.db, "pilote-g", ancien, "périmé", "t", 3000.0))
        self.assertEqual(registry.get(self.db, "pilote-g")["context_max_tokens"], 2_000_000)

    def test_garde_nouvelle_dans_la_meme_fenetre_de_forfait(self):
        """Relecture : `since` d'un dépassement date la fenêtre, pas le dépassement."""
        self._worker("pilote-h")
        depassement = {"type": "account_overage", "agent": "pilote-h", "since": 1.0}
        with mock.patch.object(pilot, "_tell"), \
                mock.patch.object(pilot.exploitation, "alerts", return_value=[]):
            self.assertEqual(self._apply("pilote-h"), 0)
        state = pilot._state_of(self.db, "pilote-h")
        with mock.patch.object(pilot.exploitation, "alerts", return_value=[depassement]), \
                mock.patch.object(pilot, "_oom_seen", return_value=None):
            facts = pilot.collect(self.cfg, self.db, "pilote-h", state, 2000.0)
        self.assertEqual(facts["guards"], ["account_overage"])
        # déjà levée à la pose : pas un effet du pilote
        state["guards_at_apply"] = [pilot._guard_id(depassement)]
        with mock.patch.object(pilot.exploitation, "alerts", return_value=[depassement]), \
                mock.patch.object(pilot, "_oom_seen", return_value=None):
            self.assertEqual(pilot.collect(self.cfg, self.db, "pilote-h", state, 2000.0)["guards"], [])

    def test_rattachement_des_alertes_de_compte(self):
        """Relecture : le producteur publie `agent=None`, parfois `host=None`."""
        self._worker("pilote-i")
        self.db.execute("UPDATE agent_registry SET session_id = 's1', session_account = 'c1', host = 'h1',"
                        " session_harness = 'claude' WHERE name = 'pilote-i'")

        def garde(**champs):
            return dict({"type": "account_overage", "agent": None, "since": 1.0}, **champs)

        visees = [garde(host=None, harness="claude", account="c1"),
                  garde(host="h2", harness="claude", account="c1"),
                  {"type": "guard_restart", "agent": None, "host": "h1", "since": 1.0}]
        ecartees = [garde(host="h1", harness="codex", account="c1"),
                    garde(host="h1", harness="claude", account="c2"),
                    {"type": "guard_restart", "agent": None, "host": "h2", "since": 1.0}]
        with mock.patch.object(pilot.exploitation, "alerts", return_value=visees + ecartees):
            self.assertEqual(pilot._guard_alerts(self.cfg, self.db, "pilote-i"), visees)

    def test_prise_en_compte_sur_une_charge_reelle(self):
        """Relecture : `context_publication` met `policy` à la racine de la charge."""
        self._worker("pilote-j", context_max_tokens=400_000)
        self.assertEqual(self._apply("pilote-j"), 0)

        def tour(**policy):
            return cost.context_publication(
                context_metrics.TurnNormalizer("claude").finish(),
                legacy=dict.fromkeys(cost.CONTEXT_LEGACY_FIELDS), agent="pilote-j", host="h",
                decision={"policy": dict(policy, session="taille")})

        pose = {"context_max_tokens": 1_000_000, "session_max_tokens": 1_000_000,
                "turn_reread_budget": 1_500_000, "session_reread_budget": 40_000_000}
        with mock.patch.object(pilot, "_context_payloads", return_value=[tour(**pose)]):
            self.assertIs(pilot.status(self.cfg, self.db, "pilote-j", 2000.0)["effective"], True)
        with mock.patch.object(pilot, "_context_payloads",
                               return_value=[tour(context_max_tokens=400_000)]):
            self.assertIs(pilot.status(self.cfg, self.db, "pilote-j", 2000.0)["effective"], False)
        state = pilot._state_of(self.db, "pilote-j")
        with mock.patch.object(pilot, "_tell"):
            pilot._return(self.db, "pilote-j", state, "fin", "t", 2000.0)
        with mock.patch.object(pilot, "_context_payloads",
                               return_value=[tour(context_max_tokens=400_000)]) as lu:
            self.assertIs(pilot.status(self.cfg, self.db, "pilote-j", 3000.0)["effective"], True)
        self.assertEqual(lu.call_args.args[2], 2000.0)

    def test_controle_replie_puis_ramene(self):
        self._worker("pilote-d", context_max_tokens=400_000)
        self.assertEqual(self._apply("pilote-d"), 0)
        one = {"guards": [], "cost_windows": [_window(10, 13)]}
        with mock.patch.object(pilot, "_tell"), mock.patch.object(pilot, "collect", return_value=one):
            self.assertEqual(pilot.check(self.cfg, self.db, "pilote-d", "t", 2000.0)["action"],
                             "fallback")
            self.assertEqual(registry.get(self.db, "pilote-d")["turn_reread_budget"], 2_000_000)
        two = {"guards": [], "cost_windows": [_window(10, 13), _window(10, 13)]}
        with mock.patch.object(pilot, "_tell"), mock.patch.object(pilot, "collect", return_value=two):
            self.assertEqual(pilot.check(self.cfg, self.db, "pilote-d", "t", 3000.0)["action"],
                             "return")
            self.assertEqual(pilot.check(self.cfg, self.db, "pilote-d", "t", 3001.0)["action"], None)
        self.assertEqual(registry.get(self.db, "pilote-d")["context_max_tokens"], 400_000)

    def test_faits_sur_les_vraies_lectures(self):
        """Sans mock de la collecte : les lectures répondent, l'inconnu ne décide rien."""
        self._worker("pilote-f")
        self.assertEqual(self._apply("pilote-f"), 0)
        import time
        now = time.time()
        with mock.patch.object(pilot, "_oom_seen", return_value=None):
            out = pilot.check(self.cfg, self.db, "pilote-f", "t", now)
        self.assertIsNone(out["action"])
        self.assertIsNone(out["facts"]["cost_windows"])
        seen = pilot.status(self.cfg, self.db, "pilote-f", now)
        self.assertTrue(seen["open"])
        self.assertIsNone(seen["effective"])

    def test_vue_d_exploitation(self):
        self._worker("pilote-e")
        storage.of(self.db).operations.set_settings("pilote-e", {"session_max_tokens": "7"})
        row = registry.get(self.db, "pilote-e")
        view = exploitation.agent_view(self.cfg, row)
        self.assertEqual(view["session_max_tokens_set"], 7)
        self.assertEqual(view["turn_reread_budget"], 0)
