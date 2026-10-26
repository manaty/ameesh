# SPDX-License-Identifier: AGPL-3.0-only
"""#3733 : un agent bridé de façon chronique par le débit du fournisseur lève
`rate_limited_chronic`, même quand chaque épisode reste sous `PERSIST_S`.

Incident du 26/10 : codex1, relecteur d'un lot P1, a fait trois épisodes de
429 en 45 min (10:02-10:06, 10:25-10:29, 10:46 →), chacun effacé par un
succès avant les 15 min de #1270 ; aucune alerte. L'exécuteur garde les
épisodes clos de l'heure glissante et écrit leur cumul en suffixe de
`status_text` ; un succès ne le retire pas.
"""
from __future__ import annotations

import os
import time
import unittest

from ameesh import alert_briefs, exploitation, notify, rate_limit, registry
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase

H = rate_limit.WINDOW_S


class FenetreTest(unittest.TestCase):
    def test_le_cumul_coupe_les_episodes_au_bord_de_la_fenetre(self):
        now = 10_000.0
        clos = [(now - H - 300, now - H + 120), (now - 900, now - 600)]
        self.assertAlmostEqual(rate_limit.window_total(clos, now), 120 + 300)
        self.assertAlmostEqual(rate_limit.window_total(clos, now, now - 60), 480)

    def test_un_episode_sorti_de_la_fenetre_est_oublie(self):
        now = 10_000.0
        clos = [(now - H - 300, now - H - 1), (now - 900, now - 600)]
        self.assertEqual(rate_limit.window_prune(clos, now), [(now - 900, now - 600)])

    def test_la_fenetre_est_bornee(self):
        now = 10_000.0
        clos = [(now - 100 - i, now - 100 - i + 0.5) for i in range(100, 0, -1)]
        self.assertEqual(len(rate_limit.window_prune(clos, now)), rate_limit.WINDOW_MAX)

    def test_le_marqueur_se_relit(self):
        since, at = 1_793_000_000.0, 1_793_002_000.0
        texte = rate_limit.window_text("mail in 12s", 960.4, 3, since, at)
        self.assertTrue(texte.startswith("mail in 12s ["))
        self.assertEqual(rate_limit.window_state(texte),
                         {"total": 960.0, "episodes": 3, "since": since, "at": at})
        # le marqueur #1270 de l'épisode en cours n'est pas confondu
        self.assertIsNone(rate_limit.state(texte))

    def test_sans_cumul_le_statut_reste_tel_quel(self):
        self.assertEqual(rate_limit.window_text("mail in 12s", 0.4, 1, 1.0, 2.0),
                         "mail in 12s")
        self.assertIsNone(rate_limit.window_state("mail in 12s"))

    def test_l_alerte_voit_l_episode_en_cours_et_oublie_une_vieille_mesure(self):
        at = 1_793_002_000.0
        fenetre = {"total": 600.0, "episodes": 2, "since": at - 1800, "at": at}
        debit = {"kind": rate_limit.RATE_LIMITED, "streak": 2, "since": at - 120,
                 "wait": 120.0, "retry_at": at + 120}
        self.assertEqual(rate_limit.window_seen(fenetre, None, now=at + 300), 600.0)
        self.assertEqual(rate_limit.window_seen(fenetre, debit, now=at + 300), 900.0)
        quota = dict(debit, kind=rate_limit.QUOTA)
        self.assertEqual(rate_limit.window_seen(fenetre, quota, now=at + 300), 600.0)
        self.assertEqual(rate_limit.window_seen(fenetre, debit, now=at + H), 0.0)

    def test_la_relecture_ne_compte_pas_deux_fois_l_episode_en_cours(self):
        at = 1_793_002_000.0
        fenetre = {"total": 600.0, "episodes": 2, "since": at - 1800, "at": at}
        debit = {"kind": rate_limit.RATE_LIMITED, "streak": 2, "since": at - 120,
                 "wait": 120.0, "retry_at": at + 120}
        clos, since = rate_limit.window_restore(fenetre, debit, now=at + 10)
        self.assertEqual(clos, [(at - 480, at)])
        self.assertEqual(since, at - 1800)
        self.assertAlmostEqual(rate_limit.window_total(clos, at, debit["since"]), 600.0)
        self.assertEqual(rate_limit.window_restore(fenetre, debit, now=at + H), ([], 0.0))


class AlerteTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "relecteur", harness="codex", host=self.cfg.host)

    def _alertes(self):
        return [a for a in exploitation.alerts(self.cfg, self.db)
                if a["type"] == "rate_limited_chronic"]

    def _statut(self, total: float, since: float, at: float, base="mail in 30s",
                error=None) -> None:
        registry.set_status(self.db, "relecteur", "idle",
                            status_text=rate_limit.window_text(base, total, 3, since, at),
                            error=error)

    def test_un_cumul_d_un_quart_d_heure_leve_l_alerte_apres_un_succes(self):
        now = time.time()
        self._statut(960, now - 2700, now - 30)
        [alerte] = self._alertes()
        self.assertEqual(alerte["agent"], "relecteur")
        self.assertEqual(alerte["value"], 960)
        self.assertEqual(alerte["threshold"], int(rate_limit.CHRONIC_S))
        self.assertEqual(alerte["episodes"], 3)
        self.assertAlmostEqual(alerte["since"], now - 2700, delta=1.0)
        self.assertIn("16 min", alerte["detail"])

    def test_sous_le_seuil_rien(self):
        now = time.time()
        self._statut(600, now - 2700, now - 30)
        self.assertEqual(self._alertes(), [])

    def test_une_mesure_de_plus_d_une_heure_ne_compte_plus(self):
        now = time.time()
        self._statut(1800, now - 2 * H, now - H - 5)
        self.assertEqual(self._alertes(), [])

    def test_l_attente_en_cours_complete_le_cumul(self):
        # #1270 : pendant l'attente de reprise, la ligne n'est pas réécrite ;
        # le temps écoulé depuis la mesure compte.
        now = time.time()
        base = rate_limit.status_text("mail in 4s (failed)", rate_limit.RATE_LIMITED, 2,
                                      now - 400, 120.0, retry_at=now + 60)
        self._statut(700, now - 2000, now - 250, base=base,
                     error="exceeded retry limit, last status: 429 Too Many Requests")
        [alerte] = self._alertes()
        self.assertGreaterEqual(alerte["value"], 949)
        self.assertIn("429 Too Many Requests", alerte["detail"])

    def test_la_cle_ne_glisse_pas_avec_la_fenetre(self):
        now = time.time()
        since = now - 3000
        cles = []
        for total, at in ((950, now - 600), (1100, now - 20)):
            self._statut(total, since, at)
            [alerte] = self._alertes()
            cles.append(exploitation.alert_key(alerte))
        self.assertEqual(cles[0], cles[1])

    def test_briefee_aux_pilotes_et_a_l_auditeur_pas_poussee_aux_humains(self):
        self.assertIn("rate_limited_chronic", exploitation.ALERT_TYPES)
        self.assertIn("rate_limited_chronic", notify.TYPE_LABELS)
        self.assertNotIn("rate_limited_chronic", notify.DEFAULT_TYPES)
        self.assertIn("rate_limited_chronic", alert_briefs.BRIEFED_TYPES)
        alerte = {"type": "rate_limited_chronic", "agent": "relecteur",
                  "orchestrators": ["orch"],
                  "detail": "relecteur rate-limited 16 min over the last hour"}
        listing = [{"name": "relecteur", "team": "nexlink"},
                   {"name": "orch", "team": "nexlink"},
                   {"name": "auditeur", "team": "ameesh"}]
        out = alert_briefs.briefs(alerte, listing, auditor_names=["auditeur"],
                                  orchestrator_names=("orch",))
        self.assertIn("orch", out)
        self.assertIn("auditeur", out)
        self.assertNotIn("relecteur", out)
        self.assertIn("ameesh work pending-reviews relecteur", out["orch"])


class ExecuteurTest(PgTestCase):
    """L'exécuteur clôt les épisodes, garde le cumul après un succès et le
    relit au redémarrage."""

    def _worker(self, status_text: str = "") -> AgentWorker:
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", "relecteur")
        os.makedirs(cwd, exist_ok=True)
        self.register("relecteur", "codex", cwd=cwd)
        registry.claim(self.db, "relecteur", runner.runner_id, 3600)
        registry.set_status(self.db, "relecteur", "idle", status_text=status_text)
        worker = AgentWorker(runner, registry.get(self.db, "relecteur"),
                             registry.claim(self.db, "relecteur", runner.runner_id, 3600))
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def test_un_succes_garde_le_cumul_des_episodes_clos(self):
        worker = self._worker()
        now = time.time()
        for debut, fin in ((now - 2600, now - 2330), (now - 1300, now - 1030)):
            worker.rate_limit, worker.rate_limit_since = rate_limit.RATE_LIMITED, debut
            worker._clos_episode_debit(fin)
            worker.rate_limit, worker.rate_limit_since = None, 0.0
        worker.rate_limit, worker.rate_limit_since = rate_limit.RATE_LIMITED, now - 400
        texte = worker._marqueurs_durables("mail turn (codex)")
        fenetre = rate_limit.window_state(texte)
        self.assertEqual(fenetre["episodes"], 3)
        self.assertAlmostEqual(fenetre["total"], 940, delta=2)
        self.assertAlmostEqual(fenetre["since"], now - 2600, delta=1.0)
        # le succès clôt l'épisode en cours ; le cumul reste sur la ligne
        worker._clos_episode_debit(now - 5)
        worker.rate_limit, worker.rate_limit_since = None, 0.0
        fenetre = rate_limit.window_state(worker._fenetre_debit("mail in 30s"))
        self.assertAlmostEqual(fenetre["total"], 935, delta=2)
        self.assertAlmostEqual(fenetre["since"], now - 2600, delta=1.0)

    def test_un_quota_n_entre_pas_dans_la_fenetre(self):
        worker = self._worker()
        worker.rate_limit, worker.rate_limit_since = rate_limit.QUOTA, time.time() - 600
        worker._clos_episode_debit(time.time())
        self.assertEqual(worker.rate_limit_window, [])
        self.assertEqual(worker._fenetre_debit("mail in 3s"), "mail in 3s")

    def test_la_serie_se_referme_quand_la_fenetre_se_vide(self):
        worker = self._worker()
        now = time.time()
        worker.rate_limit_window = [(now - H - 600, now - H - 10)]
        worker.rate_limit_window_since = now - H - 600
        self.assertEqual(worker._fenetre_debit("mail in 3s"), "mail in 3s")
        self.assertEqual(worker.rate_limit_window_since, 0.0)

    def test_le_cumul_est_relu_au_redemarrage(self):
        now = time.time()
        worker = self._worker(rate_limit.window_text("mail in 30s", 960, 3,
                                                     now - 2700, now - 60))
        self.assertAlmostEqual(worker.rate_limit_window_since, now - 2700, delta=1.0)
        fenetre = rate_limit.window_state(worker._fenetre_debit("mail in 3s"))
        self.assertAlmostEqual(fenetre["total"], 960, delta=2)
        self.assertAlmostEqual(fenetre["since"], now - 2700, delta=1.0)


if __name__ == "__main__":
    unittest.main()
