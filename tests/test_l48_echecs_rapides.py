# SPDX-License-Identifier: AGPL-3.0-only
"""L48 : une série d'échecs rapides (harnais introuvable, qui sort aussitôt)
n'est plus relancée toutes les 5 s sans fin : attente doublée à chaque échec,
puis arrêt de l'agent (`stop_reason` = erreur) au-delà du seuil."""
from __future__ import annotations

import dataclasses
import os
import time
from unittest import mock

from ameesh import registry
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase


class EchecsRapidesTest(PgTestCase):
    def _worker(self, name="rapide", **cfg):
        os.makedirs(os.path.join(self.tmp, "work", name), exist_ok=True)
        self.register(name, "claude", cwd=os.path.join(self.tmp, "work", name), prompt="tour")
        runner = Runner(dataclasses.replace(self.cfg, **cfg), self.db, once=False)
        agent = registry.get(self.db, name)
        return AgentWorker(runner, agent, {"lease_epoch": 1,
                                           "lease_expires_ts": time.time() + 3600})

    def test_attente_doublee_et_bornee(self):
        worker = self._worker(failure_backoff_max=30.0)
        attentes = []
        for n in range(1, 7):
            worker.fast_failures = n
            attentes.append(worker.failure_wait())
        self.assertEqual(attentes, [5.0, 10.0, 20.0, 30.0, 30.0, 30.0])

    def _boucle(self, worker, resultats):
        """Fait tourner `run()` sur une suite de tours simulés (True = réussi,
        False = échec rapide, None = échec lent), sans attente réelle."""
        suite = iter(resultats)

        def tour(_spec):
            r = next(suite, "fin")
            if r == "fin":
                worker.stopping.set()
                return True
            worker.fast_failure = r is False
            return bool(r)

        attentes = []
        with mock.patch.object(worker, "renew", return_value=True), \
                mock.patch.object(worker, "pick", return_value={"kind": "consigne"}), \
                mock.patch.object(worker, "rotate_for_lot", side_effect=lambda s: s), \
                mock.patch.object(worker, "maybe_rotate"), \
                mock.patch.object(worker, "apply_restart_if_requested"), \
                mock.patch.object(worker, "ensure_watchdog"), \
                mock.patch.object(worker, "release_lease"), \
                mock.patch.object(worker, "run_turn", side_effect=tour), \
                mock.patch.object(worker.wake, "wait",
                                  side_effect=lambda timeout=None: attentes.append(timeout)):
            worker.run()
        return attentes

    def test_serie_d_echecs_rapides_arrete_l_agent(self):
        worker = self._worker(max_fast_failures=3)
        attentes = self._boucle(worker, [False, False, False, False])
        self.assertEqual(attentes, [5.0, 10.0])  # pas d'attente après l'arrêt
        row = registry.get(self.db, "rapide")
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(row["stop_reason"], "erreur")
        self.assertIn("3 échecs rapides", row["status_text"])
        self.assertEqual(row["pending_prompt"], "tour")  # la consigne reste en attente

    def test_un_succes_remet_la_serie_a_zero(self):
        worker = self._worker(max_fast_failures=3)
        attentes = self._boucle(worker, [False, False, True, False, False, True])
        self.assertEqual(attentes, [5.0, 10.0, 5.0, 10.0])
        self.assertNotEqual(registry.get(self.db, "rapide")["status"], "stopped")

    def test_echec_lent_ne_compte_pas(self):
        worker = self._worker(max_fast_failures=2)
        self._boucle(worker, [None, None, None, False])
        self.assertNotEqual(registry.get(self.db, "rapide")["status"], "stopped")
