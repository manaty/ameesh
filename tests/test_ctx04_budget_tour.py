# SPDX-License-Identifier: AGPL-3.0-only
"""CTX-04a (#4970) — budget de relecture par tour, opt-in (étude CTX-01, §5.1 point 4).

Dépassé : clôture au prochain point sûr, puis la même session ; ni rotation ni
compactage pour ce seul motif. Éteint : le mode legacy ne bouge pas. Sans
mesure : aucune décision.
Postgres réel ; faux harnais `claude` du banc.
"""
from __future__ import annotations

import dataclasses
import types
import unittest
from unittest import mock

from ameesh import config as config_mod, context_metrics, cost as cost_mod, storage
from ameesh.runner import REREAD_BUDGET_CLOSE, AgentWorker

from .support import EMPTY_CONFIG
from .test_l105_plafonds_du_tour import _Base


def _due(live, tour=0, session=0):
    worker = types.SimpleNamespace(cfg=types.SimpleNamespace(
        turn_reread_budget=tour, session_reread_budget=session))
    return AgentWorker.reread_budget_due(worker, live)


class DecisionTest(unittest.TestCase):
    def test_eteint_aucune_decision(self):
        self.assertIsNone(_due({"R_turn": 10**9, "R_session": 10**9, "base": 0}))

    def test_budget_de_tour_depasse(self):
        due = _due({"R_turn": 1000, "R_session": None, "base": None}, tour=1000)
        self.assertEqual((due["kind"], due["measure"], due["value"], due["budget"]),
                         (REREAD_BUDGET_CLOSE, "R_turn", 1000, 1000))
        self.assertIsNone(_due({"R_turn": 999, "R_session": None, "base": None}, tour=1000))

    def test_mesure_nulle_aucune_decision(self):
        """R_turn null n'est jamais un 0, ni un dépassement."""
        vide = context_metrics.TurnNormalizer("claude", session="s", session_reread=5000)
        live = vide.live_reread()
        self.assertIsNone(live["R_turn"])
        self.assertIsNone(live["R_session"])
        self.assertIsNone(_due(live, tour=1, session=1))

    def test_budget_de_session_a_chaque_tranche_franchie(self):
        """Le cumul reste acquis dans la même session : les tours suivants ne
        se closent pas dès leur premier point sûr, seulement à la tranche
        suivante."""
        self.assertEqual(
            _due({"R_turn": 200, "R_session": 1100, "base": 900}, session=1000)["measure"],
            "R_session")
        self.assertIsNone(_due({"R_turn": 200, "R_session": 1300, "base": 1100}, session=1000))
        self.assertIsNotNone(_due({"R_turn": 800, "R_session": 2100, "base": 1300}, session=1000))

    def test_base_de_session_inconnue_aucune_decision(self):
        self.assertIsNone(_due({"R_turn": 5000, "R_session": None, "base": None}, session=1000))

    def test_cumul_codex_de_dix_appels_jamais_lu_comme_c_last(self):
        """R3 : dix cumuls de fil donnent un delta de tour et pas de `C_last`."""
        normalizer = context_metrics.TurnNormalizer(
            "codex", session="fil", session_reread=0,
            cumulative={"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0})
        for appel in range(1, 11):
            normalizer.observe({"usage": {"input_tokens": 50_000 * appel,
                                          "cached_input_tokens": 0, "output_tokens": 0}})
        live = normalizer.live_reread()
        self.assertEqual(live["R_turn"], 500_000)
        document = normalizer.finish()
        self.assertIsNone(document["measures"]["C_last"]["value"])
        self.assertEqual(document["measures"]["R_turn"]["value"], 500_000)

    def test_cumul_codex_sans_point_de_depart_aucune_decision(self):
        normalizer = context_metrics.TurnNormalizer("codex", session="fil")
        normalizer.observe({"usage": {"input_tokens": 900_000, "output_tokens": 0}})
        self.assertIsNone(_due(normalizer.live_reread(), tour=1))

    def test_releve_d_une_autre_session_aucune_decision(self):
        normalizer = context_metrics.TurnNormalizer("claude", session="a", session_reread=0)
        normalizer.call({"input_tokens": 5000}, "m1")
        normalizer.observe({"session": "b"})
        self.assertIsNone(_due(normalizer.live_reread(), tour=1, session=1))

    def test_defaut_eteint_et_environnement(self):
        self.assertEqual(config_mod.Config().turn_reread_budget, 0)
        self.assertEqual(config_mod.Config().session_reread_budget, 0)
        cfg = config_mod.load(env={"AMEESH_TURN_REREAD_BUDGET": "400000",
                                   "AMEESH_SESSION_REREAD_BUDGET": "-1",
                                   "AMEESH_CONFIG": EMPTY_CONFIG})
        self.assertEqual((cfg.turn_reread_budget, cfg.session_reread_budget), (400000.0, 0.0))

    def test_budget_non_fini_eteint_et_fraction_arrondie(self):
        """#5051 : inf/nan n'atteignent jamais `int()` au tour ; 0,5 n'est
        plus silencieusement éteint, il vaut un jeton."""
        for value in ("inf", "-inf", "nan"):
            cfg = config_mod.load(env={"AMEESH_TURN_REREAD_BUDGET": value,
                                       "AMEESH_SESSION_REREAD_BUDGET": value,
                                       "AMEESH_CONFIG": EMPTY_CONFIG})
            self.assertEqual((cfg.turn_reread_budget, cfg.session_reread_budget),
                             (0.0, 0.0), value)
            self.assertIsNone(_due({"R_turn": 10**9, "R_session": 10**9, "base": 0},
                                   tour=int(cfg.turn_reread_budget),
                                   session=int(cfg.session_reread_budget)))
        cfg = config_mod.load(env={"AMEESH_TURN_REREAD_BUDGET": "0.5",
                                   "AMEESH_SESSION_REREAD_BUDGET": "1.2",
                                   "AMEESH_CONFIG": EMPTY_CONFIG})
        self.assertEqual((cfg.turn_reread_budget, cfg.session_reread_budget), (1.0, 2.0))
        self.assertEqual(
            _due({"R_turn": 1, "R_session": None, "base": None},
                 tour=int(cfg.turn_reread_budget))["budget"], 1)


class ClotureTest(_Base):
    #: quatre appels de 50 k : la somme dépasse 120 k au troisième, le contexte
    #: du dernier appel reste 50 k
    APPELS = ([{"reread": 50_000, "tool": True}] * 3
              + [{"reread": 50_000, "tool": True, "sleep": 3}])

    def _worker_budget(self, name, **budgets):
        self.cfg = dataclasses.replace(self.cfg, session_max_tokens=10**9, **budgets)
        return self._worker(name)[1]

    def _tour(self, worker, appels, spec=None):
        with self._env(appels):
            self.assertTrue(worker.run_turn(
                spec or {"kind": "prompt", "prompt": "travail", "ids": []}))

    def _contexte(self, name):
        lignes = storage.of(self.db).events.recent_type(
            cost_mod.CONTEXT_EVENT, 1, 5, object="agent:" + name)
        return lignes[0]["data"]

    def test_budget_depasse_cloture_sure_meme_session_sans_rotation(self):
        worker = self._worker_budget("budget-tour", turn_reread_budget=120_000.0)
        self._tour(worker, self.APPELS)
        self.assertTrue(worker.last_turn_closed.startswith("reread budget"))
        flux = self._evenements(worker)
        self.assertIn('"tool_use_id": "outil-2"', flux)    # l'outil en cours a fini
        self.assertNotIn('"tool_use_id": "outil-3"', flux)  # le suivant, jamais
        self.assertIsNone(worker.rotation_forcee)
        session = worker.current_session()
        self.assertTrue(session)
        with self._env():
            self.assertFalse(worker.maybe_rotate())
        self.assertEqual(worker.current_session(), session)
        # tracé : événement, raison normalisée, mesure et budget
        donnees = self._contexte("budget-tour")
        self.assertEqual(
            {k: donnees["decision"]["close"][k] for k in ("kind", "measure", "value", "budget")},
            {"kind": REREAD_BUDGET_CLOSE, "measure": "R_turn",
             "value": 150_000, "budget": 120_000})
        self.assertEqual(donnees["policy"]["turn_reread_budget"], 120_000)
        # la suite reprend la même session
        spec = worker.pick()
        self.assertEqual(spec["kind"], "suite")
        self.clear_turns()
        self._tour(worker, [], spec)
        argv = self.turns()[-1]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], session)

    def test_budget_eteint_comportement_legacy_identique(self):
        worker = self._worker_budget("budget-eteint")
        with mock.patch.object(worker, "reread_budget_due") as decision:
            self._tour(worker, self.APPELS)
        decision.assert_not_called()
        self.assertIsNone(worker.last_turn_closed)
        self.assertIn('"tool_use_id": "outil-3"', self._evenements(worker))
        politique = self._contexte("budget-eteint")["policy"]
        self.assertNotIn("turn_reread_budget", politique)
        self.assertNotIn("session_reread_budget", politique)

    def test_400k_cumules_c_last_50k_pas_de_rotation(self):
        """R4 : le cumul n'est pas une fenêtre, même au-dessus de `context_max_tokens`."""
        self.cfg = dataclasses.replace(self.cfg, session_max_tokens=10**9,
                                       turn_reread_budget=400_000.0)
        _runner, worker = self._worker("budget-r4", context_max_tokens=100_000)
        self._tour(worker, [{"reread": 50_000, "tool": True}] * 8
                   + [{"reread": 50_000, "tool": True, "sleep": 3}])
        self.assertTrue(worker.last_turn_closed.startswith("reread budget"))
        # la décision lit 400 k ; le flux se lit jusqu'au bout (L217b)
        self.assertEqual(self._contexte("budget-r4")["decision"]["close"]["value"], 400_000)
        self.assertEqual(worker.last_turn_context, 50_000)
        self.assertIsNone(worker.rotation_forcee)
        self.assertFalse(worker.context_rotation_due())
        with self._env():
            self.assertFalse(worker.maybe_rotate())

    def test_le_plafond_de_contexte_passe_avant_le_budget(self):
        """Budget atteint, plafond atteint avant le point sûr : la rotation reste."""
        self.cfg = dataclasses.replace(self.cfg, turn_reread_budget=500.0)
        _runner, worker = self._worker("budget-plafond", context_max_tokens=1000)
        self._tour(worker, [{"reread": 600}, {"reread": 1200, "tool": True}])
        self.assertTrue(worker.last_turn_closed.startswith("context cap"))
        self.assertTrue(worker.rotation_forcee)
        self.assertEqual(self._contexte("budget-plafond")["decision"]["close"]["kind"],
                         "context_cap")

    def test_budget_de_session_clot_puis_laisse_travailler(self):
        worker = self._worker_budget("budget-session", session_reread_budget=120_000.0)
        self._tour(worker, self.APPELS)
        self.assertTrue(worker.last_turn_closed.startswith("reread budget: R_session"))
        # le tour suivant ne franchit pas la tranche suivante (240 k)
        spec = worker.pick()
        self._tour(worker, [{"reread": 10_000, "tool": True}] * 2, spec)
        self.assertIsNone(worker.last_turn_closed)
