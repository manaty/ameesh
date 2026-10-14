# SPDX-License-Identifier: AGPL-3.0-only
"""#1268 : deux échecs de tour **réels** d'un agent critique lèvent l'alerte
poussée `critical_turn_failures` ; et un modèle d'une autre famille que le
harnais du tour n'est jamais passé au lancement.

Incident du 14/10 : orchestrateur Codex avec `model=claude-opus-5-5` (ligne de
registre héritée d'une bascule, #1037) — 400 côté fournisseur, échecs répétés
sans alerte. Le marqueur de série est écrit par l'exécuteur dans `status_text`
(visible dans `ameesh list`/`show` et le bilan de progression) et relu par
l'alerte ; il disparaît au premier tour réussi.
"""
from __future__ import annotations

import os
import time
import unittest
from types import SimpleNamespace

from ameesh import cost, exploitation, notify, registry
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase
from .test_l30_accounts import _Base


class FamilleDeModeleTest(unittest.TestCase):
    def test_un_modele_d_une_autre_famille_est_reconnu(self):
        self.assertTrue(cost.foreign_harness_model("codex", "claude-opus-5-5"))
        self.assertTrue(cost.foreign_harness_model("claude", "gpt-6.1-sol"))
        self.assertFalse(cost.foreign_harness_model("codex", "gpt-6.1-sol"))
        self.assertFalse(cost.foreign_harness_model("claude", "claude-opus-5-5"))

    def test_un_modele_inconnu_n_est_jamais_refuse(self):
        # on ne refuse que sur une famille positivement reconnue : ni vide, ni
        # identifiant inconnu ne sont écartés (jamais une supposition)
        self.assertFalse(cost.foreign_harness_model("codex", ""))
        self.assertFalse(cost.foreign_harness_model("codex", "mystere-1"))


class MarqueurTest(unittest.TestCase):
    def test_le_marqueur_critique_porte_la_serie(self):
        texte = exploitation.turn_failure_status_text(
            "mail in 3s (failed)", 2, critical=True)
        self.assertEqual(texte, "critical failures x2: mail in 3s (failed)")
        self.assertEqual(exploitation.critical_failure_streak(texte), 2)
        self.assertEqual(exploitation.failure_streak(texte), 2)

    def test_un_succes_ou_une_pause_budget_ne_portent_pas_de_marqueur(self):
        for texte in ("mail in 30s", "budget : pause (account a, five_hour 96 %)",
                      "hôte non prêt", "auth_blocked: codex/a", ""):
            with self.subTest(texte=texte):
                self.assertEqual(exploitation.failure_streak(texte), 0)
                self.assertEqual(exploitation.critical_failure_streak(texte), 0)

    def test_un_agent_non_critique_garde_la_trace_sans_alerte(self):
        texte = exploitation.turn_failure_status_text("events in 4s (failed)", 3,
                                                      critical=False)
        self.assertEqual(exploitation.failure_streak(texte), 3)
        self.assertEqual(exploitation.critical_failure_streak(texte), 0)

    def test_le_marqueur_porte_le_debut_d_episode(self):
        # #1268 B1 : `since` est écrit dans le marqueur et relu à l'identique ;
        # il ne dépend pas de `status_since_ts` (réécrit à chaque tentative).
        since = time.time() - 120
        texte = exploitation.turn_failure_status_text(
            "mail in 3s (failed)", 2, critical=True, since=since)
        self.assertTrue(texte.startswith("critical failures x2 since "))
        self.assertEqual(exploitation.critical_failure_streak(texte), 2)
        self.assertEqual(exploitation.failure_streak(texte), 2)
        self.assertAlmostEqual(exploitation.failure_since(texte), since, delta=1.0)
        self.assertAlmostEqual(exploitation.critical_failure_since(texte), since,
                               delta=1.0)


