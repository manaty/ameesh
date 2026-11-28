# SPDX-License-Identifier: AGPL-3.0-only
"""#8423 : le réveil « idle with actionable lot » lance le tour du lot.

Constat (AG2, 28/11) : le réveil comptait (`note_wake`), ouvrait la session
du lot, puis le tour rendait la main avant le harnais (pression de l'hôte
relue au lancement : `_host_pressure_hold` ne dit rien sans consigne ni
courrier) ; aucune ligne, et le délai doublé repoussait le réveil suivant —
seul un courrier démarrait le lot. Le tour non lancé est maintenant dit, et
le réveil rejoué sans être recompté. Postgres réel ; faux harnais du banc."""
from __future__ import annotations

import os
import time
from unittest import mock

from ameesh import registry, work
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN
from .test_l165c_sessions_par_lot import _FORCE, _Base

PRESSION = {"blocked": True, "critical": False,
            "breaches": [{"metric": "load", "value": 40.0, "limit": 12.0}]}


class ReveilLanceLeTourTest(_Base):
    def _worker(self, name):
        runner = Runner(self.cfg, self.db, once=True)
        self.register(name, "claude", cwd=self._cwd(name))
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        worker.last_activity = time.monotonic() - 10_000   # au repos
        worker.nudged = True
        self.addCleanup(worker.watchdog_stop.set)
        self.addCleanup(worker._forget_wake_claim)
        return worker

    def _env(self):
        return mock.patch.dict(os.environ, {
            "AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
            "AMEESH_SESSION_STORE_CHECK": "0", "AMEESH_TEST_NEW_SESSION": "sess-8423"})

    def choix(self, worker):
        worker._lot_wake_checked = float("-inf")
        worker._scheduled_wake_checked = float("-inf")
        return worker._pick_for_turn()

    def dette(self, name):
        """#8458 : la dette de réveil vit en base, par lot."""
        return {str(r["id"]): r for r in self.st.pending_lot_wakes(name)}

    def tours(self) -> int:
        return len(self.turns()) if os.path.exists(self.turns_log) else 0

    def _lot(self, name):
        return work.add(self.db, title="à faire", assignee=name, force=_FORCE)["id"]

    def _refuse_par_pression(self, worker, spec):
        journal = []
        with self._env(), \
                mock.patch.object(worker.runner, "host_pressure", return_value=PRESSION), \
                mock.patch("ameesh.runner.log_async", side_effect=journal.append):
            self.assertFalse(worker.run_turn(spec))
        return journal

    def test_temoin_reveil_sans_courrier_lance_le_tour(self):
        worker = self._worker("t8423a")
        lot = self._lot("t8423a")
        spec = self.choix(worker)
        self.assertEqual((spec["kind"], spec["lot"]), ("idle", str(lot)))
        self.assertTrue(spec.get("lot_wake"))
        with self._env():
            self.assertTrue(worker.run_turn(spec))
        self.assertEqual(self.tours(), 1, "un tour de lot part au réveil")
        self.assertEqual(self.dette("t8423a"), {})
        self.assertEqual(self.st.listing("t8423a")[0]["wakes"], 1)

    def test_pression_au_lancement_dit_la_raison_et_rejoue_le_reveil(self):
        worker = self._worker("t8423b")
        lot = self._lot("t8423b")
        spec = self.choix(worker)
        journal = self._refuse_par_pression(worker, spec)
        self.assertEqual(self.tours(), 0)
        self.assertTrue(any("wake on lot #%d: turn not started (host pressure at launch)"
                            % lot in ligne for ligne in journal), journal)
        # le choix borné au repos (#8326) ne saute pas la sélection
        worker._lot_wake_checked = float("-inf")
        self.assertTrue(worker._lot_wake_due(time.monotonic()))
        again = self.choix(worker)
        self.assertIsNotNone(again, "le réveil non lancé est rejoué")
        self.assertEqual(again["lot"], str(lot))
        self.assertTrue(again.get("lot_wake"))
        self.assertEqual(self.st.listing("t8423b")[0]["wakes"], 1, "pas recompté")
        with self._env():
            self.assertTrue(worker.run_turn(again))
        self.assertEqual(self.tours(), 1)
        self.assertEqual(self.dette("t8423b"), {})

    def test_rejeux_bornes_puis_delai_double(self):
        worker = self._worker("t8423c")
        self._lot("t8423c")
        worker.runner.idle_nudge = 100_000.0   # le délai doublé ne s'échoit pas
        spec = self.choix(worker)
        for _ in range(worker.LOT_WAKE_RETRY_MAX):
            self._refuse_par_pression(worker, spec)
            spec = self.choix(worker)
            self.assertIsNotNone(spec)
        journal = self._refuse_par_pression(worker, spec)
        self.assertTrue(any("given up after" in ligne for ligne in journal), journal)
        self.assertEqual(self.dette("t8423c"), {})
        self.assertIsNone(self.choix(worker), "le délai doublé reprend la main")
        self.assertEqual(self.st.listing("t8423c")[0]["wakes"], 1)

    def test_rejeu_oublie_quand_le_lot_n_est_plus_actionnable(self):
        worker = self._worker("t8423d")
        lot = self._lot("t8423d")
        spec = self.choix(worker)
        self._refuse_par_pression(worker, spec)
        self.assertIn(str(lot), self.dette("t8423d"))
        work.note(self.db, lot, "hold: attend une décision", actor="t8423d")
        self.assertIsNone(self.choix(worker))
        self.assertEqual(self.dette("t8423d"), {})
