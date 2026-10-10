# SPDX-License-Identifier: AGPL-3.0-only
"""L60 — rotation de session DeepSeek et plafond de contexte qui agit.

Constat : les tours DeepSeek étaient inscrits sans session (`sessionId` non
lu), avec le modèle « inconnu » (aucun modèle passé ni annoncé), et l'usage par
étape du flux `dsh` était ignoré (taille de session toujours nulle : jamais de
rotation sur la taille). L'alerte `session_too_big` ne faisait qu'alerter.
Postgres réel ; faux harnais `dsh` du banc.
"""
from __future__ import annotations

import dataclasses
import json
import os
import unittest
from unittest import mock

from ameesh import adapters, cost, db as db_mod, exploitation, registry, storage
from ameesh.mesh_cli import parse_token_count
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase

#: un tour qui relit 20 M jetons (deux étapes de 10 M), au-dessus du plafond 15 M
GROS_TOUR = "100000,9900000,500;100000,9900000,700"
#: un tour léger : 2 M relus
PETIT_TOUR = "50000,950000,100;50000,950000,100"


class LectureDuFluxTest(unittest.TestCase):
    def test_usage_par_etape_du_flux_dsh(self):
        lecteur = adapters.DshStream()
        ligne = json.dumps({"type": "status", "phase": "step_end", "turn": 3, "step": 1,
                            "usage": {"inputTokens": 12, "cacheReadTokens": 30,
                                      "outputTokens": 4}})
        self.assertEqual(lecteur.parse(ligne)["usage"]["inputTokens"], 12)
        self.assertEqual(lecteur.parse(json.dumps(
            {"type": "session", "sessionId": "session-x"}))["session"], "session-x")

    def test_usage_normalise_pour_chaque_harnais(self):
        self.assertEqual(adapters.usage_tokens(
            {"input_tokens": 5, "cache_read_input_tokens": 7, "output_tokens": 2}), (5, 7, 2))
        self.assertEqual(adapters.usage_tokens(
            {"input_tokens": 9, "cached_input_tokens": 6, "output_tokens": 1}), (9, 6, 1))
        self.assertEqual(adapters.usage_tokens(
            {"inputTokens": 3, "cacheReadTokens": 8, "outputTokens": 4}), (3, 8, 4))
        self.assertEqual(adapters.usage_tokens({"inputTokens": "x"}), (0, 0, 0))
        self.assertEqual(adapters.usage_tokens(None), (0, 0, 0))

    def test_session_du_flux_lue_en_camel_case(self):
        evenements = [{"type": "session", "sessionId": "session-a"},
                      {"type": "status", "phase": "step_end"},
                      {"type": "session", "sessionId": "session-b"}]
        self.assertEqual(cost._last_session(evenements), "session-b")

    def test_jetons_relus_par_harnais(self):
        ds = cost.TurnUsage("a", "deepseek", "m", 0.0, input_tokens=10,
                            cached_input_tokens=90, output_tokens=1)
        self.assertEqual(ds.reread_tokens, 100)
        # Codex compte déjà le cache dans l'entrée
        cx = cost.TurnUsage("a", "codex", "m", 0.0, input_tokens=100,
                            cached_input_tokens=90, output_tokens=1)
        self.assertEqual(cx.reread_tokens, 100)

    def test_plafond_en_jetons_lisible(self):
        self.assertEqual(parse_token_count("15M"), 15_000_000)
        self.assertEqual(parse_token_count("1.5m"), 1_500_000)
        self.assertEqual(parse_token_count("500k"), 500_000)
        self.assertEqual(parse_token_count("0"), 0)
        self.assertEqual(parse_token_count("20_000_000"), 20_000_000)
        for mauvais in ("", "-1", "abc", "inf", "nan", "3G"):
            with self.assertRaises(ValueError, msg=mauvais):
                parse_token_count(mauvais)


