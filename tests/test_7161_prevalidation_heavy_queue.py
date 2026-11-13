# SPDX-License-Identifier: AGPL-3.0-only
"""#7161 — la prévalidation passe par la file heavy de l'hôte.

N gels simultanés sur un hôte au plafond K : K au plus jouent, les autres
attendent leur créneau (« en file »), puis jouent. L'attente est suspendue de
l'échéance du run (elle n'est jamais un `run-incomplete`) ; sans créneau dans
le délai, la prévalidation est rendue « en file » et le gel reste écrit.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import heavy
from ameesh import mechanics
from ameesh import prevalidation as pv
from ameesh import prevalidation_calls as calls
from ameesh.deadline import Deadline


def block(hosts=("vm1",)):
    return mechanics.Prevalidation(classes=("P0", "P1"), contracts=(),
                                   hosts=hosts, timeout_seconds=60,
                                   parts_script="scripts/test-parts.py")


def green(lot=7161, sha="a" * 40):
    return {"schema": pv.REPORT_SCHEMA, "lot": lot, "sha": sha, "base": "b" * 40,
            "card": "", "modules": ["test_x"], "planned": ["test_x"],
            "missing": [], "parts": ["psycopg:1"], "planned_parts": ["psycopg:1"],
            "tests": 1, "host": "vm1", "outcome": "green", "exit": 0,
            "seconds": 0.1, "causes": [], "ok": True, "failed": [],
            "detail": []}


class FakePrepared:
    path = "ameesh-prevalidation-plan.json"
    tree = "/repo"

    def close(self, timeout=0):
        pass


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(state_dir=tempfile.mkdtemp(prefix="ameesh-7161-"),
                                   max_heavy_suites=2)
        env = mock.patch.dict(os.environ, {
            "AMEESH_MAX_HEAVY_SUITES": "",
            "AMEESH_PREVALIDATION_QUEUE_S": "5",
            "AGENT_MAIL_NAME": "test-7161",
        })
        env.start()
        self.addCleanup(env.stop)
        poll = mock.patch.object(heavy, "POLL_S", 0.01)
        poll.start()
        self.addCleanup(poll.stop)

    def release_later(self, held, delay=0.4):
        def later():
            time.sleep(delay)
            heavy.release(held[1])
        thread = threading.Thread(target=later)
        thread.start()
        self.addCleanup(thread.join)

    def test_the_queue_wait_is_paused_from_the_deadline(self):
        """Le temps passé à attendre un créneau ne consomme pas le budget."""
        one = SimpleNamespace(state_dir=self.cfg.state_dir, max_heavy_suites=1)
        held = heavy.acquire(one, command="suite")
        self.release_later(held)
        deadline = Deadline(5.0, wall=time.monotonic() + 60.0)
        with calls.heavy_queue(one, "prévalidation #7161", say=lambda text: None,
                               deadline=deadline) as waited:
            self.assertGreaterEqual(waited, 0.3)
        # sans la pause, il ne resterait que ~4,6 s à l'échéance
        self.assertGreater(deadline.remaining(), 4.5)

    def test_without_a_slot_within_the_bound_the_state_is_queued(self):
        """Aucun créneau dans le délai : « en file », jamais run-incomplete."""
        held = heavy.acquire(self.cfg, command="suite")
        try:
            one = SimpleNamespace(state_dir=self.cfg.state_dir, max_heavy_suites=1)
            with mock.patch.dict(os.environ, {"AMEESH_PREVALIDATION_QUEUE_S": "0.05"}):
                with self.assertRaises(pv.PrevalidationError) as caught:
                    with calls.heavy_queue(one, "prévalidation #7161",
                                           say=lambda text: None):
                        self.fail("aucun créneau libre ne devait être pris")
            self.assertEqual(caught.exception.cause, "queued")
            self.assertIn("en file", caught.exception.why)
        finally:
            heavy.release(held[1])

    def test_n_prevalidations_on_k_slots_run_k_at_a_time_then_the_others(self):
        """N gels sur un hôte au plafond K : K jouent, les autres en file, puis jouent."""
        n, k = 4, 2
        cfg = SimpleNamespace(state_dir=self.cfg.state_dir, max_heavy_suites=k)
        params = calls.Parameters(params=block(hosts=("vm1",)), repo="/canon",
                                  commit="c" * 40, project="ameesh")
        state = {"now": 0, "max": 0}
        lock = threading.Lock()
        start = threading.Barrier(n)
        results: list = []

        def fake_run_hosts(prepared, params, **kwargs):
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            time.sleep(0.2)          # le run invité
            with lock:
                state["now"] -= 1
            return green()

        def worker():
            start.wait()
            try:
                results.append(calls.run(
                    cfg, None, 7161, repo="/repo", head="a" * 40, base="b" * 40,
                    project="ameesh", record=False,
                    deadline=Deadline(30.0, wall=time.monotonic() + 120.0)))
            except Exception as exc:  # noqa: BLE001 - le fil rend son échec
                results.append(exc)

        with mock.patch.object(calls, "parameters", return_value=params), \
                mock.patch.object(calls, "tooling"), \
                mock.patch.object(calls, "map_for", return_value=("", "")), \
                mock.patch.object(calls, "prepare", return_value=FakePrepared()), \
                mock.patch.object(calls, "contract_fallback", return_value=None), \
                mock.patch.object(calls, "_run_hosts", side_effect=fake_run_hosts):
            threads = [threading.Thread(target=worker) for _ in range(n)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertEqual(len(results), n)
        self.assertTrue(all(isinstance(item, dict) and item.get("ok") for item in results),
                        [str(item) for item in results])
        self.assertEqual(state["max"], k, "au plus K prévalidations en parallèle")

    def test_the_run_budget_does_not_run_during_the_wait(self):
        """La prévalidation qui a attendu garde son budget de run entier."""
        cfg = SimpleNamespace(state_dir=self.cfg.state_dir, max_heavy_suites=1)
        params = calls.Parameters(params=block(hosts=("vm1",)), repo="/canon",
                                  commit="c" * 40, project="ameesh")
        seen: dict = {}

        def fake_run_hosts(prepared, params, **kwargs):
            seen["remaining"] = kwargs["deadline"].remaining()
            return green()

        held = heavy.acquire(cfg, command="suite")
        self.release_later(held)
        with mock.patch.object(calls, "parameters", return_value=params), \
                mock.patch.object(calls, "tooling"), \
                mock.patch.object(calls, "map_for", return_value=("", "")), \
                mock.patch.object(calls, "prepare", return_value=FakePrepared()), \
                mock.patch.object(calls, "contract_fallback", return_value=None), \
                mock.patch.object(calls, "_run_hosts", side_effect=fake_run_hosts):
            report = calls.run(cfg, None, 7161, repo="/repo", head="a" * 40,
                               base="b" * 40, project="ameesh", record=False,
                               deadline=Deadline(1.5, wall=time.monotonic() + 60.0))
        self.assertTrue(report.get("ok"))
        self.assertGreaterEqual(report.get("queue_wait_s", 0), 0.3,
                                "la preuve dit que la prévalidation a attendu")
        # sans la pause, l'échéance n'aurait plus que ~1,1 s au run
        self.assertGreater(seen["remaining"], 1.3)

    def test_a_queued_freeze_is_journaled_en_file_not_unavailable(self):
        """Un gel qui n'obtient pas de créneau est dit « en file »."""
        notes: list = []
        item = {"id": 7161, "app": "ameesh", "branch": "lot/7161-x",
                "branch_target": "main"}
        with mock.patch.object(calls.work, "get", return_value=item), \
                mock.patch.object(calls, "composer_tested", return_value=None), \
                mock.patch.object(calls, "repository", return_value="/repo"), \
                mock.patch.object(calls, "target_base", return_value="b" * 40), \
                mock.patch.object(calls, "run",
                                  side_effect=pv.PrevalidationError("queued",
                                                                    "aucun créneau libre")), \
                mock.patch.object(calls, "_journal",
                                  side_effect=lambda db, lot, text, **kwargs:
                                  notes.append(text)):
            outcome = calls.after_freeze(SimpleNamespace(), object(), 7161, "a" * 40)
        self.assertEqual(outcome.get("state"), "queued")
        self.assertTrue(notes)
        self.assertIn("prevalidation en file", notes[0])
        self.assertNotIn("unavailable", notes[0])

    def test_the_timeout_reason_keeps_the_caller_budget_after_a_wait(self):
        """Le motif nomme le budget reçu, pas le temps de mur gonflé par l'attente."""
        now = [100.0]
        deadline = Deadline(90.0, clock=lambda: now[0], wall=now[0] + 2000.0)
        deadline.pause(600.0)
        now[0] = 800.0
        self.assertTrue(deadline.expired())
        why = calls._timed_out(deadline, "run")
        self.assertIn("budget de l'appelant (90 s)", why)
        self.assertNotIn("(700 s)", why)

    def test_a_wait_past_the_freeze_wall_returns_queued_under_the_composer_timeout(self):
        """Créneaux pris au-delà du mur du gel : « en file » rendu avant 570 s.

        Chemin réel de la porte : `gate_packet` -> `run`, avec l'échéance d'un
        gel (contrôles statiques déjà passés), horloge simulée et créneaux qui
        restent pris. Sans la borne murale, l'attente irait jusqu'aux 900 s de
        la file et le compositeur tuerait la commande à 600 s (#40078/#40089)."""
        now = [1000.0]
        clock = lambda: now[0]
        freeze = calls.freeze_deadline(clock=clock)
        now[0] += 40.0                 # les contrôles statiques du gel
        deadline = calls.caller_deadline(clock=clock, limit=freeze)
        seen: dict = {}

        def busy(cfg, *, command="", log=None, max_wait_s=None, **kwargs):
            seen["bound"] = max_wait_s
            now[0] += float(max_wait_s or 0.0)   # aucun créneau ne se libère
            return None

        cfg = SimpleNamespace(state_dir=self.cfg.state_dir, max_heavy_suites=2)
        params = calls.Parameters(params=block(hosts=("vm1",)), repo="/canon",
                                  commit="c" * 40, project="ameesh")
        item = {"id": 7161, "app": "ameesh", "branch": "lot/7161-x"}
        with mock.patch.dict(os.environ,
                             {"AMEESH_PREVALIDATION_QUEUE_S": "900"}), \
                mock.patch.object(calls, "parameters", return_value=params), \
                mock.patch.object(calls, "tooling"), \
                mock.patch.object(calls, "map_for", return_value=("", "")), \
                mock.patch.object(calls, "composer_tested", return_value=None), \
                mock.patch.object(calls, "repository", return_value="/repo"), \
                mock.patch.object(calls, "prepare") as prepare, \
                mock.patch.object(heavy, "acquire", side_effect=busy):
            got = calls.gate_packet(cfg, None, item,
                                    {"sha": "a" * 40, "base": "b" * 40},
                                    deadline=deadline)
        prepare.assert_not_called()
        self.assertEqual(got.get("state"), "unavailable")
        self.assertEqual(got.get("cause"), "queued")
        # l'attente est bornée par le mur du gel, pas par les 900 s de la file
        self.assertLessEqual(seen["bound"], 500.0)
        self.assertLessEqual(now[0] - 1000.0, calls.FREEZE_BUDGET_S)
        self.assertLess(now[0] - 1000.0, 600.0)   # FREEZE_TIMEOUT du compositeur


if __name__ == "__main__":
    unittest.main()
