# SPDX-License-Identifier: AGPL-3.0-only
"""#5699 — la relance au repos décide mécaniquement avant d'ouvrir un tour.

Constat (analyse profonde de l'auditeur, 4/11) : `IDLE_PROMPT` faisait ouvrir
un tour de modèle à l'agent au repos pour qu'il écrive « rien de dû » au
pilote. Le 03/11 : 123 tours de ce type sur le mesh (8,50 USD, 82 M jetons),
dont 20 pour l'auditeur (2,96 USD). Attendu : avant d'ouvrir le tour,
l'exécuteur évalue sans modèle les corrections dues, les avis dus et le lot
actionnable ; tout vide => aucun tour, statut de disponibilité posté
mécaniquement. Un agent à passages planifiés (auditeur) n'a donc plus de
relance au repos quand il n'a rien de dû : ses minuteurs restent son horloge.
"""
from __future__ import annotations

import os
import threading
import time
import types
import unittest
from unittest import mock

from ameesh import adapters, registry, reprise, runner

from . import test_1270_rate_limit_retry as t1270
from .support import PgTestCase


class IdleDecisionTest(t1270.TourDeRepriseTest):
    """La décision seule : worker factice, lectures dues neutralisées."""

    def _idle_worker(self):
        w = self._worker("qa-idle", time.time() + 60)   # aucune reprise cadencée
        w.nudged, w.last_activity = False, time.monotonic() - 10_000
        w.agent = {}
        w._idle_pilot = lambda: ""
        w._idle_actionable = mock.Mock(return_value=False)
        return w

    def _owed(self, fixes=None, reviews=None):
        """Neutralise les deux lectures dues ; `[]` = vraiment rien."""
        return mock.patch.object(
            runner, "_idle_owed_lines",
            return_value=([reprise.NO_FIX_OWED] if fixes is None else fixes,
                          [reprise.NO_PENDING_REVIEW] if reviews is None else reviews))

    def test_nothing_due_posts_availability_without_opening_a_turn(self):
        w = self._idle_worker()
        self.assertTrue(w.idle_due())
        with self._owed(), \
                mock.patch.object(runner.registry, "set_status_text") as posted:
            spec = self._pick(w)
        self.assertIsNone(spec, "aucun tour ne part quand rien n'est dû")
        posted.assert_called_once_with(w.db, w.name, runner.IDLE_AVAILABLE_STATUS)
        self.assertTrue(w.nudged, "la relance reste unique pour cette période")
        self.assertFalse(w.idle_due())

    def test_a_fix_owed_still_opens_the_turn(self):
        w = self._idle_worker()
        with self._owed(fixes=["- #12 [qa] une correction"]), \
                mock.patch.object(runner.registry, "set_status_text") as posted:
            spec = self._pick(w)
        self.assertEqual(spec["kind"], "idle")
        self.assertIn(adapters.IDLE_FIXES_HEADER, spec["prompt"])
        self.assertIn("#12", spec["prompt"])
        posted.assert_not_called()

    def test_a_review_owed_still_opens_the_turn(self):
        w = self._idle_worker()
        with self._owed(reviews=["- #13 [qa] un avis"]), \
                mock.patch.object(runner.registry, "set_status_text") as posted:
            spec = self._pick(w)
        self.assertEqual(spec["kind"], "idle")
        self.assertIn(adapters.IDLE_REVIEWS_HEADER, spec["prompt"])
        self.assertIn("#13", spec["prompt"])
        posted.assert_not_called()

    def test_an_actionable_lot_still_opens_the_turn(self):
        w = self._idle_worker()
        w._idle_actionable = mock.Mock(return_value=True)
        with self._owed():
            spec = self._pick(w)
        self.assertEqual(spec["kind"], "idle")
        self.assertEqual(spec["prompt"], adapters.IDLE_PROMPT_NO_PILOT)

    def test_an_unreadable_owed_queue_never_concludes_nothing_due(self):
        w = self._idle_worker()
        with self._owed(fixes=["(fixes owed unreadable: boom — "
                               "`ameesh work fixes-owed`)"]), \
                mock.patch.object(runner.registry, "set_status_text") as posted:
            spec = self._pick(w)
        self.assertEqual(spec["kind"], "idle")
        posted.assert_not_called()

    def test_the_availability_status_is_best_effort(self):
        w = self._idle_worker()
        with mock.patch.object(runner.registry, "set_status_text",
                               side_effect=RuntimeError("registry read-only")):
            w._post_idle_available()  # ne lève pas : le tour reste économisé

    def test_the_actionable_read_is_read_only(self):
        w = self._idle_worker()
        rows = [{"id": 7, "project": "ameesh"}]
        store = types.SimpleNamespace(
            lot_sessions=types.SimpleNamespace(actionable=mock.Mock(return_value=rows)))
        with mock.patch.object(runner.storage, "of", return_value=store), \
                mock.patch.object(w, "_lot_workdir_blocked", return_value=False), \
                mock.patch.object(w, "_reservation_fits", return_value=True):
            self.assertTrue(runner.AgentWorker._idle_actionable(w))
        store.lot_sessions.actionable.assert_called_once()

    def test_an_unactionable_lot_does_not_hold_a_turn(self):
        w = self._idle_worker()
        rows = [{"id": 7, "project": "ameesh"}]
        store = types.SimpleNamespace(
            lot_sessions=types.SimpleNamespace(actionable=mock.Mock(return_value=rows)))
        with mock.patch.object(runner.storage, "of", return_value=store), \
                mock.patch.object(w, "_lot_workdir_blocked", return_value=True), \
                mock.patch.object(w, "_reservation_fits", return_value=True):
            self.assertFalse(runner.AgentWorker._idle_actionable(w))

    def test_an_unreadable_actionable_read_never_concludes_nothing_due(self):
        w = self._idle_worker()
        store = types.SimpleNamespace(
            lot_sessions=types.SimpleNamespace(
                actionable=mock.Mock(side_effect=runner.db_mod.DbError("boom"))))
        with mock.patch.object(runner.storage, "of", return_value=store):
            self.assertTrue(runner.AgentWorker._idle_actionable(w))

    def test_an_unexpected_owed_read_is_reported_unreadable(self):
        with mock.patch("ameesh.reprise.fixes_owed_lines",
                        side_effect=RuntimeError("boom")), \
                mock.patch("ameesh.reprise.pending_review_lines",
                           side_effect=RuntimeError("boom")):
            fixes, reviews = runner._idle_owed_lines(object(), "qa-idle")
        self.assertTrue(fixes[0].startswith("(fixes owed unreadable"))
        self.assertTrue(reviews[0].startswith("(pending reviews unreadable"))

    # les cas de #1270 restent ceux de leur propre module
    test_peek_wakes_at_the_retry_deadline = None
    test_peek_does_not_wake_before_the_deadline = None
    test_peek_does_not_wake_on_an_exhausted_quota = None
    test_echeance_passee_lance_un_tour_de_reprise = None
    test_avant_l_echeance_rien = None
    test_un_quota_n_est_pas_repris_sans_fin = None


