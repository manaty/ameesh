# SPDX-License-Identifier: AGPL-3.0-only
"""CTX-04b (#4971) — entretien de fenêtre qualifié, opt-in (étude CTX-01, §5.1 point 3).

`C_next` proche de `W` connue, compactage supporté : compactage natif, puis
renouvellement si la projection reste proche. `W` ou projection inconnue : aucune
décision, inconnue nommée. Éteint : le mode legacy ne bouge pas.
Postgres réel ; faux harnais résident du banc.
"""
from __future__ import annotations

import dataclasses
import json
import os
import types
import unittest
from unittest import mock

from ameesh import config as config_mod, context_metrics, cost as cost_mod, resident_runner, storage
from ameesh.runner import AgentWorker

from . import test_l81_resident_rotation as l81
from .support import EMPTY_CONFIG


def _projection(**kwargs):
    valeurs = {"c_last": 50_000, "o_last": 30_000, "window": 100_000, "share": 0.8,
               "input_reserve": 1_000, "output_reserve": 5_000,
               "compact_supported": True}
    valeurs.update(kwargs)
    return resident_runner.window_maintenance(**valeurs)


class ProjectionTest(unittest.TestCase):
    def test_exemple_du_contrat(self):
        """§2.3 : 50 000 + 30 000 + 1 000, puis 5 000 de réserve."""
        projection = _projection()
        self.assertEqual((projection["C_next_input"], projection["C_next"]),
                         (81_000, 86_000))
        self.assertEqual(projection["decision"], resident_runner.COMPACT)
        self.assertEqual(projection["unknowns"], [])

    def test_sous_le_seuil_rien(self):
        projection = _projection(window=1_000_000)
        self.assertIs(projection["near"], False)
        self.assertEqual(projection["decision"], "")

    def test_entree_proche_de_w_moins_la_reserve(self):
        """`C_next` sous le seuil, `C_next_input` au-dessus de `W - O_reserve_next`."""
        projection = _projection(c_last=70_000, o_last=0, input_reserve=0,
                                 output_reserve=20_000, window=100_000, share=0.9)
        self.assertLess(projection["C_next"], 100_000)
        self.assertIs(projection["near"], True)

    def test_compaction_deja_tentee_renouvellement(self):
        self.assertEqual(_projection(compact_tried=True)["decision"],
                         resident_runner.WINDOW_RENEW)

    def test_inconnues_nommees_aucune_decision(self):
        for kwargs, nom in (({"window": None}, "W"), ({"c_last": None}, "C_last"),
                            ({"o_last": None}, "O_last"),
                            ({"input_reserve": -1}, "G_input_reserve"),
                            ({"output_reserve": -1}, "O_reserve_next")):
            with self.subTest(nom):
                projection = _projection(**kwargs)
                self.assertEqual(projection["decision"], "")
                self.assertIsNone(projection["near"])
                self.assertEqual(projection["unknowns"], [nom])

    def test_reserve_de_sortie_inconnue_garde_l_entree(self):
        projection = _projection(output_reserve=-1)
        self.assertEqual(projection["C_next_input"], 81_000)
        self.assertIsNone(projection["C_next"])

    def test_compaction_non_supportee_nommee_sans_decision(self):
        projection = _projection(compact_supported=False)
        self.assertIs(projection["near"], True)
        self.assertEqual(projection["decision"], "")
        self.assertEqual(projection["unknowns"], ["native_compaction"])


class ConfigTest(unittest.TestCase):
    def _load(self, **env):
        return config_mod.load(env={"AMEESH_CONFIG": EMPTY_CONFIG, **env})

    def test_eteint_et_non_declare_par_defaut(self):
        cfg = self._load()
        self.assertEqual((cfg.window_maintenance_share, cfg.window_input_reserve,
                          cfg.window_output_reserve), (0.0, -1.0, -1.0))

    def test_lecture_et_bornes(self):
        cfg = self._load(AMEESH_WINDOW_MAINTENANCE_SHARE="7",
                         AMEESH_WINDOW_INPUT_RESERVE="1000",
                         AMEESH_WINDOW_OUTPUT_RESERVE="inf")
        self.assertEqual((cfg.window_maintenance_share, cfg.window_input_reserve,
                          cfg.window_output_reserve), (1.0, 1000.0, -1.0))


class MesureDeProfilTest(unittest.TestCase):
    def test_fenetre_du_profil_jamais_celle_de_l_auxiliaire(self):
        """R1 : `context_window` legacy vient du premier modèle de `modelUsage`."""
        rp = resident_runner.ResidentProcess(mock.Mock(), key=("k",), life_id="l", stderr=None)
        rp.record_turn(types.SimpleNamespace(
            usage={"context_tokens": 10, "context_window": 200_000},
            profile={"window_tokens": 1_000_000}))
        self.assertEqual((rp.context_window, rp.profile_window), (200_000, 1_000_000))
        rp.record_turn(types.SimpleNamespace(usage={}, profile={"window_tokens": None}))
        self.assertIsNone(rp.profile_window)


