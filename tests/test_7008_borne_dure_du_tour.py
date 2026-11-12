# SPDX-License-Identifier: AGPL-3.0-only
"""#7008 — la durée maximale d'un tour a une borne dure, hors du flux.

Constat : tour 20a029f9 de l'orchestrateur Nexlink (AG1, 11/11), transcript
figé sur « end of turn » à 18:21Z, tour « running » jusqu'au redémarrage
manuel vers 19:33Z (4 368 s pour un plafond de 1 800 s). Le plafond L105 se
lit à chaque ligne du harnais et ferme au point sûr ; un harnais muet n'en
donne jamais. Désormais, au-delà du plafond + marge, l'exécuteur arrête le
groupe du harnais lui-même et solde le tour.
Postgres réel ; faux harnais `claude` du banc.
"""
from __future__ import annotations

import os
import time
from unittest import mock

from ameesh import registry, runner as runner_mod

from .test_l105_plafonds_du_tour import _Base


class BorneDureTest(_Base):
    def _env_bloque(self, **extra):
        env = {"AMEESH_TEST_HANG_AFTER_RESULT": "120"}
        env.update(extra)
        return mock.patch.dict(os.environ, env, clear=False)

    def _marge(self, secondes: float):
        return mock.patch.multiple(runner_mod, TURN_HARD_MARGIN_MIN_S=secondes,
                                   TURN_HARD_MARGIN_RATIO=0.0)

    def _tour_bloque(self, name: str, **extra):
        _runner, worker = self._worker(name, turn_max_seconds=1)
        debut = time.monotonic()
        with self._env(), self._env_bloque(**extra), self._marge(1.0):
            self.assertEqual(worker.turn_hard_limit_seconds(), 2.0)
            self.assertTrue(worker.run_turn({"kind": "prompt", "prompt": "x",
                                             "ids": []}))
        return worker, time.monotonic() - debut

    def test_harnais_muet_apres_sa_fin_de_tour_clos_a_la_borne(self):
        worker, duree = self._tour_bloque("orch-muet")
        # borne 2 s, grâce SIGTERM 3 s : loin des 120 s du sommeil
        self.assertLess(duree, 30)
        self.assertTrue(worker.last_turn_closed.startswith("maximum turn duration"))
        self.assertIn("hard limit", worker.last_turn_closed)
        agent = registry.get(self.db, "orch-muet")
        self.assertEqual(agent["status"], "idle")
        self.assertIn("closed: maximum turn duration", agent["status_text"])
        # comptabilité soldée : la ligne du grand livre est écrite
        rows = self.db.query("select count(*)::int as n from turn_costs"
                             " where agent = 'orch-muet'")
        self.assertEqual(rows[0]["n"], 1)
        self.assertEqual(worker.suite, worker.last_turn_closed)

    def test_sigterm_ignore_puis_sigkill_du_groupe(self):
        worker, duree = self._tour_bloque("orch-sourd", AMEESH_TEST_IGNORE_TERM="1")
        self.assertLess(duree, 30)
        self.assertIn("hard limit", worker.last_turn_closed or "")
        self.assertIsNone(worker.proc)

    def test_borne_plafond_plus_marge(self):
        _runner, worker = self._worker("orch-borne", turn_max_seconds=1800)
        self.assertEqual(worker.turn_hard_limit_seconds(), 2700.0)
        _runner, worker = self._worker("orch-court", turn_max_seconds=60)
        self.assertEqual(worker.turn_hard_limit_seconds(),
                         60 + runner_mod.TURN_HARD_MARGIN_MIN_S)
        _runner, worker = self._worker("orch-sans", turn_max_seconds=0)
        self.assertEqual(worker.turn_hard_limit_seconds(), 0.0)

    def test_tour_qui_finit_seul_n_est_pas_clos(self):
        _runner, worker = self._worker("orch-normal", turn_max_seconds=1)
        with self._env(), self._marge(1.0):
            self.assertTrue(worker.run_turn({"kind": "prompt", "prompt": "x",
                                             "ids": []}))
        self.assertIsNone(worker.last_turn_closed)
        # le moniteur rendu à la fin du tour ne clôt pas le tour suivant
        time.sleep(2.5)
        self.assertIsNone(worker._turn_hard_closed)