class _Base(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE turn_costs, spend_pending CASCADE")

    def _worker(self, name: str, **cfg_extra):
        cfg = dataclasses.replace(self.cfg, **cfg_extra) if cfg_extra else self.cfg
        runner = Runner(cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "deepseek", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return runner, worker

    #: barème de TEST : le cache de `pro` coûte dix fois celui de `flash`
    BAREME = {"deepseek-flash": [1.0, 0.01, 2.0], "deepseek-pro": [10.0, 0.1, 20.0]}

    def _env(self, usage: str = ""):
        bareme = os.path.join(self.tmp, "prices.json")
        with open(bareme, "w", encoding="utf-8") as fh:
            json.dump(self.BAREME, fh)
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_TEST_DSH_USAGE": usage, "AMEESH_PRICES": bareme}
        return mock.patch.dict(os.environ, env, clear=False)

    def _tour(self, worker, texte: str = "travail") -> bool:
        return worker.run_turn({"kind": "prompt", "prompt": texte, "ids": []})

    def _lignes(self, agent: str) -> list[dict]:
        return self.db.query("SELECT session, model, usd, input_tokens, cached_input_tokens,"
                             " output_tokens FROM turn_costs WHERE agent = %s ORDER BY id",
                             (agent,))


class GrandLivreDeepSeekTest(_Base):
    def test_session_et_modele_du_tour_enregistres(self):
        runner, worker = self._worker("ds-gl")
        with self._env(PETIT_TOUR):
            self.assertTrue(self._tour(worker))
        ligne = self._lignes("ds-gl")[-1]
        self.assertEqual(ligne["session"], "dsh-session-1")
        # le défaut du descripteur est passé explicitement : modèle connu
        self.assertEqual(ligne["model"], "deepseek-flash")
        self.assertEqual((int(ligne["input_tokens"]), int(ligne["cached_input_tokens"])),
                         (100_000, 1_900_000))
        argv = self.turns()[-1]["argv"]
        self.assertIn("--patch", argv)
        with open(argv[argv.index("--patch") + 1], encoding="utf-8") as fh:
            self.assertIn("model: deepseek-flash", fh.read())
        self.assertEqual(worker.last_turn_reread, 2_000_000)
        self.assertEqual(worker.last_turn_reread_session, "dsh-session-1")

    def test_tarif_du_modele_configure_pas_le_maximum(self):
        """Audit du 2026-10-10 : modèle « inconnu » = barème `pro` (le plus
        cher), estimation 12,7 fois au-dessus du solde réel. Le tour est
        facturé au tarif du modèle passé au harnais."""
        runner, worker = self._worker("ds-tarif")
        with self._env(PETIT_TOUR):
            self.assertTrue(self._tour(worker))
        ligne = self._lignes("ds-tarif")[-1]
        flash = (100_000 * 1.0 + 1_900_000 * 0.01 + 200 * 2.0) / 1e6
        self.assertAlmostEqual(float(ligne["usd"]), flash, places=6)
        self.assertLess(float(ligne["usd"]), (100_000 * 10.0 + 1_900_000 * 0.1) / 1e6)

    def test_marqueur_rejoue_n_ecrit_pas_de_doublon(self):
        """Constat du 2026-10-10 (deepseek3/4, 06:04 et 06:05) : l'exécuteur
        arrêté entre l'écriture de la ligne et l'effacement du marqueur ; le
        suivant réparait en réécrivant la même ligne."""
        runner, worker = self._worker("ds-dbl")
        vrai_clear = registry.pending_spend_clear
        appels = []

        def clear_en_panne(db, name):
            appels.append(name)
            if len(appels) == 1:
                raise db_mod.DbError("connexion perdue après l'écriture")
            return vrai_clear(db, name)

        with self._env(PETIT_TOUR), \
                mock.patch.object(registry, "pending_spend_clear", clear_en_panne):
            self.assertTrue(self._tour(worker))
            self.assertTrue(worker._compta_en_echec)
            self.assertIsNotNone(registry.pending_spend_get(self.db, "ds-dbl"))
            # un exécuteur suivant (ou ce worker) répare le marqueur resté
            suivant = AgentWorker(runner, registry.get(self.db, "ds-dbl"),
                                  {"lease_epoch": worker.epoch})
            self.addCleanup(suivant.watchdog_stop.set)
            suivant._compta_repare()
        self.assertEqual(len(self._lignes("ds-dbl")), 1, "une seule ligne pour le tour")
        self.assertIsNone(registry.pending_spend_get(self.db, "ds-dbl"))
        self.assertFalse(suivant._compta_en_echec)

    def test_modele_regle_par_ameesh_set_prime(self):
        runner, worker = self._worker("ds-pro")
        registry.upsert(self.db, "ds-pro", model="deepseek-pro")
        worker.agent = registry.get(self.db, "ds-pro")
        with self._env(PETIT_TOUR):
            self.assertTrue(self._tour(worker))
        self.assertEqual(self._lignes("ds-pro")[-1]["model"], "deepseek-pro")

    def test_alerte_session_trop_grosse_voit_deepseek(self):
        runner, worker = self._worker("ds-al", context_max_tokens=0.0)
        with self._env(GROS_TOUR):
            self.assertTrue(self._tour(worker))
        alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "session_too_big"]
        self.assertEqual([(a["agent"], a["value"], a["session"]) for a in alertes],
                         [("ds-al", 20_000_000, "dsh-session-1")])

    def test_taille_de_session_mesuree_pour_deepseek(self):
        runner, worker = self._worker("ds-taille", session_min_turns=1,
                                      session_max_tokens=150_000.0,
                                      session_max_turn_seconds=1e9)
        with self._env(PETIT_TOUR):
            self.assertTrue(self._tour(worker))
        # entrée non relue + sortie de chaque étape (mesure L11 de la taille)
        self.assertEqual(worker.session_tokens, 100_200.0)
        self.assertFalse(worker.rotation_due())
        with self._env(PETIT_TOUR):
            self.assertTrue(self._tour(worker))
        self.assertTrue(worker.rotation_due(), "la rotation sur la taille s'applique")


class PlafondDeContexteTest(_Base):
    def test_au_dela_du_plafond_la_session_tourne_avec_resume(self):
        runner, worker = self._worker("ds-cap")
        self.assertEqual(worker.context_max_tokens(), 15_000_000)
        with self._env(GROS_TOUR):
            self.assertTrue(self._tour(worker, "premier tour"))
            self.assertEqual(registry.get(self.db, "ds-cap")["session_id"], "dsh-session-1")
            self.assertTrue(worker.context_rotation_due())
            # un seul tour suffit : pas de minimum de tours pour le plafond
            self.assertLess(worker.session_turns, self.cfg.session_min_turns)
            with mock.patch.object(worker, "_fil_note") as note:
                self.assertTrue(worker.maybe_rotate())
        self.assertTrue(any("plafond de contexte" in str(appel) for appel in note.call_args_list),
                        "la rotation est tracée dans le fil avec sa raison")
        self.assertIsNone(registry.get(self.db, "ds-cap")["session_id"])
        self.assertTrue(worker.resume_summary)
        self.assertEqual(worker.last_turn_reread, 0)
        self.assertFalse(worker.context_rotation_due())
        historique = os.path.join(worker.state_dir, "session-history.jsonl")
        with open(historique, encoding="utf-8") as fh:
            self.assertEqual(json.loads(fh.readline())["session"], "dsh-session-1")
        # le résumé a été demandé DANS l'ancienne session
        resume_argv = self.turns()[-1]["argv"]
        self.assertIn("dsh-session-1", resume_argv)
        self.assertIn(adapters.SUMMARY_PROMPT, " ".join(resume_argv))
        with self._env(PETIT_TOUR):
            self.assertTrue(self._tour(worker, "tour suivant"))
        argv = self.turns()[-1]["argv"]
        self.assertNotIn("--session-id", argv, "session neuve")
        self.assertIn("Reprise de session après rotation", " ".join(argv))

    def test_sous_le_plafond_rien_ne_tourne(self):
        runner, worker = self._worker("ds-sous")
        with self._env(PETIT_TOUR):
            self.assertTrue(self._tour(worker))
            self.assertFalse(worker.context_rotation_due())
            self.assertFalse(worker.maybe_rotate())
        self.assertEqual(registry.get(self.db, "ds-sous")["session_id"], "dsh-session-1")

    def test_reglage_par_agent(self):
        runner, worker = self._worker("ds-reg")
        with self._env(PETIT_TOUR):
            self.assertTrue(self._tour(worker))
        self.assertFalse(worker.context_rotation_due())
        # plafond abaissé pour cet agent : 2 M relus suffisent
        proc = self.mesh("set", "ds-reg", "context_max_tokens=1.5M")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("contexte=1500000", proc.stdout)
        worker.agent = registry.get(self.db, "ds-reg")
        self.assertEqual(worker.context_max_tokens(), 1_500_000)
        self.assertTrue(worker.context_rotation_due())
        # 0 = désactivé
        self.assertEqual(self.mesh("set", "ds-reg", "context_max_tokens=0").returncode, 0)
        worker.agent = registry.get(self.db, "ds-reg")
        self.assertFalse(worker.context_rotation_due())
        # vide = défaut de l'exécuteur ; valeur illisible refusée
        self.assertEqual(self.mesh("set", "ds-reg", "context_max_tokens=").returncode, 0)
        self.assertIsNone(registry.get(self.db, "ds-reg")["context_max_tokens"])
        self.assertEqual(self.mesh("set", "ds-reg", "context_max_tokens=beaucoup").returncode, 2)
        self.assertEqual(self.mesh("set", "ds-reg", "context_max_tokens=-3").returncode, 2)

    def test_list_json_rend_le_plafond(self):
        runner, worker = self._worker("ds-list")
        self.assertEqual(self.mesh("set", "ds-list", "context_max_tokens=20M").returncode, 0)
        proc = self.mesh("list", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = next(r for r in json.loads(proc.stdout) if r["name"] == "ds-list")
        self.assertEqual((row["context_max_tokens"], row["context_max_tokens_set"]),
                         (20_000_000, 20_000_000))

    def test_politique_jamais_s_en_dispense(self):
        runner, worker = self._worker("ds-jamais")
        storage.of(self.db).operations.set_settings("ds-jamais", {"session_policy": "jamais"})
        worker.agent = registry.get(self.db, "ds-jamais")
        with self._env(GROS_TOUR):
            self.assertTrue(self._tour(worker))
            self.assertFalse(worker.context_rotation_due())
            self.assertFalse(worker.maybe_rotate())

    def test_releve_d_une_autre_session_ignore(self):
        runner, worker = self._worker("ds-autre")
        with self._env(GROS_TOUR):
            self.assertTrue(self._tour(worker))
        registry.set_session(self.db, "ds-autre", "session-du-remplacant")
        worker.agent = registry.get(self.db, "ds-autre")
        self.assertFalse(worker.context_rotation_due())

    def test_resume_en_echec_garde_la_session(self):
        runner, worker = self._worker("ds-echec")
        with self._env(GROS_TOUR):
            self.assertTrue(self._tour(worker))
        with mock.patch.object(worker, "run_turn", return_value=False):
            self.assertFalse(worker.maybe_rotate())
        self.assertEqual(registry.get(self.db, "ds-echec")["session_id"], "dsh-session-1")


if __name__ == "__main__":
    unittest.main()