class MesurePartielleTest(unittest.TestCase):
    """B1 (ameesh-cx1, mty-amh-qa-01) : une mesure partielle n'entre jamais dans
    la projection ; l'inconnue est nommée et rien n'est décidé."""

    APPEL = {"input_tokens": 900, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0, "output_tokens": 7}

    def _garde(self, document):
        worker = types.SimpleNamespace(_window_measure=None)
        AgentWorker._window_measure_keep(worker, document, False)
        return worker._window_measure

    def _normalizer(self, harness="claude", **kwargs):
        return context_metrics.TurnNormalizer(harness, session="s", session_reread=0, **kwargs)

    def _inconnues(self, document, *noms, motif):
        self.assertIn(motif, document["reasons"])
        mesure = self._garde(document)
        for nom in noms:
            self.assertIsNone(mesure[nom], nom)
        projection = _projection(c_last=mesure["C_last"], o_last=mesure["O_last"])
        self.assertEqual(projection["decision"], "")
        self.assertIsNone(projection["near"])
        self.assertEqual(projection["unknowns"], list(noms))

    def test_tour_complet_mesure(self):
        normalizer = self._normalizer()
        normalizer.observe({"session": "s", "call_usage": self.APPEL, "call_id": "a"})
        mesure = self._garde(normalizer.finish())
        self.assertEqual((mesure["C_last"], mesure["O_last"]), (900, 7))

    def test_tour_interrompu(self):
        normalizer = self._normalizer()
        normalizer.observe({"session": "s", "call_usage": self.APPEL, "call_id": "a"})
        self._inconnues(normalizer.finish(interrupted=True), "C_last", "O_last",
                        motif=context_metrics.REASON_TURN_INTERRUPTED)

    def test_borne_d_appels(self):
        normalizer = self._normalizer(max_calls=1)
        for ident in ("a", "b"):
            normalizer.observe({"session": "s", "call_usage": self.APPEL, "call_id": ident})
        self._inconnues(normalizer.finish(), "C_last", "O_last",
                        motif=context_metrics.REASON_CALL_BOUND_REACHED)

    def test_autre_session(self):
        normalizer = self._normalizer()
        normalizer.observe({"session": "s", "call_usage": self.APPEL, "call_id": "a"})
        normalizer.observe({"session": "s2", "call_usage": self.APPEL, "call_id": "b"})
        self._inconnues(normalizer.finish(), "C_last", "O_last",
                        motif=context_metrics.REASON_SESSION_MISMATCH)

    def test_cache_ecrit_non_qualifie(self):
        normalizer = self._normalizer("codex")
        normalizer.observe({"session": "s", "call_usage": {
            "input_tokens": 900, "cached_input_tokens": 0, "output_tokens": 7,
            "cache_creation_input_tokens": 50}, "call_id": "a"})
        document = normalizer.finish()
        self.assertEqual(document["measures"]["C_last"]["quality"], context_metrics.PARTIAL)
        self._inconnues(document, "C_last",
                        motif=context_metrics.REASON_UNQUALIFIED_CACHE_WRITE)


ACTIF = {"window_maintenance_share": 0.8, "window_input_reserve": 0.0,
         "window_output_reserve": 0.0}


