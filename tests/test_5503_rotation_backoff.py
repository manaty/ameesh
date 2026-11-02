# SPDX-License-Identifier: AGPL-3.0-only
"""#5503 (issue #4065) — a harness that refuses the rotation summary turn.

1.16 then 1.33: the summary turn exits 1 before any provider call, the session
stays due, the next pass rotates again — one 2 $ `kept` reservation every few
seconds until the account's daily cap. Here: backoff per agent, rotation
without a summary turn after ROTATION_SUMMARY_MAX_FAILURES, the L48 breaker
counts the summary turn, a turn proven without provider call settles its
reservation at 0, and the kept reservations raise an alert.
"""
from __future__ import annotations

import dataclasses
import os
import time
from unittest import mock

from ameesh import exploitation, registry, runner as runner_mod, storage
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase
from .test_1469_account_budget import OPENAI, _profile

REFUSE = "doc harness: only mail turns are accepted"


class RotationBackoffTest(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        for table in ("turn_costs", "account_reservations", "account_budgets",
                      "budget_events", "spend_pending"):
            self.db.execute("DELETE FROM %s" % table)
        storage.of(self.db).budgets.account_put(OPENAI, 500.0, actor="human:t")

    def _worker(self, name: str, **cfg_kw):
        cfg = dataclasses.replace(self.cfg, budget_check_interval=0.0, accounts={"claude": [{
            "name": "k1", "type": "api_key_env", "key_env": "K",
            "billing_account": OPENAI, "check_credentials": False}]}, **cfg_kw)
        runner = Runner(cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def _env(self, **extra):
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log, "K": "sk-test"}
        env.update(extra)
        return mock.patch.dict(os.environ, env, clear=False)

    def _with_session(self, worker) -> None:
        """A first turn that succeeds: the session to rotate."""
        with self._env():
            worker._account = _profile(OPENAI, harness="claude")
            self.assertTrue(worker.run_turn({"kind": "prompt", "prompt": "t", "ids": []}))
        self.assertEqual(registry.get(self.db, worker.name)["session_id"], "claude-sess-1")
        self.clear_turns()

    def _states(self, agent):
        return [r["state"] for r in self.db.query(
            "select state from account_reservations where agent = %s order by id", (agent,))]

    def test_boucle_de_rotation_sans_backoff_reproduite_puis_bornee(self):
        worker = self._worker("doc")
        self._with_session(worker)
        with self._env(AMEESH_TEST_REFUSE=REFUSE), \
                mock.patch.object(worker, "rotation_due", return_value=True):
            # The runner's passes, one after the other (every 4-10 s in #4065).
            for _ in range(10):
                worker._account = _profile(OPENAI, harness="claude")
                self.assertFalse(worker.maybe_rotate())
        # One summary turn, not ten: the next one waits for its deadline.
        self.assertEqual(len(self.turns()), 1)
        self.assertEqual(worker.rotation_failures, 1)
        self.assertGreater(worker.rotation_retry_at, time.time() + 50)
        # The old session is kept.
        self.assertEqual(registry.get(self.db, "doc")["session_id"], "claude-sess-1")
        # No provider call: the reservation is settled at 0, not kept.
        self.assertEqual(self._states("doc"), ["settled", "settled"])
        usage = storage.of(self.db).budgets.account_usage(OPENAI)[0]
        self.assertAlmostEqual(usage["kept"], 0.0)

    def test_backoff_croissant_puis_rotation_sans_resume(self):
        worker = self._worker("doc2")
        self._with_session(worker)
        attentes = []
        with self._env(AMEESH_TEST_REFUSE=REFUSE), \
                mock.patch.object(worker, "rotation_due", return_value=True):
            for _ in range(runner_mod.ROTATION_SUMMARY_MAX_FAILURES):
                worker._account = _profile(OPENAI, harness="claude")
                self.assertFalse(worker.maybe_rotate())
                attentes.append(worker.rotation_retry_at - time.time())
                worker.rotation_retry_at = 0.0  # the deadline has passed
            self.assertEqual(len(self.turns()), runner_mod.ROTATION_SUMMARY_MAX_FAILURES)
            self.clear_turns()
            # 1 min, 2 min, 4 min…
            self.assertTrue(all(b > a * 1.5 for a, b in zip(attentes, attentes[1:])),
                            attentes)
            # Next attempt: deterministic brief, no harness launched.
            worker._account = _profile(OPENAI, harness="claude")
            self.assertTrue(worker.maybe_rotate())
        self.assertEqual(self.turns(), [])
        self.assertIsNone(registry.get(self.db, "doc2")["session_id"])
        self.assertEqual(worker.resume_origin, "ameesh")
        self.assertIn("Session rotation without a summary", worker.resume_summary)
        self.assertEqual((worker.rotation_failures, worker.rotation_retry_at), (0, 0.0))
        self.assertNotIn("kept", self._states("doc2"))

    def test_le_disjoncteur_l48_couvre_le_tour_de_resume(self):
        worker = self._worker("doc3", max_fast_failures=2)
        self._with_session(worker)
        with self._env(AMEESH_TEST_REFUSE=REFUSE), \
                mock.patch.object(worker, "rotation_due", return_value=True):
            for _ in range(2):
                worker._account = _profile(OPENAI, harness="claude")
                worker.maybe_rotate()
                worker.rotation_retry_at = 0.0
        self.assertTrue(worker.rotation_stopped)
        row = registry.get(self.db, "doc3")
        self.assertEqual(row["status"], "stopped")
        self.assertIn("consecutive fast failures", row["status_text"])

    def test_issue_incertaine_avec_flux_reste_kept(self):
        """Output on the stream: a provider call may have happened."""
        worker = self._worker("doc4")
        with self._env(AMEESH_TEST_EXIT="1"):
            worker._account = _profile(OPENAI, harness="claude")
            self.assertFalse(worker.run_turn({"kind": "prompt", "prompt": "t", "ids": []}))
        self.assertEqual(self._states("doc4"), ["kept"])

    def test_harnais_tue_par_un_signal_reste_kept(self):
        """B impl-07: a crash before the first line is uncertain, not a proof."""
        worker = self._worker("crash")
        with self._env(AMEESH_TEST_KILL_SELF="1"):
            worker._account = _profile(OPENAI, harness="claude")
            self.assertFalse(worker.run_turn({"kind": "prompt", "prompt": "t", "ids": []}))
        self.assertEqual(self._states("crash"), ["kept"])

    def test_arret_par_l_executeur_reste_kept(self):
        """A refusal while the runner restarts the agent is not a proof."""
        worker = self._worker("restart")
        worker.restarting.set()
        self.assertFalse(worker._sans_appel_fournisseur(None, 0, False, 0.5, None, 1))
        worker.restarting.clear()
        self.assertTrue(worker._sans_appel_fournisseur(None, 0, False, 0.5, None, 1))
        self.assertFalse(worker._sans_appel_fournisseur(None, 0, False, 0.5, None, -9))
        self.assertFalse(worker._sans_appel_fournisseur(None, 0, False, 0.5, None, 0))

    def test_tour_de_resume_differe_ne_compte_pas(self):
        worker = self._worker("differe")
        self._with_session(worker)
        with mock.patch.object(worker, "rotation_due", return_value=True), \
                mock.patch.object(worker, "run_turn", return_value=None):
            for _ in range(5):
                self.assertFalse(worker.maybe_rotate())
        self.assertEqual((worker.rotation_failures, worker.rotation_retry_at), (0, 0.0))

    def test_alertes_des_reservations_gardees(self):
        api = storage.of(self.db).budgets
        api.account_put(OPENAI, 10.0, actor="human:t")
        for n in range(5):
            got = api.account_reserve(OPENAI, "boucle", 2.0, "boucle:%d:1.0" % n)
            self.assertTrue(got["ok"])
            api.account_settle("boucle:%d:1.0" % n, state="kept", actor="runner")
        alerts = {a["type"]: a for a in exploitation._kept_reservations(self.db, time.time())}
        burst = alerts["reservations_kept_burst"]
        self.assertEqual((burst["agent"], burst["value"]), ("boucle", 5))
        self.assertIn("ameesh budget release", burst["detail"])
        cap = alerts["account_cap_kept"]
        self.assertEqual(cap["agent"], "boucle")
        self.assertEqual(cap["account"], OPENAI)
        self.assertIn("ameesh budget release", cap["detail"])
        # Released: both alerts go quiet.
        for row in api.account_kept(OPENAI):
            api.account_release(row["id"], actor="human:t")
        self.assertEqual(exploitation._kept_reservations(self.db, time.time()), [])