class LancementTest(PgTestCase):
    """Le modèle du tour : réglage de l'agent, jamais celui d'un autre harnais."""

    def _worker(self, name: str, harness: str, model: str | None = None) -> AgentWorker:
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, harness, cwd=cwd, model=model)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def test_le_modele_etranger_est_ignore_au_lancement(self):
        # la ligne réelle de l'incident : harness=codex, model=claude-opus-5-5
        worker = self._worker("orch", "codex", model="claude-opus-5-5")
        adapter = SimpleNamespace(spec=SimpleNamespace(model_file=None, defaults={}))
        self.assertEqual(worker.modele_du_tour("codex", adapter, emprunte=False), "")
        # le même réglage sous son propre harnais part bien
        worker2 = self._worker("orch2", "claude", model="claude-opus-5-5")
        self.assertEqual(worker2.modele_du_tour("claude", adapter, emprunte=False),
                         "claude-opus-5-5")
        # harnais emprunté (L242b) : jamais le modèle de l'agent non plus
        self.assertEqual(worker2.modele_du_tour("codex", adapter, emprunte=True), "")

    def test_le_defaut_du_descripteur_reste_utilise_sans_reglage(self):
        worker = self._worker("ds", "deepseek")
        adapter = SimpleNamespace(spec=SimpleNamespace(
            model_file={"path": "model.patch.yml"}, defaults={"model": "deepseek-chat"}))
        self.assertEqual(worker.modele_du_tour("deepseek", adapter, emprunte=False),
                         "deepseek-chat")


class AlerteTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "orch", harness="codex", host=self.cfg.host)

    def _alertes(self):
        return [a for a in exploitation.alerts(self.cfg, self.db)
                if a["type"] == "critical_turn_failures"]

    def test_deux_echecs_reels_levent_l_alerte_avec_le_code_du_fournisseur(self):
        registry.set_status(
            self.db, "orch", "blocked",
            status_text=exploitation.turn_failure_status_text(
                "mail in 3s (failed)", 2, critical=True),
            error="codex exited 1: HTTP 400 model claude-opus-5-5")
        [alerte] = self._alertes()
        self.assertEqual(alerte["agent"], "orch")
        self.assertEqual(alerte["value"], 2)
        self.assertIn("HTTP 400", alerte["detail"])
        self.assertIn("critical_turn_failures", notify.DEFAULT_TYPES)
        self.assertIn("critical_turn_failures", notify.TYPE_LABELS)
        self.assertIn("critical_turn_failures", notify.URGENT_TYPES)

    def test_un_seul_echec_ne_leve_pas_l_alerte(self):
        registry.set_status(
            self.db, "orch", "blocked",
            status_text=exploitation.turn_failure_status_text(
                "mail in 3s (failed)", 1, critical=True),
            error="codex exited 1: HTTP 400")
        self.assertEqual(self._alertes(), [])

    def test_un_agent_non_critique_ne_leve_pas_l_alerte(self):
        registry.set_status(
            self.db, "orch", "blocked",
            status_text=exploitation.turn_failure_status_text(
                "events in 4s (failed)", 4, critical=False))
        self.assertEqual(self._alertes(), [])

    def test_un_succes_resout_l_alerte(self):
        registry.set_status(
            self.db, "orch", "blocked",
            status_text=exploitation.turn_failure_status_text(
                "mail in 3s (failed)", 2, critical=True))
        self.assertEqual(len(self._alertes()), 1)
        registry.set_status(self.db, "orch", "idle", status_text="mail in 20s")
        self.assertEqual(self._alertes(), [])

    def test_la_cle_d_alerte_est_le_debut_d_episode(self):
        # #1268 B1 : entre deux tentatives, `status_since_ts` change mais la
        # clé de l'alerte (début d'épisode marqué) reste la même — pas de
        # fausse résolution/relève.
        since = time.time() - 300
        cles = []
        for _ in range(2):
            registry.set_status(
                self.db, "orch", "blocked",
                status_text=exploitation.turn_failure_status_text(
                    "mail in 3s (failed)", 2, critical=True, since=since),
                error="codex exited 1: HTTP 400 model claude-opus-5-5")
            [alerte] = self._alertes()
            cles.append(alerte["since"])
        self.assertEqual(cles[0], cles[1])
        self.assertAlmostEqual(cles[0], since, delta=1.0)