class EntretienTest(l81.ResidentBench):
    """Le geste entre deux tours, sur le banc résident de L81."""

    ENV = {"FAKE_CLAUDE_CONTEXT": "900", "FAKE_CLAUDE_WINDOW": "1000"}

    def _actif(self, **valeurs):
        self.cfg = dataclasses.replace(self.cfg, **{**ACTIF, **valeurs})

    def _context_events(self):
        return [event["data"] for event in reversed(storage.of(self.db).events.recent_type(
            cost_mod.CONTEXT_EVENT, 1, 50, object="agent:res81"))]

    def test_eteint_le_legacy_ne_bouge_pas(self):
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_CONTEXT": "600",
                                          "FAKE_CLAUDE_WINDOW": "1000"}):
            _runner, worker = self._worker(resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            self.assertEqual(worker._resident_maintenance(), resident_runner.COMPACT)
            self.assertNotIn("window", self._context_events()[-1]["decision"])

    def test_allume_le_50_pour_cent_legacy_ne_compacte_plus(self):
        self._actif()
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_CONTEXT": "600",
                                          "FAKE_CLAUDE_WINDOW": "1000"}):
            _runner, worker = self._worker(resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            self.assertEqual(worker._resident_maintenance(), "")

    def test_proche_de_w_compacte_puis_mesure_suivante_tranche(self):
        self._actif()
        with mock.patch.dict(os.environ, self.ENV):
            _runner, worker = self._worker(resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            rp = worker.resident
            self.assertEqual(rp.profile_window, 1000)
            self.assertEqual(worker._resident_maintenance(), resident_runner.COMPACT)
            self.assertTrue(worker.maybe_rotate())
            self.assertIn("/compact", self._prompts())
            # l'après n'est pas une projection : aucune décision avant la mesure
            self.assertEqual(worker._resident_maintenance(), "")
        # la session a retrouvé de la place : droit à une future compaction
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_CONTEXT": "100",
                                          "FAKE_CLAUDE_WINDOW": "1000"}):
            worker._window_measure = {"session": rp.session, "C_last": 100, "O_last": 7}
            self.assertEqual(worker._resident_maintenance(), "")
            self.assertFalse(rp.window_compact_tried)

    def test_toujours_proche_apres_compaction_renouvelle(self):
        self._actif()
        with mock.patch.dict(os.environ, self.ENV):
            _runner, worker = self._worker(resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            rp = worker.resident
            self.assertTrue(worker.maybe_rotate())
            worker._window_measure = {"session": rp.session, "C_last": 900, "O_last": 7}
            self.assertEqual(worker._resident_maintenance(), resident_runner.WINDOW_RENEW)
            self.assertTrue(worker.maybe_rotate())
            self.assertIsNone(worker.resident)
            self.assertEqual(worker._last_rotation_observation["kind"],
                             "window_maintenance")

    def test_sans_frontiere_renouvelle_sans_boucle(self):
        self._actif()
        with mock.patch.dict(os.environ, {**self.ENV, "FAKE_COMPACT_SKIP": "1"}):
            _runner, worker = self._worker(resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            self.assertFalse(worker.maybe_rotate())
            self.assertEqual(self._prompts().count("/compact"), 1)
            self.assertEqual(worker._resident_maintenance(), resident_runner.WINDOW_RENEW)
            self.assertTrue(worker.maybe_rotate())
            self.assertEqual(self._prompts().count("/compact"), 1)

    def test_politique_jamais_compacte_sans_renouveler(self):
        self._actif()
        with mock.patch.dict(os.environ, {**self.ENV, "FAKE_COMPACT_SKIP": "1"}):
            _runner, worker = self._worker(resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            with mock.patch.object(worker, "session_policy", return_value="jamais"):
                self.assertFalse(worker.maybe_rotate())
                self.assertEqual(self._prompts().count("/compact"), 1)
                self.assertEqual(worker._resident_maintenance(), "")

    def test_w_null_rien_et_inconnue_nommee(self):
        self._actif()
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_CONTEXT": "900"}):
            _runner, worker = self._worker(resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            self.assertEqual(worker._resident_maintenance(), "")
            self.assertFalse(worker.maybe_rotate())
            self.assertNotIn("/compact", self._prompts())
            fenetre = self._context_events()[-1]["decision"]["window"]
            self.assertEqual(fenetre["unknowns"], ["W"])
            self.assertIsNone(fenetre["near"])

    def test_reserves_non_declarees_rien(self):
        self._actif(window_input_reserve=-1.0, window_output_reserve=-1.0)
        with mock.patch.dict(os.environ, self.ENV):
            _runner, worker = self._worker(resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            self.assertEqual(worker._resident_maintenance(), "")
            self.assertEqual(self._context_events()[-1]["decision"]["window"]["unknowns"],
                             ["G_input_reserve", "O_reserve_next"])

    def test_auxiliaire_en_premier_la_fenetre_reste_celle_du_principal(self):
        """R1 : un auxiliaire à 200 k en tête ne fait pas compacter à 180 k."""
        self._actif()
        usage = {"aux-haiku": {"contextWindow": 200_000},
                 "fake-model": {"contextWindow": 1_000_000}}
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_CONTEXT": "180000",
                                          "FAKE_CLAUDE_MODEL_USAGE": json.dumps(usage)}):
            _runner, worker = self._worker(resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            self.assertEqual(worker.resident.profile_window, 1_000_000)
            self.assertEqual(worker._resident_maintenance(), "")

    def test_plafond_dur_passe_avant(self):
        self._actif()
        self.cfg = dataclasses.replace(self.cfg, context_max_tokens=800.0)
        with mock.patch.dict(os.environ, self.ENV):
            _runner, worker = self._worker(resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            self.assertEqual(worker._resident_maintenance(), resident_runner.ROTATE)

    def test_garde_de_securite_passe_avant(self):
        """Pause opérateur : ni tour de compaction, ni renouvellement."""
        self._actif()
        with mock.patch.dict(os.environ, self.ENV):
            _runner, worker = self._worker(resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            with mock.patch.object(worker, "_rotation_allowed", return_value=False):
                self.assertFalse(worker.maybe_rotate())
                worker.resident.window_compact_tried = True
                self.assertFalse(worker.maybe_rotate())
            self.assertNotIn("/compact", self._prompts())

    def test_humain_recent_retient_le_renouvellement(self):
        self._actif()
        with mock.patch.dict(os.environ, self.ENV):
            _runner, worker = self._worker(resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            worker.resident.window_compact_tried = True
            with mock.patch.object(worker, "_humain_recent", return_value=True):
                self.assertFalse(worker.maybe_rotate())
            self.assertIsNotNone(worker.resident)


if __name__ == "__main__":
    unittest.main()