class IdleNothingDueDbTest(PgTestCase):
    """Le vrai chemin : registre, corrections/avis et lots réels, aucun tour."""

    def _worker(self, name: str):
        w = runner.AgentWorker.__new__(runner.AgentWorker)
        threading.Thread.__init__(w, name=name, daemon=True)  # jamais démarré
        w.db, w.epoch, w.name = self.db, 1, name
        w.runner = types.SimpleNamespace(
            runner_id="r", idle_nudge=1200.0,
            gate_holds=lambda: False, host_pressure=lambda: {})
        w.rate_limit, w.rate_limit_streak = "", 0
        w.rate_limit_since, w.rate_limit_retry_at = 0.0, 0.0
        w.nudged, w.last_activity = False, time.monotonic() - 10_000
        w._session_en_attente, w.suite, w._reprise_attach = None, None, False
        w.agent = registry.get(self.db, name)
        return w

    def _pick(self, w):
        oui, non = mock.Mock(return_value=True), mock.Mock(return_value=False)
        with mock.patch.multiple(
                w, _marque_base=non, maybe_relocate=non, budget_ok=oui,
                _held_by_pressure=non, _host_pressure_release=mock.Mock(),
                host_ready=oui, workdir_ready=oui, auth_ready=oui,
                credits_ready=oui,
                _lot_wake_spec=mock.Mock(return_value=None),
                _scheduled_wake_spec=mock.Mock(return_value=None)), \
                mock.patch("ameesh.runner.registry.take_pending_prompt",
                           return_value=None), \
                mock.patch("ameesh.runner.mail.unread_urgent", return_value=[]), \
                mock.patch("ameesh.runner.mail.unread", return_value=[]), \
                mock.patch("ameesh.runner.account_turn.continuity", return_value=True):
            return w._pick()

    def test_a_real_agent_with_nothing_due_gets_no_turn_and_a_status(self):
        cwd = os.path.join(self.tmp, "work", "qa-idle")
        os.makedirs(cwd, exist_ok=True)
        registry.upsert(self.db, "qa-idle", harness="claude", host=self.cfg.host,
                        cwd=cwd, status="idle")
        w = self._worker("qa-idle")
        self.assertTrue(w.idle_due())
        spec = self._pick(w)
        self.assertIsNone(spec)
        self.assertTrue(w.nudged)
        row = registry.get(self.db, "qa-idle")
        self.assertEqual((row["status"], row["status_text"]),
                         ("idle", runner.IDLE_AVAILABLE_STATUS))


if __name__ == "__main__":
    unittest.main()