class SerieTest(PgTestCase):
    """La série vit dans la ligne : elle survit au redémarrage de l'exécuteur."""

    def _worker(self, status_text: str) -> AgentWorker:
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", "orch")
        os.makedirs(cwd, exist_ok=True)
        self.register("orch", "claude", cwd=cwd)
        registry.claim(self.db, "orch", runner.runner_id, 3600)
        registry.set_status(self.db, "orch", "blocked", status_text=status_text)
        worker = AgentWorker(runner, registry.get(self.db, "orch"),
                             registry.claim(self.db, "orch", runner.runner_id, 3600))
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def test_la_serie_est_relue_a_la_construction(self):
        worker = self._worker(exploitation.turn_failure_status_text(
            "mail in 3s (failed)", 3, critical=True))
        self.assertEqual(worker.failure_streak, 3)

    def test_le_backoff_suit_la_serie_reelle_meme_lente(self):
        worker = self._worker(exploitation.turn_failure_status_text(
            "mail in 3s (failed)", 3, critical=True))
        # pas d'échec « rapide » : la série réelle compte quand même
        self.assertEqual(worker.fast_failures, 0)
        attendu = min(worker.runner.failure_backoff_max,
                      worker.runner.failure_backoff * 4)
        self.assertAlmostEqual(worker.failure_wait(), attendu, places=3)

    def test_le_debut_d_episode_est_relu(self):
        since = time.time() - 600
        worker = self._worker(exploitation.turn_failure_status_text(
            "mail in 3s (failed)", 3, critical=True, since=since))
        self.assertEqual(worker.failure_streak, 3)
        self.assertAlmostEqual(worker.failure_since, since, delta=1.0)

    def test_begin_turn_preserve_la_serie(self):
        # #1268 B1 : le statut d'un tour en cours garde le marqueur et son
        # début d'épisode — un redémarrage pendant la tentative ne perd pas la
        # série, et la clé d'alerte ne bouge pas.
        since = time.time() - 600
        worker = self._worker(exploitation.turn_failure_status_text(
            "mail in 3s (failed)", 3, critical=True, since=since))
        texte = worker._marqueurs_durables("mail turn (claude)")
        self.assertEqual(exploitation.failure_streak(texte), 3)
        self.assertAlmostEqual(exploitation.failure_since(texte), since, delta=1.0)
        self.assertIn("mail turn (claude)", texte)


class SessionFilleTest(_Base):
    """#1268 B2 : le refus d'une session fille ne compte pas dans la série."""

    FILLE = "bc9d3965-d6c7-4e90-a482-96296b7beaa6"

    def test_un_refus_de_session_fille_ne_compte_pas(self):
        _, worker = self._worker("ds-fille", "deepseek", {})
        try:
            # premier échec réel : la série monte à 1
            self.assertIsNotNone(self._tour(worker, AMEESH_TEST_EXIT="1"))
            self.assertEqual(worker.failure_streak, 1)
            # le refus d'une session fille (L166) ne la fait pas monter à 2
            registry.set_session(self.db, "ds-fille", self.FILLE)
            worker.agent = registry.get(self.db, "ds-fille")
            self.assertIsNotNone(self._tour(
                worker, AMEESH_TEST_DSH_CHILD_SESSIONS=self.FILLE))
            self.assertFalse(worker.fast_failure)
            self.assertEqual(worker.failure_streak, 1)
            ligne = registry.get(self.db, "ds-fille")
            self.assertEqual(exploitation.failure_streak(ligne["status_text"]), 1)
        finally:
            worker.watchdog_stop.set()


if __name__ == "__main__":
    unittest.main()
